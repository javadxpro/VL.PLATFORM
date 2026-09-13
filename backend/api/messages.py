"""
Private messaging.

Preserved: send / edit / delete / pin / forward / seen / unread counts, and the
exact legacy response shapes (`/messages/<u1>/<u2>` still returns a bare array,
`/unread_counts/<me>` still returns a bare map).

Changed:
  * realtime delivery is scoped to the two participants (was a global broadcast)
  * every read is pair-scoped and paginated
  * ownership checks cover receiver as well as sender for delete
  * new: search, reactions, delivered receipt, thread reply counts
"""

from __future__ import annotations

from flask import jsonify, request

from . import (Module, bool_arg, conn, current_db, int_arg, is_admin_user,
               my_id, payload, text_field)
from ..errors import BadRequest, Forbidden, NotFound
from ..log import get_logger
from ..notify import emit_pair, notify
from ..security import pagination_args, paginated, sanitize_text
from ..uploads import delete_upload, save_upload
from ..visibility import blocked_between, friends_of

mod = Module("messages")
log = get_logger("api.messages")

MSG_COLS = """
    m.*, u.full_name AS sender_name, u.username AS sender_username, u.avatar AS sender_avatar,
    r.content AS reply_content, r.file_name AS reply_file_name, r.file_type AS reply_file_type,
    ru.full_name AS reply_sender_name,
    (SELECT COUNT(*) FROM message_reactions mr WHERE mr.message_id = m.id) AS reactions_count
"""

#: `MSG_COLS` reads through `r`/`ru` for the quoted-message preview, so every
#: query that selects it must join them. Written inline in five places, four of
#: them forgot — which 500'd sending a message, the pinned list and forwarding.
#: One constant, so the projection and its joins cannot drift apart again.
MSG_FROM = """FROM messages m
        JOIN users u ON u.id = m.sender_id
        LEFT JOIN messages r ON r.id = m.reply_to_id
        LEFT JOIN users ru ON ru.id = r.sender_id"""


#: A "delete for me" row is not a copy of the message: the sender keeps seeing
#: it, so the only honest way to hide it is to filter it out per viewer in SQL
#: (a post-fetch Python filter would break pagination counts).
MSG_NOT_HIDDEN_FOR_ME = ("NOT EXISTS (SELECT 1 FROM message_deletes md "
                         "WHERE md.message_id = m.id AND md.user_id = ?)")


def _public(db, c, row: dict, viewer: int) -> dict:
    out = dict(row)
    if out.get("file_path"):
        out["file_url"] = f"/files/chat/{out['file_path']}"
    out["can_delete"] = bool(out.get("sender_id") == viewer or out.get("receiver_id") == viewer
                             or is_admin_user())
    out["can_edit"] = bool(out.get("sender_id") == viewer)
    if out.get("deleted_for_everyone"):
        # the tombstone keeps its id so replies/forwards still resolve
        out["content"] = ""
        out["file_path"] = None
        out["file_url"] = None
        out["file_name"] = None
    return out


def _participants(db, c, msg_row: dict) -> list[int]:
    """Who is entitled to see this message."""
    gid = msg_row.get("group_id")
    if gid:
        return [int(r["user_id"]) for r in db.query(
            c, "SELECT user_id FROM group_members WHERE group_id = ?", (gid,))]
    return [i for i in (msg_row.get("sender_id"), msg_row.get("receiver_id")) if i]


# --------------------------------------------------------------------------
# threads
# --------------------------------------------------------------------------
@mod.route("/thread/<int:partner>", auth="user", rate=None)
def thread(partner: int):
    limit, offset, page = pagination_args(default_size=50)
    rows, total = _thread_rows(partner, limit=limit, offset=offset)
    return jsonify({"success": True, "messages": rows, "partner_id": partner,
                    **paginated(total, limit, offset, page)})


