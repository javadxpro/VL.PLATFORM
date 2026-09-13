"""
Authentication and session lifecycle.

Identity is derived **only** from a server-side session row. The client never
gets to say who it is: request bodies containing `user_id`/`sender_id`/
`owner_id` are treated as hints to be checked, never as truth (spec §4, §25).

Session model
-------------
  * opaque 256-bit token, stored hashed-at-rest?  -> no: stored directly so the
    lookup is a single indexed equality probe and per-token revocation is
    cheap. The DB file is the trust boundary; a token is already equivalent to
    a password for the session's lifetime, and it expires.
  * absolute expiry (`session_ttl_days`) + idle expiry via `last_used_at`
  * rotation on use when a session has been alive past `session_idle_rotate_days`
  * revocation: logout (current), logout-all, admin revoke, password change
  * capped concurrent sessions per user (`session_max_per_user`)

Two transports, one resolver:
  * `Authorization: Bearer <t>`   — the SPA + any API client
  * `vx_session` cookie           — needed so <img> and <video> requests can
    fetch private media without putting the token in a URL
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable

from flask import Response, current_app, g, has_request_context, request
from .config import get_config
from .db import Database, Row
from .errors import Forbidden, Locked, Unauthorized
from .log import get_logger
from .security import check_password_strength, hash_password, new_session_token

log = get_logger("auth")

COOKIE_NAME = "vx_session"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip().replace("T", " ").split(".")[0].split("+")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def is_past(value: Any) -> bool:
    """True when a DB timestamp string/datetime lies in the past."""
    moment = _iso(value)
    return moment is not None and moment < _now()


def to_db_ts(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# session rows
# --------------------------------------------------------------------------
@dataclass
class SessionInfo:
    token: str
    expires_at: dt.datetime
    session_id: int


def issue_session(db: Database, conn, user_id: int, *,
                  device: str | None = None, user_agent: str | None = None,
                  ip: str | None = None) -> SessionInfo:
    """Create a session, enforce the per-user cap, return the token."""
    cfg = get_config()
    token = new_session_token()
    expires = _now() + dt.timedelta(days=cfg.session_ttl_days)
    db.insert(conn, """
        INSERT INTO sessions (token, user_id, expires_at, device, user_agent, ip, last_used_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (token, user_id, to_db_ts(expires), (device or "")[:64],
               (user_agent or "")[:200], (ip or "")[:64], to_db_ts(_now())))
    # Trim oldest beyond the cap. Subquery keeps this portable.
    keep = cfg.session_max_per_user
    db.execute(conn, f"""
        DELETE FROM sessions WHERE user_id = ? AND id NOT IN (
            SELECT id FROM sessions WHERE user_id = ?
            ORDER BY (revoked_at IS NULL) DESC, created_at DESC LIMIT {int(keep)}
        )""", (user_id, user_id)).close()
    return SessionInfo(token=token, expires_at=expires, session_id=0)


def resolve_session(db: Database, conn, token: str) -> Row | None:
    """Join session -> user, honouring expiry, revocation and account status."""
    if not token or len(token) > 128:
        return None
    return db.query_one(conn, """
        SELECT u.*, s.id AS _session_id, s.token AS _token,
               s.expires_at AS _expires_at, s.revoked_at AS _revoked_at,
               s.last_used_at AS _last_used_at, s.created_at AS _session_created,
               s.device AS _device
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token = ?""", (token,))


def session_is_valid(row: Row | None) -> bool:
    if not row:
        return False
    cfg = get_config()
    if row.get("_revoked_at"):
        return False
    exp = _iso(row.get("_expires_at"))
    if exp is not None and exp < _now():
        return False
    if int(row.get("is_banned") or 0) == 1 or (row.get("status") or "active") in {"banned", "deleted"}:
        return False
    # Idle expiry: a token used long after its TTL must die even if the DB row
    # was refreshed by the sliding window.
    last = _iso(row.get("_last_used_at")) or _iso(row.get("_session_created"))
    if last is not None and (_now() - last) > dt.timedelta(days=max(cfg.session_ttl_days, 1)):
        return False
    return True


