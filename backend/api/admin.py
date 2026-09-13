"""
Admin: dashboard, user management, content and server oversight, audit log.

Every admin route in the legacy app is preserved *and* now actually requires an
admin (they were unauthenticated in `server.py`, docs/AUDIT.md §7 S1).
Destructive actions are audit-logged with the acting user id.
"""

from __future__ import annotations

import datetime as dt

from flask import jsonify, request

from . import (Module, bool_arg, conn, current_db, int_arg, my_id, payload, text_field)
from .. import discovery, presence
from ..config import get_config
from ..errors import BadRequest, Forbidden, NotFound
from ..log import get_logger
from ..security import hash_password, pagination_args, paginated, sanitize_text

mod = Module("admin")
log = get_logger("api.admin")

# Legacy aliases live at /api/admin/... too, so they reuse this blueprint's
# prefix; the shared LEGACY_BP only carries routes at the site root.
LEGACY = "/api/admin"

#: every value `lan_hosts.status` may hold
_SERVER_STATUSES = (*discovery.MANUAL_STATUSES, discovery.STATUS_UNKNOWN)


def _audit(db, c, action: str, *, target_type: str = "", target_id: int | None = None,
           note: str = "") -> None:
    if db.has_table(c, "audit_log"):
        db.execute(c, """INSERT INTO audit_log (actor_id, action, target_type, target_id, note)
                         VALUES (?, ?, ?, ?, ?)""",
                   (my_id(), action[:48], target_type[:24], target_id, note[:500])).close()
    log.info("admin_action", extra={"ctx": {"action": action, "target_type": target_type,
                                            "target_id": target_id, "admin_id": my_id()}})


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
def _dashboard() -> tuple:
    db, c = current_db(), conn()
    day_start = dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    totals = db.query_one(c, """
        SELECT (SELECT COUNT(*) FROM users) AS users,
               (SELECT COUNT(*) FROM posts WHERE deleted_at IS NULL) AS posts,
               (SELECT COUNT(*) FROM messages) AS messages,
               (SELECT COUNT(*) FROM groups WHERE deleted_at IS NULL) AS groups,
               (SELECT COUNT(*) FROM lan_hosts WHERE archived_at IS NULL) AS servers,
               (SELECT COUNT(*) FROM game_rooms
                 WHERE closed_at IS NULL AND status IN ('open','starting','ingame')) AS rooms,
               (SELECT COUNT(*) FROM games) AS games,
               (SELECT COUNT(*) FROM users WHERE role = 'admin') AS admins,
               (SELECT COUNT(*) FROM users WHERE COALESCE(is_banned,0) = 1) AS banned_users,
               (SELECT COUNT(*) FROM reports WHERE status = 'open') AS open_reports,
               (SELECT COUNT(*) FROM users WHERE created_at > ?) AS new_today
    """, (day_start.isoformat(sep=" "),)) or {}
    server_rows = db.query(c, """
        SELECT COALESCE(AVG(CAST(COALESCE(player_count,
                  (SELECT COUNT(*) FROM server_players sp WHERE sp.server_id = lan_hosts.id
                     AND sp.left_at IS NULL)) AS REAL)), 0) AS avg_players,
               COALESCE(AVG(latency_ms), 0) AS avg_latency,
               COALESCE(SUM(CASE WHEN status = 'online' THEN 1 ELSE 0 END), 0) AS online,
               COALESCE(SUM(CASE WHEN status = 'offline' THEN 1 ELSE 0 END), 0) AS offline,
               COALESCE(SUM(CASE WHEN status = 'full' THEN 1 ELSE 0 END), 0) AS full,
               COALESCE(SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END), 0) AS unknown,
               COALESCE(SUM(CASE WHEN heartbeat_fails > 0 THEN 1 ELSE 0 END), 0) AS failing
        FROM lan_hosts WHERE archived_at IS NULL""") or [{}]
    s = dict(server_rows[0])
    recent = db.query(c, f"""
        SELECT id, username, full_name, avatar, created_at FROM users
        WHERE created_at > {db.hours_ahead_sql(-72)} ORDER BY id DESC LIMIT 10""")
    top_games = db.query(c, """
        SELECT g.id, g.slug, g.name, g.icon,
               (SELECT COUNT(*) FROM user_games ug WHERE ug.game_id = g.id) AS owners,
               (SELECT COUNT(*) FROM lan_hosts h WHERE h.game_id = g.id
                  AND h.archived_at IS NULL) AS servers,
               (SELECT COUNT(*) FROM game_rooms r WHERE r.game_id = g.id
                  AND r.closed_at IS NULL) AS rooms
        FROM games g ORDER BY owners DESC, name ASC LIMIT 10""")
    return jsonify({
        "success": True,
        "stats": {**{k: int(v or 0) for k, v in (totals or {}).items()},
                  "avg_players_per_server": round(float(s.get("avg_players") or 0), 2),
                  "avg_latency_ms": round(float(s.get("avg_latency") or 0), 1),
                  "servers_online": int(s.get("online") or 0),
                  "servers_offline": int(s.get("offline") or 0),
                  "servers_full": int(s.get("full") or 0),
                  "servers_unknown": int(s.get("unknown") or 0),
                  "servers_failing_heartbeat": int(s.get("failing") or 0)},
        # the legacy panel reads these flat keys:
        "total_users": int(totals.get("users") or 0),
        "online_users": len(presence.online_ids()),
        "total_posts": int(totals.get("posts") or 0),
        "total_messages": int(totals.get("messages") or 0),
        "total_servers": int(totals.get("servers") or 0),
        "new_today": int(totals.get("new_today") or 0),
        "open_reports": int(totals.get("open_reports") or 0),
        "recent_users": [dict(r) for r in recent],
        "top_games": [dict(r) for r in top_games],
        "presence": presence.snapshot(),
        "discovery": discovery.health_check(),
    })


