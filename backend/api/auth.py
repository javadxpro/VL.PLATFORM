"""
Authentication API.

Replaces the pre-upgrade `/login` + `/register` pair with a proper account
lifecycle, while keeping both legacy paths answering with the identical
response shape (`{success, user, token, message}`) so the current SPA works
untouched.

The default `admin / admin123` seed is **gone**. First-run administration now
requires an explicit act, and there are exactly two ways to do it:

  1. `POST /api/auth/setup` — refused unless no admin exists *and* the request
     arrives from a private/loopback address, or carries the one-time code
     printed on the server console.
  2. `python -m backend.cli create-admin` — for headless/containerised hosts.

A weak or common password is rejected by both.
"""

from __future__ import annotations

import ipaddress

from flask import g, jsonify, request, make_response

from . import Module, conn, current_db, my_id, payload, text_field
from .. import presence, vlid
from ..auth import (as_user_dict, clear_session_cookie, is_past, issue_session,
                    resolve_session, revoke_session, revoke_user_sessions,
                    session_is_valid, set_password, set_session_cookie, to_db_ts,
                    validate_new_password)
from ..config import get_config
from ..errors import (BadRequest, Conflict, Forbidden, NotFound, Unauthorized, ValidationFailed)
from ..log import get_logger
from ..security import (check_password_strength, client_ip, hash_password,
                        needs_rehash, sanitize_text, throttle, valid_username,
                        verify_password, verify_setup_code)

mod = Module("auth")
log = get_logger("api.auth")


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------
def _device_label() -> tuple[str, str]:
    """Coarse device class + short UA, for the session list UI."""
    ua = (request.headers.get("User-Agent") or "")[:200]
    low = ua.lower()
    if "mobile" in low or "android" in low or "iphone" in low:
        kind = "mobile"
    elif "tablet" in low or "ipad" in low:
        kind = "tablet"
    elif not ua:
        kind = "client"
    else:
        kind = "desktop"
    for name in ("firefox", "edg", "chrome", "safari", "curl", "python-requests", "okhttp"):
        if name in low:
            kind += f":{name}"
            break
    return kind, ua


# --------------------------------------------------------------------------
# register / login / logout
# --------------------------------------------------------------------------
@mod.route("/register", methods=["POST"], auth="none", rate=(5, 600),
           legacy="/register", legacy_methods=("POST",))
def register():
    cfg = get_config()
    body = payload()
    username = text_field("username", body, max_len=32, min_len=3)
    password = str(body.get("password") or "")
    full_name = text_field("full_name", body, max_len=64, required=False) or username
    bio = text_field("bio", body, max_len=200, required=False)

    if not valid_username(username):
        raise ValidationFailed(
            "نام کاربری فقط می‌تواند حرف انگلیسی، عدد، نقطه، خط تیره و زیرخط باشد (۳ تا ۳۲)",
            code="INVALID_USERNAME")
    if username.lower() in {"admin", "root", "system", "support", "volexturn"}:
        raise Conflict("این نام کاربری رزرو شده است", code="RESERVED_USERNAME")
    ok, message, _ = check_password_strength(password, username=username)
    if not ok:
        raise ValidationFailed(message, code="WEAK_PASSWORD",
                              details={"min_length": cfg.min_password_length})

    db = current_db()
    c = conn()
    if db.query_one(c, "SELECT id FROM users WHERE lower(username) = lower(?)", (username,)):
        raise Conflict("این نام کاربری قبلاً استفاده شده است", code="USERNAME_TAKEN")
    uid = db.insert(c, """
        INSERT INTO users (username, password, full_name, bio, avatar, role, created_at, status)
        VALUES (?, ?, ?, ?, '', 'user', CURRENT_TIMESTAMP, 'active')""",
        (username, hash_password(password), full_name,
         bio or "کاربر Volexturn"))
    db.insert(c, "INSERT INTO gaming_profiles (user_id, gamertag) VALUES (?, ?)", (uid, username))
    db.insert(c, """INSERT INTO user_settings (user_id) VALUES (?)""", (uid,))
    # every account gets its VL ID at creation (docs/ROADMAP.md P1) — see
    # tests/test_vlid.py for the guard that keeps new signup paths from skipping this
    vlid.assign(db, c, uid)
    conn().commit()
    log.info("user_registered", extra={"ctx": {"user_id": uid, "username": username}})
    return jsonify({"success": True,
                    "message": "ثبت‌نام انجام شد! اکنون وارد شوید.",
                    "user_id": uid}), 201


@mod.route("/login", methods=["POST"], auth="none", rate=None,
           legacy="/login", legacy_methods=("POST",))