def touch_session(db: Database, conn, session_id: int) -> str | None:
    """Update `last_used_at`; rotate the token when it has aged out."""
    cfg = get_config()
    row = db.query_one(conn, "SELECT id, token, created_at FROM sessions WHERE id = ?", (session_id,))
    if not row:
        return None
    db.execute(conn, f"UPDATE sessions SET last_used_at = {db.now_sql()} WHERE id = ?",
               (session_id,)).close()
    created = _iso(row.get("created_at"))
    if created and (_now() - created) > dt.timedelta(days=cfg.session_idle_rotate_days):
        new_token = new_session_token()
        db.execute(conn, "UPDATE sessions SET token = ?, created_at = ?, expires_at = ? WHERE id = ?",
                   (new_token, to_db_ts(_now()),
                    to_db_ts(_now() + dt.timedelta(days=cfg.session_ttl_days)), session_id)).close()
        log.info("session_rotated", extra={"ctx": {"session_id": session_id}})
        return new_token
    return None


def revoke_session(db: Database, conn, session_id: int, *, reason: str = "logout") -> None:
    db.execute(conn,
               f"UPDATE sessions SET revoked_at = {db.now_sql()}, revoked_reason = ? WHERE id = ?",
               (reason[:40], session_id)).close()


def revoke_user_sessions(db: Database, conn, user_id: int, *, except_id: int | None = None,
                         reason: str = "revoked") -> int:
    sql = "UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE user_id = ?"
    params: list[Any] = [to_db_ts(_now()), reason[:40], user_id]
    if except_id:
        sql += " AND id != ?"
        params.append(except_id)
    cur = db.execute(conn, sql, params)
    try:
        return cur.rowcount
    finally:
        cur.close()


def purge_expired_sessions(db: Database, conn) -> int:
    """Hard delete of rows that are expired or revoked past a grace window."""
    # keep revoked/expired rows for a day (audit), then drop them
    cutoff = db.hours_ahead_sql(-24) if db.engine == "sqlite" else "(now() - interval '24 hours')"
    cur = db.execute(conn, f"""
        DELETE FROM sessions
        WHERE (expires_at IS NOT NULL AND expires_at < {cutoff})
           OR (revoked_at IS NOT NULL AND revoked_at < {cutoff})""")
    try:
        return cur.rowcount
    finally:
        cur.close()


# --------------------------------------------------------------------------
# password helpers
# --------------------------------------------------------------------------
def set_password(db: Database, conn, user_id: int, new_password: str, *,
                 revoke_others: bool = True, keep_session_id: int | None = None) -> int:
    db.execute(conn,
               f"""UPDATE users SET password = ?, password_changed_at = {db.now_sql()},
                          must_change_password = 0 WHERE id = ?""",
               (hash_password(new_password), user_id)).close()
    if revoke_others:
        return revoke_user_sessions(db, conn, user_id, except_id=keep_session_id,
                                    reason="password_changed")
    return 0


def validate_new_password(password: str, username: str) -> None:
    ok, message, _ = check_password_strength(password, username=username)
    if not ok:
        from .errors import ValidationFailed
        raise ValidationFailed(message or "رمز عبور به اندازه کافی قوی نیست",
                               code="WEAK_PASSWORD")


def _retire_dead_session(db: Database, conn, row: Row) -> None:
    """
    Mark an expired/invalid session revoked.

    Revoking rather than deleting keeps `sessions` usable as a login history
    (useful for "where am I logged in?") while making the dead token
    unresolvable, and the row is bounded by `session_max_per_user` + the
    janitor purge.
    """
    sid = row.get("_session_id")
    if not sid:
        return
    db.execute(conn, f"""UPDATE sessions
                         SET revoked_at = COALESCE(revoked_at, {db.now_sql()}),
                             revoked_reason = COALESCE(revoked_reason, 'expired')
                         WHERE id = ?""", (sid,)).close()


# --------------------------------------------------------------------------
# request plumbing
# --------------------------------------------------------------------------
def auth_token_from_request() -> str | None:
    """Bearer header first, then the session cookie (for media requests)."""
    if not has_request_context():
        return None
    header = request.headers.get("Authorization", "")
    if header:
        m = re.match(r"^\s*(?:Bearer|Token)\s+(\S+)\s*$", header, re.I)
        if m:
            return m.group(1)[:128]
        if len(header) <= 128 and " " not in header.strip():
            return header.strip()          # tolerate a bare token
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie[:128] if cookie else None