@mod.route("/dashboard", auth="admin", rate=None)
@mod.legacy(f"{LEGACY}/stats", methods=("GET",), auth="admin", rate=None)
def stats():
    return _dashboard()


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------
def _users() -> tuple:
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=25)
    where, params = ["1=1"], []
    q = sanitize_text(request.args.get("q"), max_len=48, strip_newlines=True)
    if q:
        # `users` has no email column (the legacy schema never had one), so the
        # admin search covers exactly what exists: handle, display name, id.
        # the alias is `u` in both queries below, so the column names have to
        # match it, and the numeric id goes through a placeholder like anything
        # else a user typed
        numeric = int(q) if q.isdigit() else -1
        where.append(f"(u.id = ? OR "
                     f"{db.ilike('u.username')} OR "
                     f"{db.ilike('u.full_name')})")
        params.extend([numeric, *db.ilike_params(q), *db.ilike_params(q)])
    if str(request.args.get("banned", "")).lower() in {"1", "true"}:
        where.append("COALESCE(u.is_banned,0) = 1")
    if str(request.args.get("admins", "")).lower() in {"1", "true"}:
        where.append("u.role = 'admin'")
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM users u WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT u.id, u.username, u.full_name, u.avatar, u.bio, u.role,
               COALESCE(u.is_banned,0) AS is_banned, u.ban_reason, u.status, u.created_at,
               u.last_seen_at, u.presence,
               (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id AND p.deleted_at IS NULL) AS posts,
               (SELECT COUNT(*) FROM lan_hosts h WHERE h.user_id = u.id) AS servers,
               (SELECT COUNT(*) FROM reports r WHERE r.target_type = 'user' AND r.target_id = u.id
                  AND r.status = 'open') AS open_reports
        FROM users u WHERE {clause}
        ORDER BY {'u.last_seen_at DESC' if not q else 'u.id ASC'}
        {db.limit_offset(limit, offset)}""", params)
    items = presence.stamp_many([dict(r) for r in rows])
    for it in items:
        it["is_online"] = presence.is_online(int(it["id"]))
        it["avatar_url"] = f"/files/profiles/{it['avatar']}" if it.get("avatar") else None
    return jsonify({"success": True, "users": items, **paginated(total, limit, offset, page)})


@mod.route("/users", auth="admin", rate=None)
@mod.legacy(f"{LEGACY}/users", methods=("GET",), auth="admin", rate=None)
def users():
    return _users()


@mod.route("/users/<int:uid>", auth="admin", rate=None)
def user_detail(uid: int):
    db, c = current_db(), conn()
    row = db.query_one(c, """SELECT u.id, u.username, u.full_name, u.avatar, u.bio, u.role,
                                    COALESCE(u.is_banned,0) AS is_banned, u.ban_reason, u.status,
                                    u.created_at, u.last_seen_at, u.online_visible
                             FROM users u WHERE u.id = ?""", (uid,))
    if row is None:
        raise NotFound("کاربر پیدا نشد")
    out = dict(row)
    out["avatar_url"] = f"/files/profiles/{out['avatar']}" if out.get("avatar") else None
    out["counts"] = {
        "posts": int(db.scalar(c, "SELECT COUNT(*) FROM posts WHERE user_id = ? AND deleted_at IS NULL", (uid,)) or 0),
        "comments": int(db.scalar(c, "SELECT COUNT(*) FROM post_comments WHERE user_id = ?", (uid,)) or 0),
        "messages": int(db.scalar(c, "SELECT COUNT(*) FROM messages WHERE sender_id = ?", (uid,)) or 0),
        "followers": int(db.scalar(c, "SELECT COUNT(*) FROM follows WHERE following_id = ?", (uid,)) or 0),
        "following": int(db.scalar(c, "SELECT COUNT(*) FROM follows WHERE follower_id = ?", (uid,)) or 0),
        "groups": int(db.scalar(c, """SELECT COUNT(*) FROM group_members WHERE user_id = ?""", (uid,)) or 0),
        "servers": int(db.scalar(c, "SELECT COUNT(*) FROM lan_hosts WHERE user_id = ?", (uid,)) or 0),
        "rooms": int(db.scalar(c, "SELECT COUNT(*) FROM game_rooms WHERE host_id = ?", (uid,)) or 0),
        "reports_against": int(db.scalar(c, """SELECT COUNT(*) FROM reports
                                                WHERE target_type = 'user' AND target_id = ?""", (uid,)) or 0),
    }
    out["is_online"] = presence.is_online(uid)
    out["sessions"] = [dict(r) for r in db.query(c, """
        SELECT id, device, created_at, expires_at, revoked_at FROM sessions
        WHERE user_id = ? ORDER BY id DESC LIMIT 10""", (uid,))]
    out["moderation"] = [dict(r) for r in db.query(c, """
        SELECT action, target_type, target_id, note, created_at FROM moderation_actions
        WHERE user_id = ? ORDER BY id DESC LIMIT 20""", (uid,))]
    return jsonify({"success": True, "user": out})


@mod.route("/users/<int:uid>/ban", methods=["POST"], auth="admin", rate=(30, 600))
def ban_user(uid: int):
    me = my_id()
    if uid == me:
        raise Forbidden("نمی‌توانید خودتان را بن کنید", code="SELF_BAN")
    reason = text_field("reason", payload(), max_len=300, required=False) or "تخلف"
    db, c = current_db(), conn()
    if db.query_one(c, "SELECT id, role FROM users WHERE id = ?", (uid,)) is None:
        raise NotFound("کاربر پیدا نشد")
    db.execute(c, f"""UPDATE users SET is_banned = 1, status = 'banned', ban_reason = ?,
                      banned_at = {db.now_sql()} WHERE id = ?""", (reason, uid)).close()
    from ..auth import revoke_user_sessions
    revoke_user_sessions(db, c, uid, reason="banned")
    _audit(db, c, "user_ban", target_type="user", target_id=uid, note=reason)
    c.commit()
    return jsonify({"success": True, "banned": True, "message": "کاربر بن شد"})


@mod.route("/users/<int:uid>/unban", methods=["POST"], auth="admin", rate=(30, 600))
def unban_user(uid: int):
    db, c = current_db(), conn()
    if db.query_one(c, "SELECT id FROM users WHERE id = ?", (uid,)) is None:
        raise NotFound("کاربر پیدا نشد")
    db.execute(c, """UPDATE users SET is_banned = 0, status = 'active', ban_reason = NULL,
                      banned_at = NULL WHERE id = ?""", (uid,)).close()
    _audit(db, c, "user_unban", target_type="user", target_id=uid)
    c.commit()
    return jsonify({"success": True, "banned": False, "message": "بن کاربر برداشته شد"})


@mod.legacy(f"{LEGACY}/delete_user/<int:uid>", methods=("POST", "DELETE"),
             auth="admin", rate=(10, 600))
def legacy_delete_user(uid: int):
    return _delete_user(uid)


@mod.route("/users/<int:uid>", methods=["DELETE"], auth="admin", rate=(10, 600))
def delete_user(uid: int):
    return _delete_user(uid)


def _delete_user(uid: int):
    """Hard delete, gated behind an explicit `confirm` phrase (spec §20 danger zone)."""
    me = my_id()
    if uid == me:
        raise Forbidden("حذف حساب خود از این پنل مجاز نیست", code="SELF_DELETE")
    body = payload()
    if sanitize_text(body.get("confirm"), max_len=32, strip_newlines=True).lower() != "delete":
        raise BadRequest("برای حذف دائم، confirm=delete بفرستید", code="CONFIRM_REQUIRED")
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, username, avatar FROM users WHERE id = ?", (uid,))
    if row is None:
        raise NotFound("کاربر پیدا نشد")
    counts = {}
    for table, col in (("posts", "user_id"), ("comments", "user_id"), ("messages", "sender_id"),
                       ("lan_hosts", "user_id"), ("stories", "user_id")):
        t = {"comments": "post_comments"}.get(table, table)
        if db.has_table(c, t):
            counts[table] = int(db.scalar(c, f"SELECT COUNT(*) FROM {t} WHERE {col} = ?", (uid,)) or 0)
    cascade = [t for t, n in counts.items() if n and bool_arg(f"cascade_{t}", False, src=body)]
    for table in cascade:
        t = {"comments": "post_comments"}.get(table, table)
        db.execute(c, f"DELETE FROM {t} WHERE {'sender_id' if t == 'messages' else 'user_id'} = ?", (uid,)).close()
    for sql in (
        "DELETE FROM follows WHERE follower_id = ? OR following_id = ?",
        "DELETE FROM friendships WHERE user_a = ? OR user_b = ?",
        "DELETE FROM user_blocks WHERE user_id = ? OR blocked_user_id = ?",
        "DELETE FROM group_members WHERE user_id = ?",
        "DELETE FROM server_follows WHERE user_id = ?",
        "DELETE FROM user_games WHERE user_id = ?",
        "DELETE FROM sessions WHERE user_id = ?",
        "DELETE FROM notifications WHERE user_id = ?",
    ):
        try:
            db.execute(c, sql, (uid, uid) if sql.count("?") == 2 else (uid,)).close()
        except Exception:
            c.rollback()
    db.execute(c, "DELETE FROM users WHERE id = ?", (uid,)).close()
    from ..uploads import delete_upload
    if row.get("avatar"):
        delete_upload("profiles", row["avatar"])
    _audit(db, c, "user_delete", target_type="user", target_id=uid,
           note=f"deleted={json_dumps(counts)} cascaded={','.join(cascade)}")
    c.commit()
    return jsonify({"success": True, "deleted": True, "removed": counts, "cascaded": cascade})


def json_dumps(d: dict) -> str:
    import json
    return json.dumps(d, separators=(",", ":"))


@mod.legacy(f"{LEGACY}/promote_user/<int:uid>", methods=("POST",), auth="admin", rate=(10, 600))
def legacy_promote(uid: int):
    return set_admin(uid, True)


@mod.legacy(f"{LEGACY}/demote_user/<int:uid>", methods=("POST",), auth="admin", rate=(10, 600))
def legacy_demote(uid: int):
    return set_admin(uid, False)


@mod.route("/users/<int:uid>/admin", methods=["POST"], auth="admin", rate=(10, 600))
def set_admin_route(uid: int):
    return set_admin(uid, bool_arg("value", True, src=payload()))


def set_admin(uid: int, value: bool) -> tuple:
    if uid == my_id() and not value:
        raise Forbidden("نمی‌توانید دسترسی خودتان را بردارید", code="SELF_DEMOTE")
    db, c = current_db(), conn()
    if db.query_one(c, "SELECT id FROM users WHERE id = ?", (uid,)) is None:
        raise NotFound("کاربر پیدا نشد")
    # `role` is the single source of truth for admin-ness: the SPA reads
    # `currentUser.role`, so a parallel boolean would drift the moment one side
    # was updated.
    db.execute(c, "UPDATE users SET role = ? WHERE id = ?",
               ("admin" if value else "user", uid)).close()
    _audit(db, c, "grant_admin" if value else "revoke_admin", target_type="user", target_id=uid)
    c.commit()
    return jsonify({"success": True, "is_admin": bool(value), "role": "admin" if value else "user"})


@mod.route("/users/<int:uid>/password", methods=["POST"], auth="admin", rate=(10, 600))
def reset_password(uid: int):
    """
    Admin password reset.

    Sets `must_change_password` so the user is forced to pick their own on next
    login; the temporary value itself is returned once and never stored in plain
    text anywhere else.
    """
    body = payload()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, username FROM users WHERE id = ?", (uid,))
    if row is None:
        raise NotFound("کاربر پیدا نشد")
    import secrets
    temp = sanitize_text(body.get("password"), max_len=128, strip_newlines=True) or \
        "Vlx-" + secrets.token_urlsafe(12)
    floor = max(8, get_config().min_password_length)
    if len(temp) < floor:
        raise BadRequest(f"رمز موقت حداقل {floor} کاراکتر", code="WEAK_PASSWORD")
    db.execute(c, """UPDATE users SET password = ?, must_change_password = 1,
                      password_changed_at = CURRENT_TIMESTAMP WHERE id = ?""",
               (hash_password(temp), uid)).close()
    from ..auth import revoke_user_sessions
    revoke_user_sessions(db, c, uid, reason="admin_reset")
    _audit(db, c, "password_reset", target_type="user", target_id=uid)
    c.commit()
    return jsonify({"success": True, "temporary_password": temp, "must_change": True,
                    "message": "رمز موقت تنظیم شد؛ همه نشست‌های کاربر باطل شد"})


# --------------------------------------------------------------------------
# content + servers
# --------------------------------------------------------------------------
@mod.route("/posts", auth="admin", rate=None)
def posts():
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=25)
    where, params = ["1=1"], []
    if str(request.args.get("deleted", "")).lower() in {"1", "true"}:
        where.append("p.deleted_at IS NOT NULL")
    q = sanitize_text(request.args.get("q"), max_len=48, strip_newlines=True)
    if q:
        where.append(f"{db.ilike('p.content')}")
        params.extend(db.ilike_params(q))
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM posts p WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT p.id, p.content, p.file_path, p.file_type, p.timestamp, p.visibility,
               p.like_count, p.comment_count, p.view_count, p.deleted_at,
               u.id AS author_id, u.username, u.full_name, u.avatar
        FROM posts p JOIN users u ON u.id = p.user_id
        WHERE {clause} ORDER BY p.id DESC {db.limit_offset(limit, offset)}""", params)
    return jsonify({"success": True, "posts": [dict(r) for r in rows],
                    **paginated(total, limit, offset, page)})