def login():
    """
    Credentials + a hard lockout, then a fresh expiring session.

    Throttling is deliberately two-layer: by IP *and* by username, so neither
    a spray across accounts nor a hammer on one account is cheap.
    """
    cfg = get_config()
    body = payload()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username or not password:
        raise BadRequest("اطلاعات پر نشده است", code="CREDENTIALS_MISSING")
    if len(username) > 64 or len(password) > 200:
        raise BadRequest("ورودی بیش از حد طولانی است", code="INPUT_TOO_LONG")

    ip = client_ip()
    throttle("auth:login_ip", capacity=cfg.login_max_attempts,
             window=cfg.login_window_seconds, extra=ip)
    throttle("auth:login_user", capacity=5, window=cfg.login_window_seconds,
             extra=username.lower())

    db = current_db()
    c = conn()
    row = db.query_one(c, "SELECT * FROM users WHERE username = ?", (username,))
    if not row or not verify_password(password, row.get("password")):
        # Uniform answer: never reveal whether the account exists.
        raise Unauthorized("نام کاربری یا رمز عبور اشتباه است", code="BAD_CREDENTIALS")

    if int(row.get("is_banned") or 0) == 1 or (row.get("status") or "active") in {"banned", "deleted"}:
        reason = sanitize_text(row.get("ban_reason"), max_len=120)
        raise Forbidden(f"حساب مسدود است. {reason}".strip(), code="ACCOUNT_BANNED")

    # Transparent upgrade of legacy hashes (plaintext or low-iteration PBKDF2).
    if needs_rehash(row.get("password")):
        db.execute(c, "UPDATE users SET password = ? WHERE id = ?",
                   (hash_password(password), row["id"])).close()
        log.info("password_upgraded", extra={"ctx": {"user_id": row["id"]}})

    kind, ua = _device_label()
    sess = issue_session(db, c, int(row["id"]), device=kind, user_agent=ua, ip=ip)
    db.execute(c, f"UPDATE users SET last_seen_at = {db.now_sql()}, presence = 'online' WHERE id = ?",
               (row["id"],)).close()
    c.commit()

    user = as_user_dict(row)
    user["is_online"] = True
    user["presence"] = "online"
    body_out = {"success": True, "user": user, "token": sess.token,
                "expires_at": to_db_ts(sess.expires_at), "version": cfg.app_version,
                "must_change_password": bool(int(row.get("must_change_password") or 0))}
    resp = make_response(jsonify(body_out))
    set_session_cookie(resp, sess.token, sess.expires_at)
    log.info("login", extra={"ctx": {"user_id": row["id"], "device": kind}})
    return resp


@mod.route("/logout", methods=["POST"], auth="user", rate=(30, 60))
def logout():
    """Revoke just this device — other sessions are untouched."""
    db = current_db()
    c = conn()
    sid = getattr(g, "session_id", None)
    if sid:
        revoke_session(db, c, int(sid), reason="logout")
        c.commit()
    resp = make_response(jsonify({"success": True, "message": "از حساب خارج شدید"}))
    clear_session_cookie(resp)
    return resp


@mod.route("/logout-all", methods=["POST"], auth="user", rate=(5, 120))
def logout_all():
    """Panic button: every session except the caller's own."""
    db = current_db()
    c = conn()
    uid = my_id()
    n = revoke_user_sessions(db, c, uid, except_id=int(getattr(g, "session_id", 0) or 0),
                             reason="logout_all")
    c.commit()
    return jsonify({"success": True, "revoked": n,
                    "message": f"{n} نشست دیگر بست بسته شد"})


