"""
Group chat with an explicit permission ladder.

Pre-upgrade there were only `owner` and `member`, and checks were inlined per
endpoint (`role != 'owner' and creator != me and is_admin`). Now roles are
`owner | admin | moderator | member` and every action consults one table,
`PERMISSIONS`, so a new endpoint cannot accidentally forget a check.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from flask import jsonify, request

from . import (Module, bool_arg, conn, current_db, int_arg, is_admin_user,
               my_id, payload, text_field)
from .. import presence
from ..errors import BadRequest, Conflict, Forbidden, NotFound
from ..log import get_logger
from ..notify import emit_group, notify
from ..security import pagination_args, paginated, sanitize_text
from ..uploads import save_upload
from ..visibility import blocked_between

mod = Module("groups")
log = get_logger("api.groups")

ROLES = ("owner", "admin", "moderator", "member")

#: action -> roles allowed (spec §10)
PERMISSIONS: dict[str, set[str]] = {
    "manage_members":   {"owner", "admin"},
    "moderate_messages": {"owner", "admin", "moderator"},
    "edit_group":       {"owner", "admin"},
    "delete_group":     {"owner"},
    "pin":              {"owner", "admin", "moderator"},
    "invite":           {"owner", "admin", "moderator"},
    "kick":             {"owner", "admin"},
    "ban":              {"owner", "admin"},
    "mute":             {"owner", "admin", "moderator"},
    "set_roles":        {"owner"},
    "announce":         {"owner", "admin"},
}


def role_rank(role: str | None) -> int:
    try:
        return ROLES.index((role or "member").strip().lower())
    except ValueError:
        return len(ROLES) - 1


def can(role: str | None, action: str) -> bool:
    return (role or "member") in PERMISSIONS.get(action, set())


def _membership(db, c, gid: int, uid: int) -> dict | None:
    return db.query_one(c, """
        SELECT gm.role, gm.muted_until, gm.title, u.username, u.full_name, u.avatar
        FROM group_members gm JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ? AND gm.user_id = ?""", (gid, uid))


def _require(db, c, gid: int, uid: int, action: str | None = None) -> dict:
    """Membership + permission gate. Admins bypass (platform moderation)."""
    if is_admin_user():
        row = _membership(db, c, gid, uid)
        return row or {"role": "owner", "muted_until": None, "title": None,
                       "username": "admin", "full_name": "مدیر سیستم", "avatar": None}
    row = _membership(db, c, gid, uid)
    if row is None:
        raise Forbidden("عضو این گروه نیستی", code="NOT_A_MEMBER")
    if action and not can(row.get("role"), action):
        raise Forbidden(f"نقش «{row.get('role')}» اجازه «{action}» ندارد", code="NO_PERMISSION")
    return row


@mod.route("", methods=["GET"], auth="user", rate=None)
def list_groups():
    return jsonify({"success": True, "groups": _my_groups()})


@mod.legacy("/my_groups", methods=("GET",), rate=None)
def legacy_my_groups():
    """Legacy contract: bare array with `unread` per group."""
    return jsonify(_my_groups())


def _my_groups() -> list[dict]:
    me = my_id()
    db, c = current_db(), conn()
    rows = db.query(c, """
        SELECT g.id, g.name, g.description, g.avatar, g.creator_id, g.created_at,
               g.is_private, g.slow_mode_seconds, g.only_admins_post, g.max_members,
               gm.role,
               (SELECT COUNT(*) FROM group_members x WHERE x.group_id = g.id) AS member_count,
               (SELECT COUNT(*) FROM messages m WHERE m.group_id = g.id) AS message_count,
               (SELECT MAX(id) FROM messages m WHERE m.group_id = g.id) AS last_message_id
        FROM groups g JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.user_id = ? AND g.deleted_at IS NULL
        ORDER BY last_message_id DESC, g.id DESC""", (me,))
    out = [dict(r) for r in rows]
    if out:
        ids = [int(r["id"]) for r in out]
        marks = ", ".join("?" for _ in ids)
        unread = {int(r["gid"]): int(r["n"]) for r in db.query(c, f"""
            SELECT m.group_id AS gid, COUNT(*) AS n FROM messages m
            WHERE m.group_id IN ({marks}) AND m.sender_id != ? AND m.id > COALESCE(
                (SELECT gs.last_seen_id FROM group_seen gs WHERE gs.group_id = m.group_id
                  AND gs.user_id = ?), 0)
            GROUP BY m.group_id""", [*ids, me, me])}
        for row in out:
            row["unread"] = unread.get(int(row["id"]), 0)
    return out


@mod.route("", methods=["POST"], auth="user", rate=(15, 600),
           legacy="/create_group", legacy_methods=("POST",), endpoint="create_group")
def create_group():
    me = my_id()
    body = payload()
    name = text_field("name", body, max_len=64)
    description = text_field("description", body, max_len=280, required=False, newlines=True)
    members = body.get("members") or []
    if isinstance(members, (str, int)):
        members = [members]
    if len(name) < 2:
        raise BadRequest("نام گروه خیلی کوتاه است", code="NAME_TOO_SHORT")
    db, c = current_db(), conn()
    gid = db.insert(c, """INSERT INTO groups (name, creator_id, description, invite_code, created_at)
                          VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                   (name, me, description, secrets.token_hex(6)))
    db.insert(c, "INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, 'owner')", (gid, me))
    added = 0
    for raw in list(members)[:100]:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid == me or blocked_between(db, c, me, uid):
            continue
        if not db.query_one(c, "SELECT id FROM users WHERE id = ?", (uid,)):
            continue
        db.execute(c, db.ignore_clause("""INSERT INTO group_members (group_id, user_id, role, invited_by)
                         VALUES (?, ?, 'member', ?)""", on=["group_id", "user_id"]),
               (gid, uid, me)).close()
        added += 1
        notify(db, c, user_id=uid, actor_id=me, ntype="group_invite", target_id=gid,
               target_type="group", body=f"شما را به گروه «{name}» دعوت کرد", socketio=_sio())
    c.commit()
    _sio().emit("groups_changed", {})
    log.info("group_created", extra={"ctx": {"group_id": gid, "user_id": me, "members": added}})
    return jsonify({"success": True, "group_id": gid, "id": gid, "name": name,
                    "message": f"گروه «{name}» ساخته شد"}), 201


@mod.route("/<int:gid>", methods=["GET"], auth="user", rate=None)
def get_group(gid: int):
    return jsonify(_group_info(gid))


@mod.legacy("/group_info/<int:gid>", methods=("GET",), rate=None)
def legacy_group_info(gid: int):
    """Legacy returned the bare info dict."""
    return jsonify(_group_info(gid))


def _group_info(gid: int) -> dict:
    me = my_id()
    db, c = current_db(), conn()
    grp = db.query_one(c, """
        SELECT g.*, u.full_name AS creator_name, u.username AS creator_username, u.avatar AS creator_avatar
        FROM groups g JOIN users u ON u.id = g.creator_id WHERE g.id = ? AND g.deleted_at IS NULL""",
        (gid,))
    if grp is None:
        raise NotFound("گروه پیدا نشد")
    member = _membership(db, c, gid, me)
    if member is None and not int(grp.get("is_private") or 0) == 1 and not is_admin_user():
        raise Forbidden("عضو این گروه نیستی", code="NOT_A_MEMBER")
    members = db.query(c, """
        SELECT u.id, u.username, u.full_name, u.avatar, u.presence, u.last_seen_at,
               gm.role, gm.muted_until, gm.title, gm.joined_at
        FROM group_members gm JOIN users u ON u.id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY CASE gm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1
                              WHEN 'moderator' THEN 2 ELSE 3 END, u.full_name
        LIMIT 500""", (gid,))
    member_list = presence.stamp_many([dict(r) for r in members])
    perms = {a: (member or {}).get("role") in roles for a, roles in PERMISSIONS.items()}
    return {
        "success": True,
        "group": {k: v for k, v in dict(grp).items() if k != "creator_id"} | {
            "id": int(grp["id"]), "creator_id": int(grp["creator_id"]),
            "avatar_url": f"/files/profiles/{grp['avatar']}" if grp.get("avatar") else None},
        "members": member_list,
        "member_count": len(member_list),
        "my_role": (member or {}).get("role"),
        "permissions": {**perms, "send": bool(member) and not _muted(member)},
        "can_manage": can((member or {}).get("role"), "manage_members"),
    }


def _muted(member: dict) -> bool:
    until = member.get("muted_until")
    if not until:
        return False
    from ..auth import is_past
    return not is_past(until)


@mod.route("/<int:gid>/members", methods=["POST"], auth="user", rate=(60, 300),
           legacy="/group_add_member/<int:gid>", legacy_methods=("POST",), endpoint="group_add_member")
def group_add_member(gid: int):
    me = my_id()
    body = payload()
    db, c = current_db(), conn()
    if db.query_one(c, "SELECT id FROM groups WHERE id = ? AND deleted_at IS NULL", (gid,)) is None:
        raise NotFound("گروه پیدا نشد")
    _require(db, c, gid, me, "manage_members")
    target = int_arg("user_id", src=body)
    if target is None:
        name = text_field("username", body, max_len=32, required=False)
        row = db.query_one(c, "SELECT id FROM users WHERE username = ?", (name,)) if name else None
        if not row:
            raise NotFound("کاربر پیدا نشد")
        target = int(row["id"])
    if target == me:
        raise BadRequest("شما از قبل عضو هستید", code="ALREADY_MEMBER")
    if blocked_between(db, c, gid_owner_id(db, c, gid), target):
        raise Forbidden("امکان افزودن این کاربر وجود ندارد", code="BLOCKED")
    count = db.scalar(c, "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (gid,))
    limit_members = db.scalar(c, "SELECT COALESCE(max_members, 200) FROM groups WHERE id = ?", (gid,))
    if count >= int(limit_members or 200):
        raise Conflict("گروه پر است", code="GROUP_FULL")
    if db.query_one(c, "SELECT 1 AS x FROM group_bans WHERE group_id = ? AND user_id = ?", (gid, target)):
        raise Forbidden("این کاربر از گروه بن شده است", code="BANNED")
    db.execute(c, db.ignore_clause(
        "INSERT INTO group_members (group_id, user_id, role, invited_by) VALUES (?, ?, 'member', ?)",
        on=["group_id", "user_id"]), (gid, target, me)).close()
    c.commit()
    emit_group(_sio(), gid, "group_members_changed", {"group_id": gid})
    _sio().emit("groups_changed", {}, room=f"user:{target}")
    notify(db, c, user_id=target, actor_id=me, ntype="group_invite", target_id=gid,
           target_type="group", socketio=_sio())
    return jsonify({"success": True, "message": "عضو افزوده شد"})


def gid_owner_id(db, c, gid: int) -> int:
    row = db.query_one(c, "SELECT creator_id FROM groups WHERE id = ?", (gid,))
    return int(row["creator_id"]) if row else 0


@mod.route("/<int:gid>/members/<int:target>/remove", methods=["POST", "DELETE"],
           auth="user", rate=(60, 300), endpoint="group_remove_member")
def group_remove_member(gid: int, target: int):
    me = my_id()
    db, c = current_db(), conn()
    grp = db.query_one(c, "SELECT creator_id FROM groups WHERE id = ?", (gid,))
    if grp is None:
        raise NotFound("گروه پیدا نشد")
    mine = _membership(db, c, gid, me)
    theirs = _membership(db, c, gid, target)
    if theirs is None:
        raise NotFound("این کاربر عضو گروه نیست")
    if target == me:
        # leaving is always allowed for a plain member
        if mine and mine["role"] == "owner":
            raise Conflict("مالک نمی‌تواند خارج شود؛ گروه را واگذار یا حذف کند", code="OWNER_LEAVE")
        db.execute(c, "DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (gid, me)).close()
        db.execute(c, "DELETE FROM group_seen WHERE group_id = ? AND user_id = ?", (gid, me)).close()
        c.commit()
        emit_group(_sio(), gid, "group_members_changed", {"group_id": gid, "left": me})
        return jsonify({"success": True, "left": True})
    if not is_admin_user():
        if not can((mine or {}).get("role"), "kick"):
            raise Forbidden("مجاز به حذف عضو نیستید", code="NO_PERMISSION")
        # nobody below owner may touch the owner; and rank must be strictly higher
        if theirs["role"] == "owner" or role_rank(theirs["role"]) <= role_rank((mine or {}).get("role")):
            raise Forbidden("نمی‌توانید هم‌رتبه یا بالاتر را حذف کنید", code="RANK")
    db.execute(c, "DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (gid, target)).close()
    db.execute(c, "DELETE FROM group_seen WHERE group_id = ? AND user_id = ?", (gid, target)).close()
    c.commit()
    emit_group(_sio(), gid, "group_members_changed", {"group_id": gid, "removed": target})
    return jsonify({"success": True})


@mod.legacy("/group_remove_member/<int:gid>", methods=("POST",), rate=(60, 300))
def legacy_group_remove_member(gid: int):
    """Legacy took `user_id` in the body."""
    target = int_arg("user_id", src=payload()) or 0
    return group_remove_member(gid, int(target))


@mod.route("/<int:gid>", methods=["DELETE"], auth="user", rate=(10, 300),
           legacy="/group_delete/<int:gid>", legacy_methods=("DELETE",), endpoint="group_delete")
def group_delete(gid: int):
    me = my_id()
    db, c = current_db(), conn()
    grp = db.query_one(c, "SELECT creator_id FROM groups WHERE id = ? AND deleted_at IS NULL", (gid,))
    if grp is None:
        raise NotFound("گروه پیدا نشد")
    if int(grp["creator_id"]) != me and not is_admin_user():
        mine = _membership(db, c, gid, me)
        if not (mine and can(mine["role"], "delete_group")):
            raise Forbidden("فقط سازنده یا مدیر")
    # soft delete + hard membership removal; messages stay for audit
    db.execute(c, "UPDATE groups SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (gid,)).close()
    db.execute(c, "DELETE FROM group_members WHERE group_id = ?", (gid,)).close()
    c.commit()
    _sio().emit("groups_changed", {})
    return jsonify({"success": True, "message": "گروه حذف شد"})


@mod.route("/<int:gid>/messages", methods=["GET"], auth="user", rate=None)
def group_messages_endpoint(gid: int):
    limit, offset, page = pagination_args(default_size=50)
    rows, total = _group_messages(gid, limit=limit, offset=offset)
    return jsonify({"success": True, "messages": rows, **paginated(total, limit, offset, page)})


@mod.legacy("/group_messages/<int:gid>", methods=("GET",), rate=None)
def legacy_group_messages(gid: int):
    """Legacy contract: bare array ascending by id, capped at one page."""
    rows, _total = _group_messages(gid, limit=200, offset=0)
    return jsonify(rows)


def _group_messages(gid: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
    me = my_id()
    db, c = current_db(), conn()
    _require(db, c, gid, me)
    where = "m.group_id = ? AND m.deleted_for_everyone = 0"
    total = db.scalar(c, f"SELECT COUNT(*) FROM messages m WHERE {where}", (gid,))
    rows = db.query(c, f"""
        SELECT m.*, u.full_name AS sender_name, u.username AS sender_username, u.avatar AS sender_avatar,
               r.content AS reply_content, ru.full_name AS reply_sender_name,
               (SELECT COUNT(*) FROM message_reactions mr WHERE mr.message_id = m.id) AS reactions_count
        FROM messages m JOIN users u ON u.id = m.sender_id
        LEFT JOIN messages r ON r.id = m.reply_to_id
        LEFT JOIN users ru ON ru.id = r.sender_id
        WHERE {where} ORDER BY m.id DESC {db.limit_offset(limit, offset)}""", (gid,))
    out = [dict(r) for r in rows]
    out.reverse()
    for row in out:
        if row.get("file_path"):
            row["file_url"] = f"/files/chat/{row['file_path']}"
        row["can_delete"] = bool(row.get("sender_id") == me) or can(
            (_membership(db, c, gid, me) or {}).get("role"), "moderate_messages")
    return out, int(total)


@mod.route("/<int:gid>/seen", methods=["POST"], auth="user", rate=(120, 60))
def group_seen(gid: int):
    me = my_id()
    last_id = int_arg("last_message_id", src=payload(), lo=0) or 0
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?", (gid, me)):
        raise Forbidden("عضو این گروه نیستی")
    if not last_id:
        last_id = int(db.scalar(c, "SELECT COALESCE(MAX(id),0) FROM messages WHERE group_id = ?", (gid,)) or 0)
    db.execute(c, db.ignore_clause(
        "INSERT INTO group_seen (group_id, user_id, last_seen_id) VALUES (?, ?, ?)",
        on=["group_id", "user_id"], update=["last_seen_id"]), (gid, me, last_id)).close()
    c.commit()
    return jsonify({"success": True, "last_seen_id": last_id})


# --------------------------------------------------------------------------
# roles, mute, ban, invite
# --------------------------------------------------------------------------
@mod.route("/<int:gid>/members/<int:target>/role", methods=["POST"], auth="user", rate=(30, 300))
def set_role(gid: int, target: int):
    me = my_id()
    role = sanitize_text(payload().get("role"), max_len=16, strip_newlines=True) or ""
    if role not in ROLES:
        raise BadRequest("نقش نامعتبر است", code="BAD_ROLE", details={"allowed": list(ROLES)})
    db, c = current_db(), conn()
    _require(db, c, gid, me, "set_roles")
    if int(gid_owner_id(db, c, gid)) == target and role != "owner":
        raise Conflict("نمی‌توان نقش مالک را گرفت؛ ابتدا مالکیت را واگذار کنید", code="OWNER_STICKY")
    db.execute(c, "UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?",
               (role, gid, target)).close()
    c.commit()
    emit_group(_sio(), gid, "group_role_changed", {"group_id": gid, "user_id": target, "role": role})
    return jsonify({"success": True, "role": role})


@mod.route("/<int:gid>/members/<int:target>/mute", methods=["POST"], auth="user", rate=(40, 300))
def mute_member(gid: int, target: int):
    me = my_id()
    body = payload()
    minutes = int_arg("minutes", 10, src=body, lo=0, hi=60 * 24 * 30)
    db, c = current_db(), conn()
    _require(db, c, gid, me, "mute")
    if role_rank((_membership(db, c, gid, target) or {}).get("role")) <= role_rank(
            (_membership(db, c, gid, me) or {}).get("role")):
        raise Forbidden("نمی‌توانید هم‌رتبه یا بالاتر را بی‌صدا کنید", code="RANK")
    if minutes <= 0:
        db.execute(c, "UPDATE group_members SET muted_until = NULL WHERE group_id = ? AND user_id = ?",
                   (gid, target)).close()
    else:
        until = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes))
        db.execute(c, "UPDATE group_members SET muted_until = ? WHERE group_id = ? AND user_id = ?",
                   (until.strftime("%Y-%m-%d %H:%M:%S"), gid, target)).close()
    c.commit()
    notify(db, c, user_id=target, actor_id=me, ntype="moderation", target_id=gid,
           target_type="group", body=f"برای {minutes} دقیقه در گروه بی‌صدا شدید" if minutes else "بی‌صدایی شما برداشته شد",
           socketio=_sio())
    emit_group(_sio(), gid, "group_member_muted", {"group_id": gid, "user_id": target, "minutes": minutes})
    return jsonify({"success": True, "muted_until": None if minutes <= 0 else minutes})


@mod.route("/<int:gid>/members/<int:target>/ban", methods=["POST"], auth="user", rate=(30, 300))
def ban_member(gid: int, target: int):
    me = my_id()
    db, c = current_db(), conn()
    _require(db, c, gid, me, "ban")
    if int(gid_owner_id(db, c, gid)) == target:
        raise Forbidden("مالک گروه را نمی‌توان بن کرد", code="OWNER_PROTECTED")
    reason = text_field("reason", payload(), max_len=200, required=False)
    db.execute(c, db.ignore_clause(
        "INSERT INTO group_bans (group_id, user_id, banned_by, reason) VALUES (?, ?, ?, ?)",
        on=["group_id", "user_id"], update=["reason"]), (gid, target, me, reason)).close()
    db.execute(c, "DELETE FROM group_members WHERE group_id = ? AND user_id = ?", (gid, target)).close()
    c.commit()
    notify(db, c, user_id=target, actor_id=me, ntype="moderation", target_id=gid, target_type="group",
           body=f"از گروه حذف و بن شدید. {reason}".strip(), socketio=_sio())
    emit_group(_sio(), gid, "group_members_changed", {"group_id": gid, "banned": target})
    return jsonify({"success": True})


@mod.route("/<int:gid>/bans", auth="user", rate=None)
def list_bans(gid: int):
    db, c = current_db(), conn()
    _require(db, c, gid, my_id(), "manage_members")
    rows = db.query(c, """SELECT b.*, u.username, u.full_name FROM group_bans b
                          JOIN users u ON u.id = b.user_id WHERE b.group_id = ? ORDER BY b.id DESC LIMIT 100""",
                    (gid,))
    return jsonify({"success": True, "bans": [dict(r) for r in rows]})


@mod.route("/<int:gid>/unban/<int:target>", methods=["POST"], auth="user", rate=(30, 300))
def unban_member(gid: int, target: int):
    db, c = current_db(), conn()
    _require(db, c, gid, my_id(), "ban")
    db.execute(c, "DELETE FROM group_bans WHERE group_id = ? AND user_id = ?", (gid, target)).close()
    c.commit()
    return jsonify({"success": True})


@mod.route("/<int:gid>/invite", methods=["POST"], auth="user", rate=(20, 300))
def create_invite(gid: int):
    me = my_id()
    db, c = current_db(), conn()
    _require(db, c, gid, me, "invite")
    max_uses = int_arg("max_uses", 0, src=payload(), lo=0, hi=1000)
    hours = int_arg("hours", 72, src=payload(), lo=1, hi=24 * 90)
    code = secrets.token_urlsafe(10)
    exp = db.hours_ahead_sql(hours)
    iid = db.insert(c, f"""INSERT INTO group_invites (group_id, code, created_by, max_uses, expires_at)
                           VALUES (?, ?, ?, ?, {exp})""", (gid, code, me, max_uses))
    c.commit()
    return jsonify({"success": True, "invite_id": iid, "code": code,
                    "join_url": f"/api/groups/join/{code}", "expires_in_hours": hours})


@mod.route("/join/<code>", methods=["POST", "GET"], auth="user", rate=(20, 300))
def join_by_code(code: str):
    me = my_id()
    db, c = current_db(), conn()
    invite = db.query_one(c, """
        SELECT gi.*, g.deleted_at FROM group_invites gi JOIN groups g ON g.id = gi.group_id
        WHERE gi.code = ?""", (sanitize_text(code, max_len=48, strip_newlines=True),))
    if invite is None or invite.get("deleted_at"):
        raise NotFound("دعوت‌نامه نامعتبر است", code="BAD_INVITE")
    from ..auth import is_past
    if invite.get("expires_at") and is_past(invite["expires_at"]):
        raise Conflict("این دعوت‌نامه منقضی شده است", code="INVITE_EXPIRED")
    if int(invite.get("max_uses") or 0) and int(invite.get("uses") or 0) >= int(invite["max_uses"]):
        raise Conflict("ظرفیت این دعوت‌نامه پر شده است", code="INVITE_USED_UP")
    if db.query_one(c, "SELECT 1 AS x FROM group_bans WHERE group_id = ? AND user_id = ?",
                    (invite["group_id"], me)):
        raise Forbidden("شما در این گروه بن هستید", code="BANNED")
    db.execute(c, db.ignore_clause(
        "INSERT INTO group_members (group_id, user_id, role, invited_by) VALUES (?, ?, 'member', ?)",
        on=["group_id", "user_id"]), (invite["group_id"], me, invite["created_by"])).close()
    db.execute(c, "UPDATE group_invites SET uses = COALESCE(uses,0) + 1 WHERE id = ?", (invite["id"],)).close()
    c.commit()
    emit_group(_sio(), int(invite["group_id"]), "group_members_changed", {"group_id": invite["group_id"]})
    return jsonify({"success": True, "group_id": int(invite["group_id"]),
                    "message": "به گروه پیوستید"})


@mod.route("/<int:gid>", methods=["PATCH", "POST"], auth="user", rate=(20, 300),
           endpoint="update_group")
def update_group(gid: int):
    me = my_id()
    db, c = current_db(), conn()
    _require(db, c, gid, me, "edit_group")
    body = payload() if not request.files else payload(form=True)
    sets: list[str] = []
    args: list = []
    name = text_field("name", body, max_len=64, required=False)
    if name:
        sets.append("name = ?"); args.append(name)
    if "description" in body:
        sets.append("description = ?")
        args.append(text_field("description", body, max_len=280, required=False, newlines=True))
    for key, col in (("max_members", "max_members"), ("slow_mode_seconds", "slow_mode_seconds")):
        val = int_arg(key, src=body, lo=0 if key == "slow_mode_seconds" else 2,
                      hi=1000 if key == "slow_mode_seconds" else 5000)
        if val is not None:
            sets.append(f"{col} = ?"); args.append(val)
    for key, col in (("only_admins_post", "only_admins_post"), ("is_private", "is_private")):
        if key in body:
            sets.append(f"{col} = ?")
            args.append(1 if bool_arg(key, src=body) else 0)
    avatar = request.files.get("avatar")
    if avatar is not None and getattr(avatar, "filename", ""):
        saved = save_upload(avatar, user_id=me, category="profiles", kind="avatar")
        sets.append("avatar = ?"); args.append(saved.name)
    if not sets:
        raise BadRequest("چیزی برای تغییر نبود", code="NO_CHANGES")
    sets.append("updated_at = CURRENT_TIMESTAMP")
    db.execute(c, f"UPDATE groups SET {', '.join(sets)} WHERE id = ?", (*args, gid)).close()
    c.commit()
    emit_group(_sio(), gid, "group_updated", {"group_id": gid})
    return jsonify({"success": True, "message": "گروه به‌روزرسانی شد"})


@mod.route("/<int:gid>/leave", methods=["POST"], auth="user", rate=(20, 300))
def leave_group(gid: int):
    return group_remove_member(gid, my_id())


@mod.route("/<int:gid>/search", auth="user", rate=None)
def group_search(gid: int):
    q = sanitize_text(request.args.get("q"), max_len=60, strip_newlines=True)
    if len(q) < 2:
        raise BadRequest("حداقل ۲ کاراکتر", code="QUERY_TOO_SHORT")
    db, c = current_db(), conn()
    _require(db, c, gid, my_id())
    limit, offset, page = pagination_args(default_size=30)
    where = f"m.group_id = ? AND m.deleted_for_everyone = 0 AND {db.ilike('COALESCE(m.search_text, m.content)')}"
    params = [gid, *db.ilike_params(q)]
    total = db.scalar(c, f"SELECT COUNT(*) FROM messages m WHERE {where}", params)
    rows = db.query(c, f"""SELECT m.id, m.content, m.timestamp, m.sender_id, m.file_type,
                                  u.full_name AS sender_name
                           FROM messages m JOIN users u ON u.id = m.sender_id
                           WHERE {where} ORDER BY m.id DESC {db.limit_offset(limit, offset)}""", params)
    return jsonify({"success": True, "messages": [dict(r) for r in rows],
                    **paginated(total, limit, offset, page)})


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
