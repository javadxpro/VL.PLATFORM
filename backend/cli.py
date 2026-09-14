"""
Command line tools: `python -m backend <command>`.

    run            serve the app (same as python server.py)
    migrate        apply schema migrations and print the report
    create-admin   create or promote an administrator (replaces the old
                   hardcoded admin/admin123 seed)
    passwd         set a user's password and revoke their sessions
    token          print a session token for a user (dev/testing only)
    routes         list the HTTP surface, canonical vs legacy
    doctor         pre-flight checks; exit code 1 when something is broken
    janitor        run one housekeeping tick now (sessions, orphan files, …)
    sweep          list or delete upload files nothing references

No third-party CLI framework on purpose: `argparse` keeps the deploy image small.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import json
import os
import sys
from typing import Any

from .config import get_config
from .db import Database
from .log import configure_logging, get_logger

log = get_logger("cli")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _db() -> Database:
    cfg = get_config()
    return Database(engine=cfg.db_engine, db_path=str(cfg.db_file), pg_dsn=cfg.pg_dsn)


#: set while `--json` is in effect. A machine-readable run must emit *only* the
#: document — one human line in front of it is enough to break
#: `python -m backend doctor --json | jq`.
_QUIET = False


def _say(msg: str) -> None:
    if not _QUIET:
        print(msg)


def _ensure_schema(db: Database, *, why: str) -> None:
    """
    Bring the schema up to date before a command reads tables.

    `create-admin` is a *bootstrap* command: on a fresh install it is normally run
    before the server has ever booted, and the migration that `create_app()` does on
    startup has therefore not happened yet. It used to die with

        ❌ OperationalError: no such table: users

    which reads like a broken install instead of a missing first step — and worse,
    someone who misses that line on stderr ends up with no admin row at all, then
    a login screen that only says "نام کاربری یا رمز عبور اشتباه است".

    Migrations are idempotent, so this costs nothing on an already-migrated
    database. `doctor` deliberately does *not* call it: a diagnostic must not
    change the state it is reporting on.
    """
    from . import migrations
    try:
        report = migrations.run_migrations(db)
    except Exception as exc:  # noqa: BLE001 - surfaced as a CLI error, not a trace
        raise SystemExit(f"❌ مهاجرت اسکیمای دیتابیس نشد: {type(exc).__name__}: {exc}") from exc
    if report.errors:
        raise SystemExit("❌ مهاجرت با خطا تمام شد: " + "; ".join(report.errors[:3]))
    if report.applied:
        _say(f"  · {len(report.applied)} مهاجرت اعمال شد تا نسخه {report.current_version} ({why})")


def _ok(msg: str) -> None:
    _say(f"  ✅ {msg}")


def _warn(msg: str) -> None:
    _say(f"  ⚠️  {msg}")


def _fail(msg: str) -> None:
    _say(f"  ❌ {msg}")


def _echo(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print(value)


# --------------------------------------------------------------------------
# run / migrate
# --------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    from .app import create_app, socketio
    cfg = get_config()
    app = create_app()
    port = args.port or cfg.port
    host = args.host or cfg.host
    print(f"🚀 Volexturn on http://{host}:{port}  (engine={cfg.db_engine})", flush=True)
    socketio.run(app, host=host, port=port, debug=False,
                 use_reloader=bool(args.reload), allow_unsafe_werkzeug=True)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from .migrations import run_migrations
    db = _db()
    report = run_migrations(db, force=args.force)
    print(f"engine={report.engine} version={report.current_version} "
          f"legacy_detected={report.legacy_detected}")
    for line in report.applied:
        _ok(f"applied {line}")
    for line in report.skipped:
        print(f"  ⏭  {line}")
    for line in report.errors:
        _fail(line)
    return 1 if report.errors else 0


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
def cmd_create_admin(args: argparse.Namespace) -> int:
    """
    Seed an administrator.

    Replaces the legacy `admin/admin123` auto-seed (docs/AUDIT.md §7): nothing is
    created with a guessable password, and the CLI is the only offline path to a
    first admin besides the loopback-only setup endpoint.
    """
    from .security import check_password_strength, hash_password
    db = _db()
    _ensure_schema(db, why="scaffold for create-admin")
    conn = db.connect()
    try:
        row = db.query_one(conn, "SELECT id, role FROM users WHERE username = ?", (args.username,))
        if row:
            if (row["role"] or "user") == "admin" and not args.password:
                _ok(f"@{args.username} already is an administrator (id={row['id']})")
                return 0
            pw = args.password or _prompt(args.username)
            ok, why, _ = check_password_strength(pw, username=args.username)
            if not ok:
                _fail(f"رمز ضعیف است: {why}")
                return 2
            db.execute(conn, """UPDATE users SET role = 'admin', password = ?, status = 'active',
                                 is_banned = 0 WHERE id = ?""", (hash_password(pw), row["id"])).close()
            conn.commit()
            _ok(f"@{args.username} promoted to administrator (id={row['id']})")
            return 0

        pw = args.password or _prompt(args.username)
        ok, why, _ = check_password_strength(pw, username=args.username)
        if not ok:
            _fail(f"رمز ضعیف است: {why}")
            return 2
        # No email column exists in this schema (the legacy app never had one),
        # so the CLI does not pretend otherwise.
        uid = db.insert(conn, """
            INSERT INTO users (username, password, full_name, bio, role, status, presence)
            VALUES (?, ?, ?, ?, 'admin', 'active', 'online')""",
            (args.username, hash_password(pw), args.full_name or "Administrator",
             "administrator"))
        conn.commit()
        _ok(f"administrator created: @{args.username} (id={uid})")
        print("  ℹ️  رمز در هیچ لاگی نوشته نمی‌شود؛ اگر فراموش شد با `passwd` عوضش کنید.")
        return 0
    finally:
        db.close(conn)


def _prompt(username: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("❌ بدون TTY باید --password بدهید (یا VOLEXTURN_ADMIN_PASSWORD)")
    first = getpass.getpass(f"رمز جدید برای @{username}: ")
    if first != getpass.getpass("تکرار رمز: "):
        raise SystemExit("❌ رمزها یکسان نبودند")
    return first


def cmd_passwd(args: argparse.Namespace) -> int:
    from .auth import revoke_user_sessions
    from .security import check_password_strength
    db = _db()
    _ensure_schema(db, why="passwd needs the users table")
    conn = db.connect()
    try:
        row = db.query_one(conn, "SELECT id FROM users WHERE username = ?", (args.username,))
        if row is None:
            _fail(f"کاربری با نام {args.username} نیست")
            return 1
        pw = args.password or _prompt(args.username)
        ok, why, _ = check_password_strength(pw, username=args.username)
        if not ok:
            _fail(f"رمز ضعیف است: {why}")
            return 2
        from .security import hash_password
        db.execute(conn, "UPDATE users SET password = ?, must_change_password = 0 WHERE id = ?",
                   (hash_password(pw), row["id"])).close()
        revoke_user_sessions(db, conn, int(row["id"]), reason="password_reset")
        conn.commit()
        _ok(f"رمز @{args.username} عوض شد و همهٔ نشست‌هایش باطل شد")
        return 0
    finally:
        db.close(conn)


def cmd_token(args: argparse.Namespace) -> int:
    """
    Mint a session token.

    A debugging shortcut for local/dev use only — it refuses to run when
    VOLEXTURN_ENV claims production, because handing out tokens from a shell
    would bypass every login control.
    """
    cfg = get_config()
    if cfg.is_production:
        _fail("در production مجاز نیست")
        return 2
    from .auth import issue_session
    db = _db()
    _ensure_schema(db, why="sessions live in the schema")
    conn = db.connect()
    try:
        row = db.query_one(conn, "SELECT id FROM users WHERE username = ?", (args.username,))
        if row is None:
            _fail("کاربر پیدا نشد")
            return 1
        sess = issue_session(db, conn, int(row["id"]), device="cli", user_agent="volexturn-cli")
        conn.commit()
        # A bare token on stdout by default, so `TOKEN=$(python -m backend token
        # alice)` pastes straight into an Authorization header; --json for the
        # document with the expiry and id.
        if args.json:
            _echo({"user_id": int(row["id"]), "token": sess.token,
                   "expires_at": sess.expires_at.isoformat(sep=" ")}, True)
        else:
            _say(sess.token)
        return 0
    finally:
        db.close(conn)


# --------------------------------------------------------------------------
# introspection
# --------------------------------------------------------------------------
def cmd_routes(args: argparse.Namespace) -> int:
    from .app import create_app
    app = create_app(skip_migrations=not args.migrate)
    rules = sorted(app.url_map.iter_rules(), key=lambda r: (str(r.rule), sorted(r.methods or []))[0])
    api, legacy, other = [], [], []
    for r in rules:
        methods = sorted(m for m in (r.methods or set()) if m not in {"HEAD", "OPTIONS"})
        line = f"{','.join(methods) or 'GET':<12} {str(r.rule):<52} → {r.endpoint}"
        if str(r.rule).startswith("/api/"):
            api.append(line)
        elif str(r.endpoint).startswith("legacy"):
            legacy.append(line)
        else:
            other.append(line)
    if args.filter:
        needle = args.filter.lower()
        api = [l for l in api if needle in l.lower()]
        legacy = [l for l in legacy if needle in l.lower()]
        other = [l for l in other if needle in l.lower()]
    print(f"\n🧩 canonical /api surface — {len(api)} routes")
    print("\n".join(api))
    print(f"\n🕰  legacy compatibility — {len(legacy)} routes")
    print("\n".join(legacy))
    print(f"\n📦 other — {len(other)} routes")
    print("\n".join(other))
    print()
    return 0


def cmd_janitor(args: argparse.Namespace) -> int:
    from .app import create_app
    from .janitor import run_once_for_tests
    app = create_app(start_janitor=False)
    stats = run_once_for_tests(app, app.extensions["volexturn_db"], app.extensions["volexturn_storage"])
    _echo({"tick": stats}, args.json)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """
    Find uploads that no database row references.

    Default is dry-run: deleting user media on a typo would be worse than keeping
    a few orphaned files. Anything newer than `--older-than` hours is skipped,
    because a file can be saved before the row that points at it is committed.
    """
    from .storage import get_storage
    from .uploads import referenced_keys
    db = _db()
    _ensure_schema(db, why="sweep reads upload rows")
    conn = db.connect()
    try:
        refs = referenced_keys(db, conn)
    finally:
        db.close(conn)
    storage = get_storage()
    orphans: list[dict[str, Any]] = []
    for category in ("profiles", "chat", "stories", "posts", "games", "rooms"):
        try:
            keys = storage.list_keys(category)
        except OSError:
            keys = []
        for key in keys:
            if f"{category}/{key}" in refs:
                continue
            try:
                info = storage.stat(category, key)
            except OSError:
                continue
            if info.get("age_seconds", 0) < args.older_than * 3600:
                continue
            orphans.append({"category": category, "key": key, "size": info.get("size", 0),
                            "age_hours": round(info.get("age_seconds", 0) / 3600, 1)})
    total = sum(int(o["size"]) for o in orphans)
    _echo({"referenced": len(refs), "orphans": orphans, "bytes": total,
           "dry_run": not args.delete}, args.json)
    if not args.delete:
        print(f"  ℹ️  {len(orphans)} فایل یتیم · {_mb(total)} — با --delete حذف می‌شوند")
        return 0
    removed = 0
    for o in orphans:
        if storage.delete(o["category"], o["key"]):
            removed += 1
    print(f"  🧹 {removed} فایل یتیم حذف شد ({_mb(total)})")
    return 0


def _mb(nbytes: int) -> str:
    val = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024:
            return f"{val:.1f}{unit}"
        val /= 1024
    return f"{val:.1f}PB"


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    """
    Environment / data pre-flight.

    Every check is a fact we can verify locally, not advice: it is the quickest
    way to tell "the app is broken" from "the deployment is misconfigured".
    """
    import shutil
    from pathlib import Path

    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_json)
    problems: list[str] = []
    notes: dict[str, Any] = {}

    def report(title: str) -> None:
        _say(f"\n🩺 {title}")

    def bad(msg: str) -> None:
        problems.append(msg)
        _fail(msg)

    # ---- runtime ----
    report("Runtime")
    minor = sys.version_info
    if minor < (3, 10):
        bad(f"Python {major_minor(minor)} < 3.10 — type-union syntax and typing features are required")
    else:
        _ok(f"Python {major_minor(minor)}")
    for pkg in ("flask", "flask_socketio", "engineio", "socketio"):  # engineio/socketio are the real transports
        try:
            importlib.import_module(pkg)
            _ok(f"{pkg} {_ver(pkg)}")
        except ImportError as exc:
            bad(f"{pkg} قابل import نیست: {exc}")
    notes["async_drivers"] = _async_drivers()

    # ---- config ----
    report("Configuration")
    _ok(f"env={cfg.env} host={cfg.host} port={cfg.port} engine={cfg.db_engine}")
    if not cfg.secret_key:
        (bad if cfg.is_production else _warn)(
            "VOLEXTURN_SECRET_KEY تنظیم نشده — نشست‌ها بعد از restart باطل می‌شوند")
    elif len(cfg.secret_key) < 32:
        bad("VOLEXTURN_SECRET_KEY کوتاه‌تر از ۳۲ کاراکتر است")
    else:
        _ok("secret key present")
    if cfg.is_production and "*" in (cfg.allowed_hosts or ()):
        bad("VOLEXTURN_ALLOWED_HOSTS شامل '*' است؛ same-origin check را بی‌اثر می‌کند")
    if not cfg.trust_x_forwarded_for:
        _warn("trust_x_forwarded_for خاموش است: rate limit پشت proxy روی IP خود proxy می‌بندد")
    if cfg.max_page_size < cfg.default_page_size:
        bad(f"max_page_size ({cfg.max_page_size}) < default_page_size ({cfg.default_page_size})")

    # ---- database ----
    report("Database")
    try:
        db = _db()
        conn = db.connect()
    except Exception as exc:                                   # noqa: BLE001
        bad(f"اتصال برقرار نشد: {type(exc).__name__}: {exc}")
        return _finish(notes, problems, args)
    try:
        if cfg.db_engine == "sqlite":
            path = Path(cfg.db_file)
            _ok(f"sqlite file {path} ({'exists' if path.exists() else 'will be created'})")
            if path.exists():
                _ok(f"size {_mb(path.stat().st_size)}")
            writable_dir = path.parent if path.parent.exists() else Path.cwd()
            if not os.access(writable_dir, os.W_OK):
                bad(f"دایرکتوری {writable_dir} نوشتنی نیست")
        version = None
        if db.has_table(conn, "vx_schema_version"):
            version = db.scalar(conn, "SELECT MAX(version) FROM vx_schema_version")
        from .migrations import expected_version, pending_count
        pending = pending_count(db)
        if version is None:
            _warn("جدول vx_schema_version نیست — با `python -m backend migrate` بسازید")
        else:
            _ok(f"schema v{version} / expected v{expected_version()}")
        if pending:
            bad(f"{pending} مهاجرت در انتظار اجراست (python -m backend migrate)")
        tables = [r["name"] for r in db.query(conn, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")] \
            if cfg.db_engine == "sqlite" else \
            [r["table_name"] for r in db.query(conn,
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")]
        notes["tables"] = len(tables)
        _ok(f"{len(tables)} جدول")
        if not tables:
            bad("هیچ جدولی وجود ندارد")
        for required in ("users", "posts", "messages", "lan_hosts"):
            if required not in tables:
                bad(f"جدول حیاتی {required} غایب است")
        counts = {}
        for t in ("users", "posts", "messages", "lan_hosts", "game_rooms", "reports"):
            if t in tables:
                counts[t] = int(db.scalar(conn, f"SELECT COUNT(*) FROM {t}") or 0)
        notes["rows"] = counts
        _ok("row counts " + json.dumps(counts))

        # ---- the legacy default-password regression ----
        report("Legacy default credentials")
        if not db.has_table(conn, "users"):
            # the whole point of doctor is that it runs *before* you fix the
            # install, so an unmigrated database is a warning, not a traceback
            _warn("جدول users هنوز ساخته نشده — اول `python -m backend migrate`")
        else:
            admins = list(db.query(conn, "SELECT id, username, password FROM users "
                                         "WHERE role = 'admin' LIMIT 25"))
            if not admins:
                _warn("هیچ ادمینی وجود ندارد — `python -m backend create-admin`")
            else:
                from .security import verify_password
                weak = [r["username"] for r in admins if verify_password("admin123", r["password"])]
                if weak:
                    bad(f"ادمین با رمز پیش‌فرض قدیمی: {', '.join(weak)} — فوراً عوض کنید")
                else:
                    _ok(f"{len(admins)} ادمین، هیچ‌کدام با رمز پیش‌فرض admin123 نیستند")
        conn.commit()
    finally:
        db.close(conn)

    # ---- storage ----
    report("Storage")
    root = Path(cfg.uploads_root)
    if not root.exists():
        _warn(f"{root} وجود ندارد (در اولین آپلود ساخته می‌شود)")
    elif not os.access(root, os.W_OK):
        bad(f"{root} نوشتنی نیست")
    else:
        _ok(f"{root} نوشتنی است")
        free = shutil.disk_usage(root).free
        if free < 200 * 1024 * 1024:
            _warn(f"فضای دیسک کم است: {_mb(free)} — آپلودها به‌زودی رد می‌شوند")
        else:
            _ok(f"{_mb(free)} فضای آزاد")
    notes["uploads"] = {"max_image_mb": cfg.max_image_mb, "max_video_mb": cfg.max_video_mb,
                        "max_file_mb": cfg.max_file_mb, "max_avatar_mb": cfg.max_avatar_mb}
    _ok(f"سقف آپلود: img {cfg.max_image_mb}MB / video {cfg.max_video_mb}MB / file {cfg.max_file_mb}MB")

    # ---- discovery policy ----
    report("LAN discovery / SSRF policy")
    if not cfg.discovery_enabled:
        _ok("discovery خاموش است (پیش‌فرض امن) — فقط heartbeat میزبان وضعیت را عوض می‌کند")
    elif not cfg.discovery_networks:
        bad("VOLEXTURN_DISCOVERY_ENABLED=1 ولی VOLEXTURN_DISCOVERY_NETWORKS خالی است: هیچ هدفی قابل پروب نیست")
    else:
        _ok(f"allowlist: {', '.join(cfg.discovery_networks)}")
        try:
            import ipaddress
            for entry in cfg.discovery_networks:
                net = ipaddress.ip_network(entry.strip(), strict=False)
                if not net.is_private:
                    bad(f"{entry} یک شبکهٔ خصوصی نیست؛ پروب آدرس‌های عمومی ریسک SSRF دارد")
        except ValueError as exc:
            bad(f"CIDR نامعتبر: {exc}")

    # ---- realtime ----
    report("Realtime")
    try:
        importlib.import_module("flask_socketio")   # presence check
        _ok(f"flask-socketio {_ver('flask_socketio')} · drivers: {notes['async_drivers']}")
        if notes["async_drivers"] == []:
            _warn("هیچ async driver نصب نیست؛ threading mode کار می‌کند ولی gevent/eventlet سریع‌تر است")
    except ImportError as exc:
        bad(f"flask_socketio نیست: {exc}")

    return _finish(notes, problems, args)


def _ver(pkg: str) -> str:
    """
    The installed version of *pkg* as a string.

    Read from the distribution metadata rather than ``module.__version__``,
    which Flask deprecated and removes in 3.2 — a diagnostic that breaks
    because of a deprecation is worse than no diagnostic.
    """
    for name in (pkg, pkg.replace("_", "-")):
        try:
            import importlib.metadata as md
            return md.version(name)
        except Exception:  # noqa: BLE001 - no metadata for vendored/editable installs
            continue
    try:
        return str(getattr(importlib.import_module(pkg), "__version__", "?"))
    except Exception:  # noqa: BLE001
        return "?"


def major_minor(v) -> str:
    return f"{v.major}.{v.minor}.{v.micro}"


def _async_drivers() -> list[str]:
    """Which socket.io async transports are importable (threading is always available)."""
    found = []
    for name in ("gevent", "eventlet", "simple-websocket"):
        try:
            importlib.import_module(name)
            found.append(name)
        except ImportError:
            continue
    return found


def _finish(notes: dict[str, Any], problems: list[str], args: argparse.Namespace) -> int:
    if args.json:
        _echo({"ok": not problems, "problems": problems, **notes}, True)
        return 1 if problems else 0
    _say("")
    if problems:
        _say(f"❌ {len(problems)} مشکل پیدا شد")
        return 1
    _say("✅ همه‌چیز درست به نظر می‌رسد")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backend",
                                description="Volexturn server & maintenance tools")
    p.add_argument("--json", action="store_true", help="machine-readable output where supported")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="serve the app")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--reload", action="store_true")
    run.set_defaults(fn=cmd_run)

    mig = sub.add_parser("migrate", help="apply schema migrations")
    mig.add_argument("--force", action="store_true", help="re-run even if already stamped")
    mig.set_defaults(fn=cmd_migrate)

    adm = sub.add_parser("create-admin", help="create or promote an administrator")
    adm.add_argument("-u", "--username", required=True)
    adm.add_argument("-p", "--password", help="omit to be prompted (recommended)")
    adm.add_argument("-n", "--full-name")
    adm.set_defaults(fn=cmd_create_admin)

    pw = sub.add_parser("passwd", help="reset a user's password + revoke sessions")
    pw.add_argument("username")
    pw.add_argument("-p", "--password")
    pw.set_defaults(fn=cmd_passwd)

    tk = sub.add_parser("token", help="print a session token (dev only)")
    tk.add_argument("username")
    tk.add_argument("--json", action="store_true", help="print {user_id, token, expires_at}")
    tk.set_defaults(fn=cmd_token)

    rt = sub.add_parser("routes", help="list HTTP surface")
    rt.add_argument("--filter", help="substring filter on rule/endpoint")
    rt.add_argument("--migrate", action="store_true", help="run migrations first")
    rt.set_defaults(fn=cmd_routes)

    jn = sub.add_parser("janitor", help="run one housekeeping tick now")
    jn.set_defaults(fn=cmd_janitor)

    sw = sub.add_parser("sweep", help="find (or delete) unreferenced uploads")
    sw.add_argument("--delete", action="store_true")
    sw.add_argument("--older-than", type=float, default=24.0, dest="older_than",
                    help="ignore files newer than this many hours")
    sw.set_defaults(fn=cmd_sweep)

    dr = sub.add_parser("doctor", help="pre-flight checks")
    dr.add_argument("--json", action="store_true",
                    help="machine-readable {ok, problems, …} on stdout, for CI and health probes")
    dr.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_json)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        print("\n💡 مثلاً:  python -m backend doctor   |   python -m backend run")
        return 0
    global _QUIET
    # `--json` means "stdout is a machine stream": human lines are silenced,
    # errors keep going to stderr. The reset is for in-process callers (tests,
    # embedders) that would otherwise inherit a muted CLI.
    _QUIET = bool(getattr(args, "json", False))
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("\nمتوقف شد", file=sys.stderr)
        return 130
    except Exception as exc:                                    # noqa: BLE001
        log.exception("cli_failed", extra={"ctx": {"command": args.command}})
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("VOLEXTURN_CLI_TRACEBACK"):
            raise
        return 1
    finally:
        _QUIET = False


if __name__ == "__main__":                                       # pragma: no cover
    raise SystemExit(main())
