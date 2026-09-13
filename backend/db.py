"""
Data access layer.

Design goal: the application code is written once against a tiny, explicit
interface and runs on **SQLite (zero-config, default)** or **PostgreSQL
(production)** without per-dialect branching at every call site.

Consequences of that choice, all localised in this file:

  * SQL is authored with `?` placeholders and rewritten for psycopg (`%s`).
  * Dialect-specific expressions go through helpers (`now_sql`, `hours_ahead_sql`).
  * DDL lives in `backend/migrations.py`, keyed by dialect.
  * No ORM. No session cache. Explicit SQL keeps the query cost obvious,
    which is what the pagination / index requirements in the spec need.

Connections are per-request (`g.db`) or per-task for the background janitor.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import get_config
from .log import get_logger

log = get_logger("db")

SQLITE = "sqlite"
POSTGRES = "postgres"

# Columns we always want available for hot paths; see migrations for the DDL.
PLACEHOLDER_RE = re.compile(r"\?(?=(?:[^']*'[^']*')*[^']*$)")


def _to_pg(sql: str) -> str:
    """`?` -> `%s`, skipping `?` characters inside single-quoted literals."""
    out, in_str, i = [], False, 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class Row(dict):
    """Dict row that also tolerates `row[i]` and attribute-style misuse."""

    __getattr__ = dict.get      # type: ignore[assignment]


#: LIKE wildcards plus the escape character itself, so user text cannot widen a
#: search into a full-table scan (`%%%`) or invent `_` single-char matches.
_LIKE_META = re.compile("([%_" + chr(92) + chr(92) + "])")
#: SQL text that makes a backslash the LIKE wildcard-escape character
ESCAPE_CLAUSE = " ESCAPE '" + chr(92) + "'"
_TX_LOCAL = threading.local()


def _tx_depths() -> dict[int, int]:
    """Per-thread {id(connection): nesting depth} map for re-entrant `tx()`."""
    depths = getattr(_TX_LOCAL, "depths", None)
    if depths is None:
        depths = _TX_LOCAL.depths = {}
    return depths


class Database:
    """Thin, dialect-aware connection factory + query helpers."""

    def __init__(self, engine: str | None = None, *, db_path: str | None = None,
                 pg_dsn: str | None = None, statement_timeout_ms: int | None = None):
        cfg = get_config()
        engine = (engine or cfg.db_engine or SQLITE).lower()
        if engine in {"postgres", "postgresql", "pg"}:
            engine = POSTGRES
        else:
            engine = SQLITE
        self.engine = engine
        self.db_path = db_path or str(cfg.db_file)
        self.pg_dsn = pg_dsn or cfg.pg_dsn
        self.statement_timeout_ms = statement_timeout_ms or cfg.db_statement_timeout_ms
        if self.engine == POSTGRES and not self.pg_dsn:
            raise RuntimeError("VOLEXTURN_DB_ENGINE=postgres requires DATABASE_URL")
        if self.engine == SQLITE:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._pg = None                      # lazily imported psycopg module
        self._dialect_tag = 0                # bumps if sqlite/postgres switch mid-run
        log.info("db_init", extra={"ctx": {"engine": self.engine,
                                           "path": self.db_path if self.engine == SQLITE else "(dsn)"}})

    # -------------------------------------------------------------- connections
    def connect(self) -> Any:
        if self.engine == SQLITE:
            conn = sqlite3.connect(
                self.db_path,
                timeout=15.0,
                isolation_level=None,          # we manage transactions explicitly
                check_same_thread=False,
                detect_types=0,
            )
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # WAL keeps readers non-blocking next to the janitor thread.
            for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                           "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=8000",
                           "PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-16000"):
                try:
                    cur.execute(pragma)
                except sqlite3.Error:
                    pass
            cur.close()
            return conn
        return self._pg_connect()

    def _pg_connect(self) -> Any:
        if self._pg is None:
            try:                            # psycopg3 first, then psycopg2
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore
                self._pg = ("psycopg3", psycopg, dict_row)
            except ImportError:
                try:
                    import psycopg2  # type: ignore
                    from psycopg2.extras import RealDictCursor  # type: ignore
                    self._pg = ("psycopg2", psycopg2, RealDictCursor)
                except ImportError as exc:  # pragma: no cover - optional dep
                    raise RuntimeError(
                        "PostgreSQL support needs `psycopg[binary]` or `psycopg2-binary` "
                        "installed (it is an optional dependency)."
                    ) from exc
        flavour, module, row_factory = self._pg
        if flavour == "psycopg3":
            conn = module.connect(self.pg_dsn, row_factory=row_factory)
        else:                                # pragma: no cover
            conn = module.connect(self.pg_dsn, cursor_factory=row_factory)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
        return conn

    # ------------------------------------------------------------- execution
    def _prep(self, sql: str) -> str:
        return _to_pg(sql) if self.engine == POSTGRES else sql

    @staticmethod
    def _norm(params: Sequence | dict | None) -> tuple:
        if params is None:
            return ()
        if isinstance(params, dict):
            return params                      # type: ignore[return-value]
        return tuple(params)

    def execute(self, conn, sql: str, params: Sequence | None = None) -> Any:
        """Run one statement; returns the cursor (has .rowcount / .lastrowid)."""
        cur = conn.cursor()
        try:
            cur.execute(self._prep(sql), self._norm(params))
        except Exception:
            cur.close()
            raise
        return cur

    def query(self, conn, sql: str, params: Sequence | None = None) -> list[Row]:
        cur = self.execute(conn, sql, params)
        try:
            fetched = cur.fetchall()
        finally:
            cur.close()
        return [dict(r) for r in fetched]

    def query_one(self, conn, sql: str, params: Sequence | None = None) -> Row | None:
        rows = self.query(conn, sql, params)
        return rows[0] if rows else None

    def scalar(self, conn, sql: str, params: Sequence | None = None, default: Any = 0) -> Any:
        row = self.query_one(conn, sql, params)
        if not row:
            return default
        val = next(iter(row.values()))
        return default if val is None else val

    def insert(self, conn, sql: str, params: Sequence | None = None) -> int:
        """Execute an INSERT and return the new primary key."""
        cur = self.execute(conn, sql, params)
        try:
            if self.engine == POSTGRES:
                pk = None
                try:
                    pk = cur.fetchone()
                except Exception:
                    pk = None
                return int(pk["id"] if isinstance(pk, dict) else pk[0]) if pk else 0
            return int(cur.lastrowid)
        finally:
            cur.close()

    def insert_returning(self, conn, sql: str, params: Sequence | None = None,
                         table: str = "", pk: str = "id"):
        """
        Portable insert-then-read-id. `sql` must not include a RETURNING clause;
        on SQLite we use lastrowid, on Postgres we append RETURNING.
        """
        if self.engine == POSTGRES:
            suffix = f" RETURNING {pk}" if table and pk else ""
            cur = self.execute(conn, sql.rstrip().rstrip(";") + suffix, params)
            try:
                row = cur.fetchone()
                return int(row[pk] if isinstance(row, dict) else row[0]) if row else 0
            finally:
                cur.close()
        cur = self.execute(conn, sql, params)
        try:
            return int(cur.lastrowid)
        finally:
            cur.close()

    def ignore_clause(self, sql: str, *, on: Sequence[str] | None = None,
                      update: Sequence[str] | None = None) -> str:
        """
        Append the dialect-correct conflict behaviour to an INSERT.

        Postgres needs an explicit ON CONFLICT target for a DO UPDATE; SQLite
        gets `INSERT OR IGNORE` which leans on the same UNIQUE constraint.
        """
        if self.engine == POSTGRES:
            if update:
                tgt = f"({', '.join(on)})" if on else ""
                set_ = ", ".join(f"{c} = EXCLUDED.{c}" for c in update)
                return sql.rstrip().rstrip(";") + f" ON CONFLICT {tgt} DO UPDATE SET {set_}"
            tgt = f"({', '.join(on)})" if on else ""
            return sql.rstrip().rstrip(";") + f" ON CONFLICT {tgt} DO NOTHING"
        if update:
            tgt = f"({', '.join(on)})" if on else ""
            set_ = ", ".join(f"{c} = excluded.{c}" for c in update)
            return sql.rstrip().rstrip(";") + f" ON CONFLICT {tgt} DO UPDATE SET {set_}"
        return sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)

    def insert_ignore(self, conn, table: str, values: dict[str, Any]) -> int | None:
        """`INSERT OR IGNORE` (sqlite) / `ON CONFLICT DO NOTHING` (postgres)."""
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({marks})"
        if self.engine == POSTGRES:
            sql += " ON CONFLICT DO NOTHING"
        else:
            sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
        cur = self.execute(conn, sql, list(values.values()))
        try:
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else None
        finally:
            cur.close()

    def upsert(self, conn, table: str, columns: Sequence[str], values: Sequence,
               conflict: Sequence[str], updates: Sequence[str]) -> None:
        """
        Conflict-aware write that reads the same on both engines.
        `updates` are column names set to the incoming value.
        """
        marks = ", ".join("?" for _ in columns)
        cols = ", ".join(columns)
        if self.engine == POSTGRES:
            set_ = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates) or "NULL"
            sql = (f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
                   f"ON CONFLICT ({', '.join(conflict)}) DO "
                   + (f"SET {set_}" if updates else "NOTHING"))
        else:
            set_ = ", ".join(f"{c} = excluded.{c}" for c in updates) or None
            sql = f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT ({', '.join(conflict)})"
            sql += f" DO UPDATE SET {set_}" if set_ else " DO NOTHING"
        self.execute(conn, sql, list(values)).close()

    def delete_where(self, conn, table: str, where: str, params: Sequence) -> int:
        cur = self.execute(conn, f"DELETE FROM {table} WHERE {where}", params)
        try:
            return cur.rowcount
        finally:
            cur.close()

    # ------------------------------------------------------------ transactions
    @contextmanager
    def tx(self, conn) -> Iterator[Any]:
        """
        Explicit transaction, re-entrant.

        SQLite is opened in autocommit mode (`isolation_level=None`), so the
        BEGIN/COMMIT here are real and cheap. Nesting depth lives in a
        thread-local map rather than on the connection object, because
        `sqlite3.Connection` refuses arbitrary attributes — and only the
        outermost frame may commit or roll back, otherwise an inner `with`
        would tear up the outer one's work.
        """
        owns = False
        if conn is None:
            conn = self.connect()
            owns = True
        depths = _tx_depths()
        key = id(conn)
        depth = depths.get(key, 0)
        depths[key] = depth + 1
        try:
            if depth == 0:
                if self.engine == POSTGRES:
                    pass          # psycopg opens a transaction implicitly
                else:
                    conn.execute("BEGIN IMMEDIATE")
            yield conn
            if depth == 0:
                conn.commit()
        except Exception:
            if depth == 0:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if depth == 0:
                depths.pop(key, None)
            else:
                depths[key] = depth
            if owns:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------- dialect helpers
    def now_sql(self, offset: str | None = None) -> str:
        """
        Timestamp expression. `offset` is an ISO-8601-ish duration we translate,
        e.g. `'-24 hours'`. Callers should prefer `hours_ahead_sql`.
        """
        if self.engine == POSTGRES:
            return f"(now() {offset})" if offset else "now()"
        return f"datetime('now', '{offset}')" if offset else "datetime('now')"

    def hours_ahead_sql(self, hours: int | float, col: str | None = None) -> str:
        base = self.now_sql() if col is None else col
        if self.engine == POSTGRES:
            return f"({base} + interval '{float(hours):g} hours')"
        return f"datetime({base}, '+{float(hours):g} hours')"

    def is_expired_sql(self, col: str, seconds: int) -> str:
        """True when `col` is older than `seconds` (NULL counts as expired)."""
        if self.engine == POSTGRES:
            return f"({col} IS NULL OR {col} < (now() - interval '{int(seconds)} seconds'))"
        return f"({col} IS NULL OR {col} < datetime('now', '-{int(seconds)} seconds'))"

    def ilike(self, col: str) -> str:
        """
        A complete case-insensitive *contains* comparison for `col`, with the
        parameter placeholder in place: `lower(col) LIKE ? ESCAPE '\\'`.

        Paired with `ilike_params(needle)`. Callers must not assemble the
        fragments by hand: an earlier revision built
        `… {ilike_casefold(c)} {ilike('?')}`, which expanded to
        `lower(c) ? LIKE ?` and failed on every search. One call, one shape.
        """
        folded = col if self.engine == POSTGRES else f"lower({col})"
        op = "ILIKE" if self.engine == POSTGRES else "LIKE"
        return f"{folded} {op} ?{ESCAPE_CLAUSE}"
    def ilike_params(self, needle: str) -> tuple[str, ...]:
        """
        One bound parameter: wildcard-escaped, `%`-wrapped, and lower()ed on
        SQLite (LIKE there is only case-insensitive for ASCII, and the column
        side is lower()ed by `ilike_casefold`).
        """
        escaped = _LIKE_META.sub(r"\\\1", str(needle or ""))
        if self.engine == POSTGRES:
            return (f"%{escaped}%",)
        return (f"%{escaped.lower()}%",)

    def limit_offset(self, limit: int, offset: int) -> str:
        return f"LIMIT {int(limit)} OFFSET {int(offset)}"

    def random_fn(self) -> str:
        return "random()" if self.engine == SQLITE else "random()"

    def has_table(self, conn, name: str) -> bool:
        if self.engine == SQLITE:
            return bool(self.query_one(
                conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))
        return bool(self.query_one(
            conn,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name=?",
            (name,)))

    def columns(self, conn, table: str) -> set[str]:
        if self.engine == SQLITE:
            return {r["name"] for r in self.query(conn, f"PRAGMA table_info({table})")}
        rows = self.query(
            conn,
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=?", (table,))
        return {r["name"] for r in rows}

    def lock(self) -> None:
        """No-op hook kept so callers can be explicit about write serialisation."""

    def close(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# process-wide accessor
# --------------------------------------------------------------------------
_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database | None) -> None:
    """Test hook: swap the whole data layer."""
    global _db
    _db = db


def rebuild_db() -> Database:
    global _db
    _db = None
    return get_db()


def dict_rows(rows: Iterable[dict]) -> list[dict]:
    return [dict(r) for r in rows]


def split_sql(script: str) -> list[str]:
    """Split a DDL script on `;` while respecting quoted strings."""
    stmts, buf, in_str = [], [], False
    for ch in script:
        if ch == "'":
            in_str = not in_str
        if ch == ";" and not in_str:
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts
