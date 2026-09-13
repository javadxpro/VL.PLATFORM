"""
Notifications + realtime fan-out.

Fixes the leak in the pre-upgrade build, where `add_notification` called
`socketio.emit(...)` with no room, so **every connected browser** received
someone else's notification (docs/AUDIT.md §7 S2). Same for private messages.

Contract:
  * `notify(...)` writes one row, then emits **only** to the recipient's room.
  * `emit_to_user(...)` / `emit_to_users(...)` are the only ways application
    events reach a browser. Broadcast to the whole room is opt-in and explicit
    (`emit_public`) and reserved for genuinely public signals.
  * per-user preferences in `user_settings` gate delivery; a disabled type is
    still written to the DB (audit/inbox) but not pushed realtime.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .db import Database
from .log import get_logger

log = get_logger("notify")

#: Stable vocabulary. Add here first, then handle in the frontend.
TYPES: dict[str, dict[str, Any]] = {
    "follow":         {"setting": "notify_follow", "icon": "user-plus", "fa": "شما را دنبال کرد"},
    "friend_request": {"setting": "notify_friend_request", "icon": "user-plus", "fa": "درخواست دوستی فرستاد"},
    "friend_accept":  {"setting": "notify_friend_request", "icon": "user-check", "fa": "درخواست دوستی شما را پذیرفت"},
    "like":           {"setting": "notify_like", "icon": "heart", "fa": "پست شما را پسندید"},
    "comment":        {"setting": "notify_comment", "icon": "message", "fa": "زیر پست شما نظر داد"},
    "mention":        {"setting": "notify_mention", "icon": "at", "fa": "شما را صدا زد"},
    "message":        {"setting": "notify_message", "icon": "send", "fa": "پیام جدید برای شما فرستاد"},
    "group_invite":   {"setting": "notify_group_invite", "icon": "users", "fa": "شما را به گروه دعوت کرد"},
    "game_invite":    {"setting": "notify_game_invite", "icon": "gamepad", "fa": "به بازی دعوتتان کرد"},
    "server_online":  {"setting": "notify_server_status", "icon": "wifi", "fa": "سرور آنلاین شد"},
    "server_offline": {"setting": "notify_server_status", "icon": "wifi-off", "fa": "سرور آفلاین شد"},
    "room_join":      {"setting": "notify_room_activity", "icon": "login", "fa": "به روم شما پیوست"},
    "room_leave":     {"setting": "notify_room_activity", "icon": "logout", "fa": "از روم شما خارج شد"},
    "room_full":      {"setting": "notify_room_activity", "icon": "users", "fa": "اتاق بازی پر شد"},
    "room_start":     {"setting": "notify_room_activity", "icon": "play", "fa": "اتاق بازی را شروع کرد"},
    "report_resolved": {"setting": None, "icon": "shield", "fa": "گزارش شما بررسی شد"},
    "moderation":     {"setting": None, "icon": "shield", "fa": "توسط مدیر"},
    "story":          {"setting": "notify_like", "icon": "circle", "fa": "استوری شما را دید"},
}

USER_ROOM = "user:{}"
SETTABLE = {t: meta["setting"] for t, meta in TYPES.items() if meta.get("setting")}


def user_room(user_id: int) -> str:
    return USER_ROOM.format(int(user_id))


def settings_allows(db: Database, conn, user_id: int, ntype: str) -> bool:
    setting = TYPES.get(ntype, {}).get("setting")
    if not setting:
        return True
    row = db.query_one(conn, f"SELECT {setting} AS v FROM user_settings WHERE user_id = ?", (user_id,))
    if row is None:
        return True                                  # no row yet => defaults are permissive
    return bool(row.get("v") if row.get("v") is not None else 1)


def notify(db: Database, conn, *, user_id: int | None, actor_id: int | None, ntype: str,
           target_id: int | None = None, body: str = "", target_type: str | None = None,
           room_id: int | None = None, server_id: int | None = None,
           priority: str = "normal", data: dict | None = None,
           socketio=None, broadcast_private: bool = False) -> int | None:
    """
    Record one notification and push it to its owner only.

    Returns the notification id, or None when nothing was written (self-notify,
    unknown recipient).
    """
    try:
        uid = int(user_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not uid:
        return None
    try:
        actor = int(actor_id) if actor_id is not None else None    # type: ignore[arg-type]
    except (TypeError, ValueError):
        actor = None
    if actor and actor == uid:
        return None                                   # never notify yourself
    meta = TYPES.get(ntype, {})
    payload_json = json.dumps(data or {}, ensure_ascii=False)[:2000] if data else None
    try:
        nid = db.insert(conn, """
            INSERT INTO notifications (user_id, actor_id, type, target_id, target_type,
                                       body, icon, room_id, server_id, priority, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, actor, ntype[:32], target_id, (target_type or "")[:24], body[:280],
             meta.get("icon", "bell"), room_id, server_id, priority[:12], payload_json))
    except Exception as exc:                          # pragma: no cover
        log.warning("notify_insert_failed", extra={"ctx": {"err": str(exc)[:160]}})
        return None

    if socketio is None or not broadcast_private:
        # Realtime push is the caller's job unless they asked us to own it.
        if socketio is not None:
            _push(db, conn, socketio, uid, nid, actor, ntype, target_id, meta, body, room_id, server_id)
    return nid


