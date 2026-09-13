"""
Notifications: types, pagination, preferences, targeted delivery.

`/notifications/<me>` keeps its legacy `{unread, items}` shape; everything else
is additive. The realtime push moved from a global broadcast to the recipient's
own room, which is the fix for AUDIT §7 S2.
"""

from __future__ import annotations


from flask import jsonify, request

from . import Module, conn, current_db, int_arg, is_admin_user, my_id, payload
from ..errors import BadRequest, Forbidden, NotFound
from ..log import get_logger
from ..notify import TYPES, parse_data, render_prefs, user_room
from ..security import pagination_args, paginated, sanitize_text

mod = Module("notifications")
log = get_logger("api.notifications")

#: which events are pushable realtime (vs inbox-only)
PUSHABLE = set(TYPES)


@mod.route("", methods=["GET"], auth="user", rate=None)
def list_notifications():
    limit, offset, page = pagination_args(default_size=30)
    me = my_id()
    db, c = current_db(), conn()
    where = ["n.user_id = ?"]
    params: list = [me]
    ntype = sanitize_text(request.args.get("type"), max_len=24, strip_newlines=True)
    if ntype:
        if ntype not in TYPES:
            raise BadRequest("نوع اعلان نامعتبر است", code="BAD_TYPE",
                             details={"allowed": sorted(TYPES)})
        where.append("n.type = ?"); params.append(ntype)
    if str(request.args.get("unread", "")).lower() in {"1", "true", "yes"}:
        where.append("n.is_read = 0")
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM notifications n WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT n.*, u.full_name AS actor_name, u.username AS actor_username, u.avatar AS actor_avatar
        FROM notifications n LEFT JOIN users u ON u.id = n.actor_id
        WHERE {clause} ORDER BY n.id DESC {db.limit_offset(limit, offset)}""", params)
    items = []
    for r in rows:
        d = dict(r)
        d["label"] = TYPES.get(d.get("type") or "", {}).get("fa", "")
        d["data"] = parse_data(d)
        d["read"] = bool(int(d.get("is_read") or 0))
        items.append(d)
    body = {"success": True, "items": items,
            "unread": db.scalar(c, "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (me,)),
            **paginated(total, limit, offset, page)}
    return jsonify(body)


@mod.legacy("/notifications/<int:me_id>", methods=("GET",), rate=None)
def legacy_notifications(me_id: int):
    """Legacy: {unread, items} for *your own* id only."""
    if me_id != my_id() and not is_admin_user():
        raise Forbidden("دسترسی غیرمجاز")
    db, c = current_db(), conn()
    rows = db.query(c, """
        SELECT n.*, u.full_name AS actor_name, u.avatar AS actor_avatar
        FROM notifications n LEFT JOIN users u ON u.id = n.actor_id
        WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 40""", (me_id,))
    items = []
    for r in rows:
        d = dict(r)
        d["label"] = TYPES.get(d.get("type") or "", {}).get("fa", "")
        items.append(d)
    unread = db.scalar(c, "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (me_id,))
    return jsonify({"unread": int(unread), "items": items})


@mod.route("/unread", auth="user", rate=None)
def unread():
    me = my_id()
    db, c = current_db(), conn()
    return jsonify({"success": True,
                    "unread": int(db.scalar(c, "SELECT COUNT(*) FROM notifications "
                                                "WHERE user_id = ? AND is_read = 0", (me,)) or 0)})


@mod.route("/read", methods=["POST"], auth="user", rate=(120, 60))
def read():
    """Mark all, or up to a given id (the SPA's 'seen this far' pattern)."""
    me = my_id()
    body = payload()
    up_to = int_arg("up_to_id", src=body, lo=0)
    one = int_arg("id", src=body, lo=0)
    db, c = current_db(), conn()
    if one:
        cur = db.execute(c, "UPDATE notifications SET is_read = 1, read_at = CURRENT_TIMESTAMP "
                            "WHERE user_id = ? AND id = ?", (me, one))
    else:
        cur = db.execute(c, "UPDATE notifications SET is_read = 1, read_at = CURRENT_TIMESTAMP "
                            "WHERE user_id = ? AND is_read = 0"
                            + (f" AND id <= {int(up_to)}" if up_to else ""), (me,))
    n = cur.rowcount
    cur.close()
    if db.has_table(c, "notifications_seen"):
        db.execute(c, f"""INSERT INTO notifications_seen (user_id, last_read_at)
                         VALUES (?, {db.now_sql()})
                         ON CONFLICT (user_id) DO UPDATE SET last_read_at = {db.now_sql()}""", (me,)).close()
    c.commit()
    _sio().emit("notif_read", {"unread": 0}, room=user_room(me))
    return jsonify({"success": True, "marked": int(n)})


@mod.legacy("/notifications_read/<int:me_id>", methods=("POST",), rate=(120, 60))
def legacy_read(me_id: int):
    if me_id != my_id() and not is_admin_user():
        raise Forbidden("دسترسی غیرمجاز")
    return read()


@mod.route("/<int:nid>/read", methods=["POST"], auth="user", rate=(200, 60))
def read_one(nid: int):
    me = my_id()
    db, c = current_db(), conn()
    cur = db.execute(c, "UPDATE notifications SET is_read = 1, read_at = CURRENT_TIMESTAMP "
                        "WHERE id = ? AND user_id = ?", (nid, me))
    n = cur.rowcount
    cur.close()
    c.commit()
    if not n:
        raise NotFound("اعلانی پیدا نشد")
    return jsonify({"success": True})


@mod.route("/<int:nid>", methods=["DELETE"], auth="user", rate=(60, 60))
def delete_one(nid: int):
    me = my_id()
    db, c = current_db(), conn()
    cur = db.execute(c, "DELETE FROM notifications WHERE id = ? AND user_id = ?", (nid, me))
    n = cur.rowcount
    cur.close()
    c.commit()
    if not n:
        raise NotFound("اعلانی پیدا نشد")
    return jsonify({"success": True})


@mod.route("/preferences", methods=["GET", "POST"], auth="user", rate=None, endpoint="prefs")
def preferences():
    me = my_id()
    db, c = current_db(), conn()
    if request.method == "POST":
        body = payload()
        if not db.query_one(c, "SELECT user_id FROM user_settings WHERE user_id = ?", (me,)):
            db.insert(c, "INSERT INTO user_settings (user_id) VALUES (?)", (me,))
        cols = {r["name"] for r in db.query(c, "PRAGMA table_info(user_settings)")} \
            if db.engine == "sqlite" else set(db.columns(c, "user_settings"))
        allowed = {k for k in cols if k.startswith("notify_") or k in
                   {"nsfw_filter", "activity_public", "dm_from"}}
        sets, args = [], []
        for key, value in body.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            args.append(1 if value in (1, True, "1", "true", "on", "yes") else 0)
        if not sets:
            raise BadRequest("هیچ تنظیم معتبری ارسال نشد", code="NO_FIELDS")
        db.execute(c, f"UPDATE user_settings SET {', '.join(sets)} WHERE user_id = ?", (*args, me)).close()
        c.commit()
    return jsonify({"success": True, "preferences": render_prefs(db, c, me),
                    "types": {k: v["fa"] for k, v in TYPES.items()},
                    "pushable": sorted(PUSHABLE)})


@mod.route("/test", methods=["POST"], auth="user", rate=(5, 300))
def test_push():
    """
    Explicit self-test for the realtime path.

    Deliberately notifies *yourself only*, so an admin can verify websocket
    delivery without spamming another account or pretending to.
    """
    me = my_id()
    sio = _sio()
    from ..notify import emit_to_user
    emit_to_user(sio, me, "notify", {"id": 0, "type": "follow", "label": "تست اعلان",
                                     "body": "اگر این را می‌بینید، سوکت شما کار می‌کند ✅",
                                     "actor_name": "سامانه", "test": True})
    return jsonify({"success": True, "delivered": me in set(_connected_ids()),
                    "message": "تست ارسال شد" if me in set(_connected_ids())
                    else "شما به سوکت وصل نیستید — صفحه را باز نگه دارید"})


def _connected_ids() -> list[int]:
    from .. import presence
    return sorted(presence.online_ids())


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