@mod.legacy(f"{LEGACY}/delete_post/<int:pid>", methods=("POST", "DELETE"),
            auth="admin", rate=(30, 600))
def legacy_delete_post(pid: int):
    return _delete_post(pid)


@mod.route("/posts/<int:pid>", methods=["DELETE"], auth="admin", rate=(30, 600))
def delete_post(pid: int):
    return _delete_post(pid)


def _delete_post(pid: int):
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, user_id, file_path FROM posts WHERE id = ?", (pid,))
    if row is None:
        raise NotFound("پست پیدا نشد")
    # Soft delete keeps the media on disk: the post is recoverable, and the
    # storage sweep in janitor.py removes files no row references any more.
    db.execute(c, "UPDATE posts SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (pid,)).close()
    _audit(db, c, "post_delete", target_type="post", target_id=pid)
    c.commit()
    from ..notify import emit_to_user
    emit_to_user(_sio(), int(row["user_id"]), "post_moderated",
                 {"post_id": pid, "action": "deleted", "reason": "تصمیم مدیریتی"})
    return jsonify({"success": True, "post_id": pid, "mode": "soft",
                    "message": "پست حذف شد (نرم)"})


@mod.legacy(f"{LEGACY}/clear_all_messages", methods=("POST", "DELETE"),
            auth="admin", rate=(3, 3600))
def clear_all_messages():
    """
    Legacy maintenance endpoint, kept but made explicit and reversible-by-backup only.

    It now requires the same `confirm` phrase as user deletion and writes an
    audit row: previously one click wiped every message with no trace.
    """
    body = payload() if request.method == "POST" else {}
    if sanitize_text(body.get("confirm"), max_len=32, strip_newlines=True).lower() != "clear":
        raise BadRequest("برای پاک کردن همه پیام‌ها confirm=clear بفرستید", code="CONFIRM_REQUIRED")
    db, c = current_db(), conn()
    n = int(db.scalar(c, "SELECT COUNT(*) FROM messages") or 0)
    db.execute(c, "DELETE FROM messages").close()
    if db.has_table(c, "message_reactions"):
        db.execute(c, "DELETE FROM message_reactions").close()
    _audit(db, c, "messages_clear", note=f"{n} rows")
    c.commit()
    return jsonify({"success": True, "cleared": n, "message": f"{n} پیام پاک شد"})


@mod.route("/servers", auth="admin", rate=None)
def servers():
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=25)
    where, params = ["1=1"], []
    status = sanitize_text(request.args.get("status"), max_len=16, strip_newlines=True)
    if status in _SERVER_STATUSES:
        where.append("s.status = ?"); params.append(status)
    if str(request.args.get("archived", "")).lower() in {"1", "true"}:
        where.append("s.archived_at IS NOT NULL")
    q = sanitize_text(request.args.get("q"), max_len=48, strip_newlines=True)
    if q:
        where.append(f"({db.ilike('s.name')} OR s.ip_address = ?)")
        params.extend([*db.ilike_params(q), q])
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM lan_hosts s WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT s.id, s.name, s.game_name, s.ip_address, s.port, s.status, s.region,
               s.player_count, s.players_max, s.heartbeat_fails, s.last_heartbeat, s.created_at,
               s.archived_at, COALESCE(s.is_enabled,1) AS is_enabled, s.manual_status,
               COALESCE(u.full_name, '—') AS host_name, u.id AS host_id, u.username AS host_username
        FROM lan_hosts s JOIN users u ON u.id = s.user_id
        WHERE {clause} ORDER BY s.id DESC {db.limit_offset(limit, offset)}""", params)
    stale = [dict(r) for r in db.query(c, f"""
        SELECT id, name, ip_address, port, status, heartbeat_fails, last_heartbeat FROM lan_hosts
        WHERE archived_at IS NULL AND
              (last_heartbeat IS NULL OR last_heartbeat < {db.hours_ahead_sql(-1)})
        ORDER BY id DESC LIMIT 20""")]
    cfg_pol = get_config()
    return jsonify({"success": True, "servers": [dict(r) for r in rows],
                    "stale": stale,
                    "policy": {"discovery_enabled": cfg_pol.discovery_enabled,
                               "discovery_networks": list(cfg_pol.discovery_networks),
                               "heartbeat_timeout_seconds": cfg_pol.heartbeat_timeout_seconds,
                               "note": ("probe only reaches allowlisted literal IPs; empty "
                                        "discovery_networks means nothing is probeable")},
                    **paginated(total, limit, offset, page)})


@mod.route("/servers/<int:sid>/disable", methods=["POST"], auth="admin", rate=(30, 600))
def disable_server(sid: int):
    enabled = not bool_arg("value", True, src=payload())
    db, c = current_db(), conn()
    cur = db.execute(c, "UPDATE lan_hosts SET is_enabled = ? WHERE id = ?", (1 if enabled else 0, sid))
    if not cur.rowcount:
        cur.close()
        raise NotFound("سرور پیدا نشد")
    cur.close()
    note = text_field("note", payload(), max_len=300, required=False)
    _audit(db, c, "server_disable" if not enabled else "server_enable",
           target_type="server", target_id=sid, note=note)
    c.commit()
    return jsonify({"success": True, "enabled": int(enabled)})


@mod.route("/servers/<int:sid>", methods=["DELETE"], auth="admin", rate=(10, 600))
def delete_server_row(sid: int):
    db, c = current_db(), conn()
    if sanitize_text(payload().get("confirm"), max_len=32, strip_newlines=True).lower() != "delete":
        raise BadRequest("برای حذف دائم confirm=delete بفرستید", code="CONFIRM_REQUIRED")
    if db.query_one(c, "SELECT id, user_id FROM lan_hosts WHERE id = ?", (sid,)) is None:
        raise NotFound("سرور پیدا نشد")
    db.execute(c, "DELETE FROM server_players WHERE server_id = ?", (sid,)).close()
    db.execute(c, "DELETE FROM server_follows WHERE server_id = ?", (sid,)).close()
    db.execute(c, "DELETE FROM server_status_log WHERE server_id = ?", (sid,)).close()
    db.execute(c, "DELETE FROM game_rooms WHERE server_id = ?", (sid,)).close()
    db.execute(c, "DELETE FROM lan_hosts WHERE id = ?", (sid,)).close()
    _audit(db, c, "server_delete", target_type="server", target_id=sid)
    c.commit()
    return jsonify({"success": True, "deleted": True, "server_id": sid})


# --------------------------------------------------------------------------
# audit log + system
# --------------------------------------------------------------------------
@mod.route("/audit", auth="admin", rate=None)
def audit():
    db, c = current_db(), conn()
    if not db.has_table(c, "audit_log"):
        return jsonify({"success": True, "entries": [], "total": 0})
    limit, offset, page = pagination_args(default_size=40)
    where, params = ["1=1"], []
    action = sanitize_text(request.args.get("action"), max_len=48, strip_newlines=True)
    if action:
        where.append("a.action = ?"); params.append(action)
    who = int_arg("actor_id", src=request.args, lo=1)
    if who:
        where.append("a.actor_id = ?"); params.append(who)
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM audit_log a WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT a.*, u.username AS actor_username FROM audit_log a
        LEFT JOIN users u ON u.id = a.actor_id
        WHERE {clause} ORDER BY a.id DESC {db.limit_offset(limit, offset)}""", params)
    return jsonify({"success": True, "entries": [dict(r) for r in rows],
                    **paginated(total, limit, offset, page)})


@mod.route("/health", auth="admin", rate=None)
def health():
    db, c = current_db(), conn()
    from .. import migrations
    applied = [int(r["version"]) for r in db.query(c, "SELECT version FROM vx_schema_version")] \
        if db.has_table(c, "vx_schema_version") else []
    report = {"success": True,
              "schema_applied": applied,
              "schema_expected": migrations.expected_version(),
              "schema_pending": migrations.pending_count(db, c),
              "engine": db.engine,
              "presence": presence.snapshot()}
    if request.args.get("deep"):
        report["migrations"] = [dict(r) for r in migrations.status(db, c)]
    return jsonify(report)


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