@mod.legacy("/messages/<int:u1>/<int:u2>", methods=("GET",), rate=None)
def legacy_messages(u1: int, u2: int):
    """
    Legacy contract: bare array, ascending id, both members of the pair
    enforced server-side. Capped to a page instead of the whole table.
    """
    me = my_id()
    if me not in (u1, u2) and not is_admin_user():
        raise Forbidden("دسترسی غیرمجاز")
    partner = u2 if u1 == me else u1
    rows, _total = _thread_rows(int(partner), limit=200, offset=0)
    return jsonify(rows)


def _thread_rows(partner: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
    me = my_id()
    db, c = current_db(), conn()
    if partner == me:
        raise BadRequest("گفتگو با خود ممکن نیست", code="SELF_CHAT")
    where = f"""m.group_id IS NULL AND m.deleted_for_everyone = 0
               AND ((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))
               AND {MSG_NOT_HIDDEN_FOR_ME}"""
    params = [me, partner, partner, me, me]
    total = db.scalar(c, f"SELECT COUNT(*) FROM messages m WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT {MSG_COLS} {MSG_FROM}
        WHERE {where}
        ORDER BY m.id DESC {db.limit_offset(limit, offset)}""", params)
    out = [dict(r) for r in rows]
    out.reverse()                                   # clients render oldest-first
    reactions = _reactions_for(db, c, [int(r["id"]) for r in out], me)
    seen_by_partner = db.query_one(c, """
        SELECT 1 AS x FROM messages WHERE sender_id = ? AND receiver_id = ? AND seen = 1
        ORDER BY id DESC LIMIT 1""", (me, partner))
    return [{**_public(db, c, r, me), "reactions": reactions.get(int(r["id"]), {}),
             "partner_read_upto": bool(seen_by_partner)} for r in out], int(total)


def _reactions_for(db, c, ids: list[int], viewer: int) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = db.query(c, f"""
        SELECT message_id, emoji, COUNT(*) AS n,
               MAX(CASE WHEN user_id = ? THEN 1 ELSE 0 END) AS mine
        FROM message_reactions WHERE message_id IN ({marks}) GROUP BY message_id, emoji""",
        [viewer, *ids])
    out: dict[int, dict] = {}
    for r in rows:
        out.setdefault(int(r["message_id"]), {})[r["emoji"]] = {
            "count": int(r["n"]), "mine": bool(r["mine"])}
    return out


@mod.route("/partners", auth="user", rate=None)
def partners():
    """Conversation list with last message + unread, in one query."""
    me = my_id()
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=30)
    rows = db.query(c, f"""
        SELECT other_id,
               MAX(mid) AS last_id, SUM(unread) AS unread, COUNT(*) AS messages
        FROM (
            SELECT CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AS other_id,
                   m.id AS mid,
                   CASE WHEN m.receiver_id = ? AND m.seen = 0 THEN 1 ELSE 0 END AS unread
            FROM messages m
            WHERE m.group_id IS NULL AND (m.sender_id = ? OR m.receiver_id = ?)
              AND m.deleted_for_everyone = 0
        ) t
        WHERE other_id IS NOT NULL
        GROUP BY other_id ORDER BY last_id DESC {db.limit_offset(limit, offset)}""",
        (me, me, me, me))
    ids = [int(r["other_id"]) for r in rows]
    users = {}
    if ids:
        marks = ", ".join("?" for _ in ids)
        users = {int(u["id"]): dict(u) for u in db.query(
            c, f"""SELECT id, username, full_name, avatar, presence, last_seen_at
                   FROM users WHERE id IN ({marks})""", ids)}
    last_msgs = {}
    if ids:
        marks = ", ".join("?" for _ in ids)
        for r in db.query(c, """
            SELECT id, content, file_name, file_type, timestamp, sender_id, receiver_id
            FROM messages WHERE id IN (SELECT MAX(id) FROM messages
                                       WHERE (sender_id = ? OR receiver_id = ?) AND group_id IS NULL
                                         AND deleted_for_everyone = 0
                                       GROUP BY CASE WHEN sender_id = ? THEN receiver_id ELSE sender_id END)""",
            (me, me, me)):
            # key by the *partner*, not the author: the last message is usually
            # mine, and keying it by sender_id dropped it from my own list
            other = int(r["receiver_id"]) if int(r["sender_id"]) == me else int(r["sender_id"])
            if other:
                last_msgs[other] = {k: v for k, v in dict(r).items() if k != "receiver_id"}
    from .. import presence
    out = []
    for r in rows:
        oid = int(r["other_id"])
        u = users.get(oid, {"id": oid})
        item = dict(u)
        item["unread"] = int(r["unread"] or 0)
        item["messages"] = int(r["messages"] or 0)
        item["last_message"] = last_msgs.get(oid)
        item["is_online"] = presence.is_online(oid)
        out.append(item)
    return jsonify({"success": True, "partners": out, **paginated(len(out), limit, offset, page)})


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------
@mod.route("", methods=["POST"], auth="user", rate=(90, 60),
           legacy="/send_message", legacy_methods=("POST",), endpoint="send_message")
def send_message():
    """
    Send to a user or a group.

    Identity is `g.user` — the legacy `sender_id` form field is accepted and
    *rejected* if it disagrees, which is what the old endpoint started doing and
    what any client that used it legitimately will keep doing fine.
    """
    me = my_id()
    body = payload(form=True)
    claimed = int_arg("sender_id", src=body)
    if claimed is not None and claimed != me:
        raise Forbidden("هویت ارسال‌کننده معتبر نیست", code="SENDER_MISMATCH")

    content = text_field("content", body, max_len=4000, required=False, newlines=True)
    group_id = int_arg("group_id", src=body, lo=0)
    receiver_id = int_arg("receiver_id", src=body, lo=0)
    reply_to_id = int_arg("reply_to_id", src=body, lo=0)

    if bool(group_id) == bool(receiver_id):
        raise BadRequest(" دقیقاً یک مقصد لازم است", code="DESTINATION_REQUIRED")

    db, c = current_db(), conn()
    if group_id:
        member = db.query_one(c, "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?",
                              (group_id, me))
        if member is None:
            raise Forbidden("عضو این گروه نیستی", code="NOT_A_MEMBER")
        if _is_muted(db, c, group_id, me):
            raise Forbidden("در این گروه بی‌صدا هستید", code="MUTED")
        group = db.query_one(c, "SELECT only_admins_post FROM groups WHERE id = ?", (group_id,))
        if group and int(group.get("only_admins_post") or 0) and member["role"] not in ("owner", "admin"):
            raise Forbidden("فقط مدیران این گروه می‌توانند پیام بفرستند", code="ADMINS_ONLY")
        receiver_id = None
    else:
        if receiver_id == me:
            raise BadRequest("نمی‌توانی به خودت پیام بدهی", code="SELF_DM")
        target = db.query_one(c, "SELECT id FROM users WHERE id = ?", (receiver_id,))
        if target is None:
            raise NotFound("گیرنده پیدا نشد")
        if blocked_between(db, c, me, int(receiver_id)):
            raise Forbidden("امکان ارسال پیام وجود ندارد", code="BLOCKED")
        prefs = db.query_one(c, "SELECT dm_from FROM user_settings WHERE user_id = ?", (receiver_id,))
        if prefs and int(prefs.get("dm_from") or 0) == 1 and receiver_id not in friends_of(db, c, me):
            raise Forbidden("گیرنده پیام خصوصی را از غیردوست‌ها بسته است", code="DM_CLOSED")

    if reply_to_id:
        parent = db.query_one(c, "SELECT id FROM messages WHERE id = ?", (reply_to_id,))
        if parent is None:
            raise NotFound("پیام مرجع پیدا نشد")

    saved = None
    field = request.files.get("file")
    if field is not None and getattr(field, "filename", ""):
        saved = save_upload(field, user_id=me, category="chat")
    if not content and saved is None:
        raise BadRequest("متن یا فایل لازم است", code="EMPTY_MESSAGE")

    msg_type = saved.kind if saved else "text"
    if saved and saved.kind == "audio" and body.get("duration_ms"):
        msg_type = "voice"
    mid = db.insert(c, """
        INSERT INTO messages (sender_id, receiver_id, group_id, content, file_path, file_type,
                              file_name, reply_to_id, msg_type, duration_ms, search_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (me, receiver_id, group_id, content, saved.name if saved else None,
         saved.kind if saved else None, saved.original_name if saved else None,
         reply_to_id, msg_type, int_arg("duration_ms", src=body), content or None))
    if reply_to_id:
        db.execute(c, "UPDATE messages SET reply_count = COALESCE(reply_count,0) + 1 WHERE id = ?",
                   (reply_to_id,)).close()
    row = db.query_one(c, f"SELECT {MSG_COLS} {MSG_FROM} WHERE m.id = ?", (mid,))
    msg = dict(row or {"id": mid})
    if saved:
        msg["file_url"] = saved.url
    if not group_id:
        _notify_dm(db, c, msg, receiver_id=int(receiver_id))
    c.commit()

    if group_id:
        from ..notify import emit_group
        emit_group(_sio(), int(group_id), "new_message", msg)
        _bump_group_unread(db, c, int(group_id), me, mid)
    else:
        emit_pair(_sio(), me, int(receiver_id), "new_message", msg)
    log.info("message_sent", extra={"ctx": {"from": me, "group": bool(group_id),
                                            "has_file": bool(saved)}})
    return jsonify({"success": True, "message": msg}), 201


def _notify_dm(db, c, msg: dict, *, receiver_id: int) -> None:
    me = int(msg["sender_id"])
    notify(db, c, user_id=receiver_id, actor_id=me, ntype="message",
           target_id=int(msg["id"]), target_type="message",
           body=(msg.get("content") or "")[:120] or (msg.get("file_name") or "پیام"),
           socketio=_sio())


def _is_muted(db, c, group_id: int, uid: int) -> bool:
    row = db.query_one(c, "SELECT muted_until FROM group_members WHERE group_id = ? AND user_id = ?",
                       (group_id, uid))
    if not row or not row.get("muted_until"):
        return False
    from ..auth import is_past
    return not is_past(row["muted_until"])


def _bump_group_unread(db, c, gid: int, sender: int, mid: int) -> None:
    for uid in db.query(c, "SELECT user_id FROM group_members WHERE group_id = ? AND user_id != ?",
                        (gid, sender)):
        notify(db, c, user_id=int(uid["user_id"]), actor_id=sender, ntype="message",
               target_id=mid, target_type="group_message", body="پیام جدید در گروه",
               socketio=_sio())


# --------------------------------------------------------------------------
# edit / delete / reactions
# --------------------------------------------------------------------------
@mod.route("/<int:mid>", methods=["PATCH", "POST"], auth="user", rate=(60, 60),
           legacy="/edit_message", legacy_methods=("POST",), endpoint="edit_message")
def edit_message(mid: int | None = None):
    body = payload()
    if mid is None:
        mid = int_arg("message_id", src=body)
    if not mid:
        raise BadRequest("شناسه پیام لازم است", code="MESSAGE_ID_REQUIRED")
    content = text_field("content", body, max_len=4000, newlines=True)
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT * FROM messages WHERE id = ?", (mid,))
    if row is None:
        raise NotFound("پیام پیدا نشد")
    if int(row["sender_id"]) != me:
        raise Forbidden("فقط فرستنده می‌تواند پیام را ویرایش کند", code="NOT_OWNER")
    db.execute(c, """UPDATE messages SET content = ?, edited_at = CURRENT_TIMESTAMP,
                     search_text = ?, edited_by = ? WHERE id = ?""",
               (content, content, me, mid)).close()
    new = db.query_one(c, f"SELECT {MSG_COLS} {MSG_FROM} WHERE m.id = ?", (mid,))
    msg = dict(new or {"id": mid, "content": content})
    c.commit()
    emit_pair(_sio(), int(row["sender_id"]), row.get("receiver_id"), "message_updated", msg)
    if row.get("group_id"):
        from ..notify import emit_group
        emit_group(_sio(), int(row["group_id"]), "message_updated", msg)
    return jsonify({"success": True, "message": msg})


@mod.route("/<int:mid>", methods=["DELETE"], auth="user", rate=(60, 60),
           legacy="/delete_message/<int:mid>", legacy_methods=("DELETE",), endpoint="delete_message")
def delete_message(mid: int):
    """
    Delete for everyone (sender, admins, group owner) or just for me.

    The pre-upgrade rule is kept: sender, the receiving party, the group owner
    and admins may remove. `for_me` narrows it to a single side.
    """
    me = my_id()
    # Three spellings, one meaning: the SPA sent a query flag, the new client
    # sends `scope` in the body, and `for_me` is the explicit form.
    body = payload()
    mode = str(request.args.get("mode") or body.get("scope") or "").strip().lower()
    for_me = bool_arg("for_me", False, src=body) or mode in {"mine", "me", "for_me", "for-me"}
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT * FROM messages WHERE id = ?", (mid,))
    if row is None:
        raise NotFound("پیام پیدا نشد")
    sender, receiver, gid = int(row["sender_id"]), row.get("receiver_id"), row.get("group_id")
    if for_me and me not in (sender, receiver):
        raise Forbidden("این پیام در گفتگوی شما نیست")
    allowed = me in (sender, receiver) or is_admin_user()
    if gid:
        gm = db.query_one(c, "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?", (gid, me))
        allowed = allowed or bool(gm and gm["role"] in ("owner", "admin", "moderator"))
    if not allowed:
        raise Forbidden("مجاز به حذف این پیام نیستید")
    if for_me and db.has_table(c, "message_deletes"):
        db.execute(c, db.ignore_clause(
            "INSERT INTO message_deletes (message_id, user_id) VALUES (?, ?)",
            on=["message_id", "user_id"]), (mid, me)).close()
        c.commit()
        return jsonify({"success": True, "mode": "mine"})
    file_path = row.get("file_path")
    db.execute(c, """UPDATE messages SET deleted_for_everyone = 1, deleted_by = ?, content = '',
                     edited_at = CURRENT_TIMESTAMP WHERE id = ?""", (me, mid)).close()
    if gid:
        db.execute(c, "DELETE FROM group_seen WHERE group_id = ? AND last_seen_id = ?", (gid, mid)).close()
    if file_path:
        used = db.scalar(c, "SELECT COUNT(*) FROM messages WHERE file_path = ? AND id != ? "
                            "AND deleted_for_everyone = 0", (file_path, mid))
        if not used:
            delete_upload("chat", file_path)
    c.commit()
    emit_pair(_sio(), sender, receiver, "message_deleted", {"id": mid})
    if gid:
        from ..notify import emit_group
        emit_group(_sio(), int(gid), "message_deleted", {"id": mid})
    return jsonify({"success": True, "mode": "everyone"})


@mod.route("/<int:mid>/react", methods=["POST"], auth="user", rate=(120, 60))
def react(mid: int):
    me = my_id()
    emoji = text_field("emoji", payload(), max_len=16)[:8]
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, sender_id, receiver_id, group_id FROM messages WHERE id = ?", (mid,))
    if row is None:
        raise NotFound("پیام پیدا نشد")
    if not _can_see(db, c, row, me):
        raise Forbidden("دسترسی ندارید")
    existing = db.query_one(c, "SELECT id FROM message_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                            (mid, me, emoji))
    if existing:
        db.execute(c, "DELETE FROM message_reactions WHERE id = ?", (existing["id"],)).close()
        active = False
    else:
        db.execute(c, """INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)
                         ON CONFLICT (message_id, user_id, emoji) DO NOTHING""", (mid, me, emoji)).close()
        active = True
    tally = db.query(c, "SELECT emoji, COUNT(*) AS n FROM message_reactions WHERE message_id = ? GROUP BY emoji",
                     (mid,))
    c.commit()
    counts = {r["emoji"]: int(r["n"]) for r in tally}
    packet = {"id": mid, "reactions": counts}
    emit_pair(_sio(), int(row["sender_id"]), row.get("receiver_id"), "message_reaction", packet)
    if row.get("group_id"):
        from ..notify import emit_group
        emit_group(_sio(), int(row["group_id"]), "message_reaction", packet)
    return jsonify({"success": True, "active": active, "counts": counts})


def _can_see(db, c, msg_row, viewer: int) -> bool:
    if viewer in (int(msg_row["sender_id"]), msg_row.get("receiver_id")):
        return True
    gid = msg_row.get("group_id")
    if gid:
        return bool(db.query_one(c, "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?",
                                 (gid, viewer)))
    return is_admin_user()


# --------------------------------------------------------------------------
# read state / unread
# --------------------------------------------------------------------------
@mod.route("/seen/<int:partner>", methods=["POST"], auth="user", rate=(120, 60),
           legacy="/seen_messages/<int:partner>", legacy_methods=("POST",), endpoint="seen_messages")
def seen_messages(partner: int):
    """Marks *incoming* messages from `partner` as read — never another pair's."""
    me = my_id()
    if partner == me:
        raise BadRequest("تنظیم خوانده‌شده برای خود معنا ندارد", code="SELF_SEEN")
    db, c = current_db(), conn()
    cur = db.execute(c, """UPDATE messages SET seen = 1, delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP)
                           WHERE sender_id = ? AND receiver_id = ? AND seen = 0""", (partner, me))
    n = cur.rowcount
    cur.close()
    c.commit()
    if n:
        emit_pair(_sio(), me, partner, "messages_seen", {"by": me, "partner": partner})
    return jsonify({"success": True, "seen": int(n)})


@mod.route("/unread", auth="user", rate=None, legacy="/unread_counts/<int:me_id>",
           endpoint="unread_counts")
def unread_counts(me_id: int | None = None):
    """
    Legacy contract: a bare map of `sender_id -> count`.

    `me_id` from the URL is cross-checked against the token, so guessing another
    user's id returns 403 instead of their unread counts.
    """
    me = my_id()
    if me_id is not None and int(me_id) != me and not is_admin_user():
        raise Forbidden("دسترسی غیرمجاز")
    db, c = current_db(), conn()
    rows = db.query(c, """SELECT sender_id, COUNT(*) AS n FROM messages
                          WHERE receiver_id = ? AND seen = 0 AND group_id IS NULL GROUP BY sender_id""", (me,))
    return jsonify({str(r["sender_id"]): int(r["n"]) for r in rows})


@mod.route("/unread/groups", auth="user", rate=None)
def group_unread():
    me = my_id()
    db, c = current_db(), conn()
    # The cursor lives in group_seen (group_members only has `last_read_at`), so
    # it must be read through that alias — COALESCE keeps brand-new members at 0.
    rows = db.query(c, """
        SELECT gm.group_id, COALESCE(gs.last_seen_id, 0) AS last_seen_id, COUNT(m.id) AS n
        FROM group_members gm
        LEFT JOIN group_seen gs ON gs.group_id = gm.group_id AND gs.user_id = gm.user_id
        LEFT JOIN messages m ON m.group_id = gm.group_id AND m.sender_id != ?
             AND m.id > COALESCE(gs.last_seen_id, 0) AND m.deleted_for_everyone = 0
        WHERE gm.user_id = ?
        GROUP BY gm.group_id, COALESCE(gs.last_seen_id, 0)""", (me, me))
    return jsonify({"success": True, "unread": {str(r["group_id"]): int(r["n"] or 0) for r in rows}})


@mod.route("/<int:mid>/seen-by", auth="user", rate=None)
def seen_by(mid: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT sender_id, receiver_id FROM messages WHERE id = ?", (mid,))
    if row is None:
        raise NotFound("پیام پیدا نشد")
    if int(row["sender_id"]) != me:
        raise Forbidden("فقط فرستنده می‌تواند رسید ببیند")
    seen = bool(db.query_one(c, "SELECT 1 AS x FROM messages WHERE id = ? AND seen = 1", (mid,)))
    return jsonify({"success": True, "seen": seen, "delivered": bool(row.get("receiver_id"))})


# --------------------------------------------------------------------------
# pin / forward / search
# --------------------------------------------------------------------------
@mod.route("/<int:mid>/pin", methods=["POST"], auth="user", rate=(30, 60),
           legacy="/pin_message/<int:mid>", legacy_methods=("POST",), endpoint="pin_message")
def pin_message(mid: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT * FROM messages WHERE id = ?", (mid,))
    if row is None:
        raise NotFound("پیام پیدا نشد")
    gid = row.get("group_id")
    if gid:
        member = db.query_one(c, "SELECT role FROM group_members WHERE group_id = ? AND user_id = ?", (gid, me))
        if member is None:
            raise Forbidden("مجاز نیستید")
        if member["role"] not in ("owner", "admin", "moderator", "member"):
            raise Forbidden("نقش شما اجازه پین نمی‌دهد", code="NO_PIN_PERM")
    ok_see = me in (int(row["sender_id"]), row.get("receiver_id"))
    if not gid and not ok_see:
        raise Forbidden("مجاز نیستید")
    new_state = 0 if int(row.get("pinned") or 0) else 1
    if gid:
        db.execute(c, "UPDATE messages SET pinned = 0 WHERE group_id = ?", (gid,)).close()
    else:
        a, b = int(row["sender_id"]), int(row["receiver_id"])
        db.execute(c, """UPDATE messages SET pinned = 0 WHERE group_id IS NULL AND
                         ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))""",
                   (a, b, b, a)).close()
    db.execute(c, "UPDATE messages SET pinned = ?, pinned_by = ?, pinned_at = CURRENT_TIMESTAMP WHERE id = ?",
               (new_state, me if new_state else None, mid)).close()
    c.commit()
    if gid:
        from ..notify import emit_group
        emit_group(_sio(), int(gid), "messages_pin", {"id": mid, "pinned": bool(new_state)})
    else:
        emit_pair(_sio(), int(row["sender_id"]), row.get("receiver_id"), "messages_pin",
                  {"id": mid, "pinned": bool(new_state)})
    return jsonify({"success": True, "pinned": bool(new_state)})


@mod.route("/pinned", auth="user", rate=None)
def pinned_in_thread():
    partner = int_arg("partner", src=request.args, lo=1)
    gid = int_arg("group_id", src=request.args, lo=0)
    me = my_id()
    db, c = current_db(), conn()
    if gid:
        if not db.query_one(c, "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?", (gid, me)):
            raise Forbidden("عضو این گروه نیستی")
        rows = db.query(c, f"""SELECT {MSG_COLS} {MSG_FROM}
                               WHERE m.group_id = ? AND m.pinned = 1 ORDER BY m.id DESC LIMIT 10""",
                         (gid,))
    elif partner:
        rows = db.query(c, f"""SELECT {MSG_COLS} {MSG_FROM}
                               WHERE m.group_id IS NULL AND m.pinned = 1
                                 AND ((m.sender_id = ? AND m.receiver_id = ?)
                                      OR (m.sender_id = ? AND m.receiver_id = ?))
                               ORDER BY m.id DESC LIMIT 10""", (me, partner, partner, me))
    else:
        raise BadRequest("partner یا group_id لازم است", code="SCOPE_REQUIRED")
    return jsonify({"success": True, "messages": [dict(r) for r in rows]})


@mod.route("/forward", methods=["POST"], auth="user", rate=(40, 60),
           legacy="/forward_message", legacy_methods=("POST",), endpoint="forward_message")
def forward_message():
    me = my_id()
    body = payload()
    mid = int_arg("message_id", src=body)
    targets = body.get("targets") or []
    if isinstance(targets, (str, int)):
        targets = [targets]
    if not mid or not targets:
        raise BadRequest("مقصدی انتخاب نشده", code="NO_TARGETS")
    db, c = current_db(), conn()
    orig = db.query_one(c, "SELECT * FROM messages WHERE id = ?", (mid,))
    if orig is None or orig.get("deleted_for_everyone"):
        raise NotFound("پیام پیدا نشد")
    if not _can_see(db, c, orig, me):
        raise Forbidden("به این پیام دسترسی نداری")
    sent = 0
    emitted: list[tuple[int | None, int | None]] = []
    for raw in list(targets)[:10]:
        if isinstance(raw, (int, str)):
            target = {"type": "user", "id": raw}
        elif isinstance(raw, dict):
            target = raw
        else:
            continue
        ttype, tid = target.get("type"), int_arg("id", src=target, lo=0)
        if not tid:
            continue
        if ttype == "user":
            if int(tid) == me:
                continue
            if blocked_between(db, c, me, int(tid)):
                continue
            new_id = db.insert(c, """
                INSERT INTO messages (sender_id, receiver_id, content, file_path, file_type,
                                      file_name, forwarded, msg_type, search_text)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (me, int(tid), orig.get("content"), orig.get("file_path"), orig.get("file_type"),
                 orig.get("file_name"), orig.get("msg_type") or "text", orig.get("content")))
            sent += 1
            emitted.append((me, int(tid)))
            notify(db, c, user_id=int(tid), actor_id=me, ntype="message", target_id=new_id,
                   target_type="message", body="پیام هدایت شد", socketio=_sio())
        elif ttype == "group":
            if not db.query_one(c, "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?",
                                (int(tid), me)):
                continue
            if _is_muted(db, c, int(tid), me):
                continue
            new_id = db.insert(c, """
                INSERT INTO messages (sender_id, group_id, content, file_path, file_type,
                                      file_name, forwarded, msg_type, search_text)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (me, int(tid), orig.get("content"), orig.get("file_path"), orig.get("file_type"),
                 orig.get("file_name"), orig.get("msg_type") or "text", orig.get("content")))
            sent += 1
            from ..notify import emit_group
            row2 = db.query_one(c, f"SELECT {MSG_COLS} {MSG_FROM} WHERE m.id = ?", (new_id,))
            emit_group(_sio(), int(tid), "new_message", dict(row2 or {"id": new_id}))
    c.commit()
    for a, b in emitted:
        row2 = db.query_one(c, f"""SELECT {MSG_COLS} {MSG_FROM}
                                  WHERE m.sender_id = ? AND m.receiver_id = ?
                                  ORDER BY m.id DESC LIMIT 1""", (a, b))
        if row2:
            emit_pair(_sio(), a, b, "new_message", dict(row2))
    return jsonify({"success": True, "sent": sent})


@mod.route("/search", auth="user", rate=None)
def search_messages():
    """
    Full-text-ish search confined to the requester's own conversations.

    Uses the denormalised `search_text` column so LIKE has something indexed to
    work with, and never reaches outside (me AS sender OR receiver).
    """
    q = sanitize_text(request.args.get("q"), max_len=80, strip_newlines=True)
    if len(q) < 2:
        raise BadRequest("حداقل ۲ کاراکتر لازم است", code="QUERY_TOO_SHORT")
    partner = int_arg("partner", src=request.args, lo=0)
    db, c = current_db(), conn()
    me = my_id()
    limit, offset, page = pagination_args(default_size=30)
    where = ["m.deleted_for_everyone = 0",
             "(m.sender_id = ? OR m.receiver_id = ? OR m.group_id IN "
             "(SELECT group_id FROM group_members WHERE user_id = ?))"]
    params: list = [me, me, me]
    where.append(f"{db.ilike('COALESCE(m.search_text, m.content)')}")
    params.extend(db.ilike_params(q))
    if partner:
        where.append("((m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?))")
        params.extend([me, partner, partner, me])
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM messages m WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT m.id, m.sender_id, m.receiver_id, m.group_id, m.content, m.timestamp,
               m.file_type, m.file_name, u.full_name AS sender_name, u.avatar AS sender_avatar
        FROM messages m JOIN users u ON u.id = m.sender_id
        WHERE {clause} ORDER BY m.id DESC {db.limit_offset(limit, offset)}""", params)
    out = []
    for r in rows:
        d = dict(r)
        text = d.get("content") or ""
        low = text.lower()
        idx = low.find(q.lower())
        if idx > 60:
            d["highlight"] = "…" + text[max(0, idx - 40):idx + len(q) + 60]
        out.append(d)
    return jsonify({"success": True, "messages": out, "query": q,
                    **paginated(total, limit, offset, page)})


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