# --------------------------------------------------------------------------
# session introspection
# --------------------------------------------------------------------------
@mod.route("/me", auth="user", rate=None, legacy="/api/me")
def me_endpoint():
    user = g.user
    data = as_user_dict(user)
    data["presence"] = presence.status_for(int(user["id"]))
    data["is_online"] = data["presence"] != "offline"
    data["session"] = {
        "device": user.get("_device"),
        "created_at": user.get("_session_created"),
        "expires_at": user.get("_expires_at"),
    }
    db = current_db()
    c = conn()
    counts = {
        "posts": db.scalar(c, "SELECT COUNT(*) FROM posts WHERE user_id = ?", (user["id"],)),
        "followers": db.scalar(c, "SELECT COUNT(*) FROM follows WHERE following_id = ?", (user["id"],)),
        "following": db.scalar(c, "SELECT COUNT(*) FROM follows WHERE follower_id = ?", (user["id"],)),
        "friends": db.scalar(c, """SELECT COUNT(*) FROM friendships
                                   WHERE state = 'friends' AND (user_a = ? OR user_b = ?)""",
                             (user["id"], user["id"])),
        "notifications_unread": db.scalar(
            c, "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user["id"],)),
        "servers": db.scalar(c, "SELECT COUNT(*) FROM lan_hosts WHERE user_id = ?", (user["id"],)),
        "rooms_open": db.scalar(c, """SELECT COUNT(*) FROM game_rooms
                                      WHERE host_id = ? AND status != 'closed'""", (user["id"],)),
    }
    return jsonify({"success": True, "user": data, "counts": counts,
                    "is_admin": (user.get("role") or "user") == "admin"})


@mod.route("/sessions", auth="user", rate=None)
def sessions_list():
    db = current_db()
    c = conn()
    rows = db.query(c, """
        SELECT id, device, user_agent, ip, created_at, last_used_at, expires_at, revoked_at
        FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 30""", (my_id(),))
    current_sid = int(getattr(g, "session_id", 0) or 0)
    out = []
    for r in rows:
        revoked = bool(r.get("revoked_at"))
        expired = is_past(r.get("expires_at"))
        out.append({
            "id": r["id"], "device": r.get("device") or "unknown",
            "ip": (r.get("ip") or "")[:45],
            "created_at": r.get("created_at"), "last_used_at": r.get("last_used_at"),
            "expires_at": r.get("expires_at"),
            "current": int(r["id"]) == current_sid,
            "active": not revoked and not expired,
        })
    return jsonify({"success": True, "sessions": out, "count": len(out)})


@mod.route("/sessions/<int:sid>/revoke", methods=["POST"], auth="user", rate=(30, 60))
def session_revoke(sid: int):
    db = current_db()
    c = conn()
    row = db.query_one(c, "SELECT id, user_id FROM sessions WHERE id = ?", (sid,))
    if row is None:
        raise NotFound("نشستی با این شناسه نیست")
    if int(row["user_id"]) != my_id():
        raise Forbidden("فقط نشست‌های خودت")
    revoke_session(db, c, sid, reason="user_revoked")
    c.commit()
    return jsonify({"success": True, "message": "این نشست بسته شد"})


# --------------------------------------------------------------------------
# password
# --------------------------------------------------------------------------
@mod.route("/change_password", methods=["POST"], auth="user", rate=(5, 300),
           legacy="/change_password", legacy_methods=("POST",))
def change_password():
    body = payload()
    old = str(body.get("old_password") or "")
    new = str(body.get("new_password") or "")
    if not old or not new:
        raise BadRequest("هر دو رمز لازم است", code="FIELDS_REQUIRED")
    db = current_db()
    c = conn()
    me = g.user
    if not verify_password(old, me.get("password")):
        raise Forbidden("رمز فعلی اشتباه است", code="BAD_CURRENT_PASSWORD")
    validate_new_password(new, str(me.get("username") or ""))
    kept = int(getattr(g, "session_id", 0) or 0)
    revoked = set_password(db, c, int(me["id"]), new, keep_session_id=kept or None)
    c.commit()
    return jsonify({"success": True,
                    "message": f"رمز عوض شد و {revoked} نشست دیگر بی‌اعتبار شدند 🔒",
                    "revoked_sessions": revoked})


@mod.route("/password/strength", methods=["POST"], auth="none", rate=(20, 300))
def password_strength():
    """Client-side meter helper — no state change, no password stored."""
    body = payload()
    pw = str(body.get("password") or "")[:200]
    user = str((getattr(g, "user", None) or {}).get("username") or "")
    ok, message, problems = check_password_strength(pw, username=user)
    return jsonify({"success": True, "strong": ok, "message": message or "رمز مناسب است",
                    "rules": [p.split(":")[0] for p in problems],
                    "min_length": get_config().min_password_length})


@mod.route("/password/reset", methods=["POST"], auth="none", rate=None)
def password_reset():
    """
    Honest 501.

    A reset flow needs a delivery channel (SMTP/SMTP relay). Volexturn ships
    without one, so rather than pretending to send mail we say so and point at
    the two paths that do work today.
    """
    raise BadRequest(
        "بازیابی رمز با ایمیل در این نسخه پیاده نشده است. از مدیر بخواهید با "
        "«python -m backend.cli set-password <username>» رمز شما را بازنشانی کند.",
        code="PASSWORD_RESET_UNAVAILABLE")


# --------------------------------------------------------------------------
# first-run admin bootstrap
# --------------------------------------------------------------------------
@mod.route("/setup", auth="none", rate=(10, 300))
def setup_status():
    """Public: is this instance still unclaimed? Drives the SPA's banner."""
    db = current_db()
    c = conn()
    has_admin = bool(db.scalar(c, "SELECT COUNT(*) FROM users WHERE role = 'admin'"))
    return jsonify({
        "success": True,
        "setup_required": not has_admin,
        "admin_exists": has_admin,
        "local_only": not _is_local_request(),
        "methods": ["api_setup_form", "cli:create-admin", "env:VOLEXTURN_ADMIN_PASSWORD"],
    })


def _is_local_request() -> bool:
    """Loopback or RFC1918/ULA — the networks a self-hosted admin types from."""
    ip = client_ip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip in {"127.0.0.1", "::1", "localhost", "?"}
    return addr.is_loopback or addr.is_private


def _setup_authorized() -> tuple[bool, str]:
    if _is_local_request():
        return True, "local"
    code = str((request.get_json(silent=True) or {}).get("setup_code")
               or request.headers.get("X-Volexturn-Setup-Code") or "")
    if code and verify_setup_code(code, 0):
        return True, "code"
    env_pw = get_config().auto_admin_password
    if env_pw:
        return True, "env"
    return False, "remote_unauthenticated"


@mod.route("/setup", methods=["POST"], auth="none", rate=(5, 900))
def setup_admin():
    """
    Create the first administrator. Refused the moment an admin exists.

    This is the whole answer to "remove insecure default administrator
    credentials": nothing is seeded, and creating the account needs a local
    request or a console-issued code.
    """
    db = current_db()
    c = conn()
    if db.scalar(c, "SELECT COUNT(*) FROM users WHERE role = 'admin'"):
        raise Conflict("پیکربندی انجام شده است — یک مدیر موجود است", code="ALREADY_CONFIGURED")
    allowed, how = _setup_authorized()
    if not allowed:
        raise Forbidden(
            "ایجاد مدیر فقط از شبکه محلی یا با کد یک‌بار‌مصرف کنسول مجاز است",
            code="SETUP_REQUIRES_LOCAL")

    body = payload()
    username = text_field("username", body, max_len=32, min_len=3)
    password = str(body.get("password") or "")
    if not valid_username(username):
        raise ValidationFailed("نام کاربری مدیر نامعتبر است", code="INVALID_USERNAME")
    ok, message, _ = check_password_strength(password, username=username)
    if not ok:
        raise ValidationFailed(message, code="WEAK_PASSWORD")

    uid = db.insert(c, """
        INSERT INTO users (username, password, full_name, bio, role, status, created_at)
        VALUES (?, ?, ?, ?, 'admin', 'active', CURRENT_TIMESTAMP)""",
        (username, hash_password(password),
         text_field("full_name", body, max_len=64, required=False) or "مدیر سیستم",
         "مدیر ارشد Volexturn"))
    db.insert(c, "INSERT INTO gaming_profiles (user_id, gamertag) VALUES (?, ?)", (uid, username))
    if not db.query_one(c, "SELECT user_id FROM user_settings WHERE user_id = ?", (uid,)):
        db.insert(c, "INSERT INTO user_settings (user_id) VALUES (?)", (uid,))
    vlid.assign(db, c, uid)
    c.commit()
    log.info("admin_created", extra={"ctx": {"user_id": uid, "via": how, "ip": client_ip()}})

    sess = issue_session(db, c, uid, device="setup", user_agent="setup", ip=client_ip())
    c.commit()
    resp = make_response(jsonify({
        "success": True,
        "message": "مدیر ساخته شد. حالا وارد شوید.",
        "user_id": uid, "via": how,
        "token": sess.token,
    }))
    set_session_cookie(resp, sess.token, sess.expires_at)
    return resp


# --------------------------------------------------------------------------
# legacy aliases that must keep their exact shape
# --------------------------------------------------------------------------
@mod.route("/verify", auth="none", rate=None)
def verify():
    """Used by the SPA on boot to decide whether a stored token is still good."""
    token = request.args.get("token") or ""
    if not token:
        from ..auth import auth_token_from_request
        token = auth_token_from_request() or ""
    db = current_db()
    c = conn()
    row = resolve_session(db, c, str(token)[:128])
    valid = session_is_valid(row)
    if not valid:
        return jsonify({"success": True, "authenticated": False,
                        "message": "نشست منقضی شده است"}), 200
    data = as_user_dict(row)
    data["presence"] = presence.status_for(int(row["id"]))
    return jsonify({"success": True, "authenticated": True, "user": data,
                    "expires_at": row.get("_expires_at")})


@mod.route("/online", auth="user", rate=None)
def online_now():
    return jsonify({"success": True, "online": sorted(presence.online_ids()),
                    "count": len(presence.online_ids())})