def set_session_cookie(resp: Response, token: str, expires: dt.datetime) -> Response:
    cfg = get_config()
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=int((expires - _now()).total_seconds()),
        path="/", httponly=True, samesite="Lax", secure=cfg.is_production,
    )
    return resp


def clear_session_cookie(resp: Response) -> Response:
    resp.delete_cookie(COOKIE_NAME, path="/", samesite="Lax",
                       secure=get_config().is_production)
    return resp


def authenticate(db: Database | None = None, *, conn=None, token: str | None = None) -> Row | None:
    """
    Resolve the caller from the session token.

    Returns the joined user+session row, or None. Raises Locked for a banned
    account so the client gets an actionable message instead of 'log in again'.

    The per-request cache is keyed on the *request object*, never on a boolean
    flag: `flask.g` lives on the app context, and an app context can outlive one
    request (test clients, `with app.app_context()` around a batch of calls).
    A flag-based cache would then answer the next request with the previous
    caller's identity — including after that session has been revoked.
    """
    if has_request_context():
        cached = getattr(g, "_vx_identity", None)
        if cached is not None and cached[0] is request:
            user = cached[1]
            if user is not None and int(user.get("is_banned") or 0) == 1:
                raise Locked("حساب کاربری شما مسدود شده است.")
            return user

    db = db or current_app.extensions["volexturn_db"]
    token = token if token is not None else auth_token_from_request()
    if not token:
        return None
    own_conn = conn is None
    conn = conn or db.connect()
    try:
        row = resolve_session(db, conn, token)
        if row is None:
            return None
        if not session_is_valid(row):
            _retire_dead_session(db, conn, row)
            return None
        if int(row.get("is_banned") or 0) == 1 or (row.get("status") or "active") == "banned":
            raise Locked("حساب کاربری شما مسدود شده است — با مدیر تماس بگیرید.")
        new_token = touch_session(db, conn, row["_session_id"])
        if new_token:
            row["_token"] = new_token
            if has_request_context():
                g._vx_rotated_token = new_token
        conn.commit()
        row = dict(row)
        out = Row(row)
        if has_request_context():
            g.user = out
            g.user_id = out["id"]
            g.session_id = out.get("_session_id")
            g._vx_identity = (request, out)
        return out
    finally:
        if own_conn:
            db.close(conn)


def auth_required(fn: Callable) -> Callable:
    """Decorator: 401 unless `g.user` resolved. Keeps the legacy message shape."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        user = authenticate()
        if user is None:
            raise Unauthorized()
        return fn(*args, **kwargs)

    wrapper.__wrapped_view = fn            # type: ignore[attr-defined]
    wrapper._vx_auth = True                # type: ignore[attr-defined]
    return wrapper


def admin_required(fn: Callable) -> Callable:
    from functools import wraps

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        user = authenticate()
        if user is None:
            raise Unauthorized()
        if (user.get("role") or "user") != "admin":
            raise Forbidden("دسترسی مدیریتی لازم است")
        return fn(*args, **kwargs)

    wrapper.__wrapped_view = fn            # type: ignore[attr-defined]
    wrapper._vx_auth = True                # type: ignore[attr-defined]
    return wrapper


def optional_auth(fn: Callable) -> Callable:
    from functools import wraps

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            authenticate()
        except Locked:
            raise
        return fn(*args, **kwargs)

    return wrapper


def as_user_dict(row: Row | None) -> dict[str, Any]:
    """Public projection of a user row — never contains a password or token."""
    if row is None:
        return {}
    d = dict(row)
    for drop in ("password", "_token", "_session_id", "_revoked_at", "_last_used_at",
                 "_session_created", "_device", "_expires_at", "must_change_password"):
        d.pop(drop, None)
    d["id"] = int(d["id"])
    return d


def require_owner(row: Row | None, user_id: int, *, message: str = "فقط مالک می‌تواند این کار را انجام دهد") -> Row:
    """Ownership guard shared by posts/stories/servers/rooms."""
    if row is None:
        from .errors import NotFound
        raise NotFound()
    if int(row.get("user_id") or row.get("host_id") or row.get("creator_id") or -1) != int(user_id):
        raise Forbidden(message)
    return row


def is_admin(user: Row | dict | None) -> bool:
    return bool(user) and (user.get("role") or "user") == "admin"