def _push(db: Database, conn, socketio, uid: int, nid: int, actor: int | None,
          ntype: str, target_id: int | None, meta: dict, body: str,
          room_id: int | None, server_id: int | None) -> None:
    if not settings_allows(db, conn, uid, ntype):
        return
    actor_row = db.query_one(conn, "SELECT id, username, full_name, avatar FROM users WHERE id = ?",
                             (actor,)) if actor else None
    emit_to_user(socketio, uid, "notify", {
        "id": nid, "type": ntype, "target_id": target_id,
        "target_type": meta.get("icon"),
        "label": meta.get("fa", ""),
        "body": body or "",
        "icon": meta.get("icon", "bell"),
        "room_id": room_id, "server_id": server_id,
        "actor": dict(actor_row) if actor_row else None,
        "actor_name": (actor_row or {}).get("full_name") if actor_row else "?",
    })


def emit_to_user(socketio, user_id: int, event: str, data: Any) -> None:
    """Send to one user's room. The default namespace only — see realtime.py."""
    if socketio is None:
        return
    try:
        socketio.emit(event, data, room=user_room(int(user_id)))
    except Exception as exc:                                   # pragma: no cover
        log.debug("emit_failed", extra={"ctx": {"err": str(exc)[:120], "event": event}})


def emit_to_users(socketio, user_ids: Iterable[int], event: str, data: Any) -> None:
    for uid in {int(u) for u in user_ids if u}:
        emit_to_user(socketio, uid, event, data)


def emit_to_room_participants(socketio, room_id: int, event: str, data: Any) -> None:
    try:
        socketio.emit(event, data, room=f"game_room:{int(room_id)}")
    except Exception:                                          # pragma: no cover
        pass


def emit_group(socketio, group_id: int, event: str, data: Any) -> None:
    """Group-wide event is legitimately group-scoped, not global."""
    try:
        socketio.emit(event, data, room=f"group:{int(group_id)}")
    except Exception:                                          # pragma: no cover
        pass


def emit_pair(socketio, a: int | None, b: int | None, event: str, data: Any) -> None:
    """DM traffic goes to exactly the two humans in the conversation."""
    for uid in (a, b):
        if uid:
            emit_to_user(socketio, int(uid), event, data)


def emit_public(socketio, event: str, data: Any) -> None:
    """Explicit broadcast. Reserved for presence/global announcements."""
    try:
        socketio.emit(event, data)
    except Exception:                                          # pragma: no cover
        pass


def unread_count(db: Database, conn, user_id: int) -> int:
    row = db.query_one(conn, "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0",
                       (user_id,))
    return int(row["n"]) if row else 0


def parse_data(row: dict) -> dict:
    raw = row.get("data")
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def mark_read(db: Database, conn, user_id: int, *, up_to: int | None = None) -> int:
    if up_to:
        cur = db.execute(conn, "UPDATE notifications SET is_read = 1, read_at = CURRENT_TIMESTAMP "
                                "WHERE user_id = ? AND is_read = 0 AND id <= ?", (user_id, up_to))
    else:
        cur = db.execute(conn, "UPDATE notifications SET is_read = 1, read_at = CURRENT_TIMESTAMP "
                                "WHERE user_id = ? AND is_read = 0", (user_id,))
    try:
        return cur.rowcount
    finally:
        cur.close()


def render_prefs(db: Database, conn, user_id: int) -> dict[str, bool]:
    row = db.query_one(conn, "SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    base = {key: True for key in SETTABLE.values() if key}
    base.update({"nsfw_filter": False, "activity_public": True, "dm_from": False})
    if not row:
        return base
    out = {}
    for key in set(list(base) + list(row.keys())):
        if key in ("user_id", "updated_at"):
            continue
        val = row.get(key)
        if isinstance(val, int):
            out[key] = bool(val)
    out.setdefault("dm_from", bool((row or {}).get("dm_from") or 0))
    return out
