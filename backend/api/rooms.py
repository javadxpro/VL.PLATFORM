"""
Game rooms — the social lobby, deliberately separate from a game server.

Spec §17 is right that these are different things: a `lan_hosts` row describes
something you can connect *to*; a `game_rooms` row describes a group of people
organising to play, which may point at a server (`server_id`) or at nothing at
all ("we'll figure out the IP on voice"). A room can exist with no server and a
server with no room; joining a room therefore never claims to have joined a
game, and starting a room only publishes connection info the host supplied.
"""

from __future__ import annotations

from flask import jsonify, request

from . import (Module, bool_arg, conn, current_db, int_arg, is_admin_user,
               my_id, payload, text_field)
from .. import presence
from ..errors import BadRequest, Conflict, Forbidden, NotFound
from ..log import get_logger
from ..notify import emit_to_room_participants, notify
from ..security import (hash_password, pagination_args, paginated, sanitize_text,
                        verify_password)
from ..visibility import blocked_between, friends_of

mod = Module("rooms")
log = get_logger("api.rooms")

ROOM_OPEN = ("open", "starting", "ingame")
ROOM_VISIBILITY = ("public", "friends", "private", "invite")


def _cols() -> str:
    return """
        r.id, r.name, r.game_id, r.host_id, r.server_id, r.description, r.max_players,
        r.status, r.visibility, r.region, r.voice_enabled, r.created_at, r.updated_at,
        r.started_at, r.closed_at, r.expires_at, r.last_activity_at,
        (r.password_hash IS NOT NULL) AS is_locked,
        u.username AS host_username, u.full_name AS host_name, u.avatar AS host_avatar,
        COALESCE(g.name, 'بدون بازی') AS game_name, g.slug AS game_slug, g.icon AS game_icon,
        s.ip_address AS server_ip, s.port AS server_port, s.status AS server_status
    """


def _member_state(db, c, room_id: int, uid: int) -> dict | None:
    return db.query_one(c, """
        SELECT role, ready, mic_muted, joined_at FROM game_room_members
        WHERE room_id = ? AND user_id = ? AND left_at IS NULL""", (room_id, uid))


def _public(db, c, row: dict, viewer: int) -> dict:
    out = dict(row)
    room_id = int(out["id"])
    host = int(out.get("host_id") or 0)
    members = db.query(c, """
        SELECT m.user_id, m.role, m.ready, m.mic_muted, m.joined_at,
               u.username, u.full_name, u.avatar, u.presence
        FROM game_room_members m JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? AND m.left_at IS NULL
        ORDER BY CASE m.role WHEN 'host' THEN 0 ELSE 1 END, m.joined_at ASC
        LIMIT 64""", (room_id,))
    participants = presence.stamp_many([dict(r) for r in members])
    out.update({
        "participants": participants,
        "player_count": len(participants),
        "max_players": int(out.get("max_players") or 8),
        "is_locked": bool(out.get("is_locked")),
        "is_host": host == viewer,
        "is_member": any(int(p["user_id"]) == viewer for p in participants),
        "can_manage": host == viewer or is_admin_user(),
        "joinable": (out.get("status") or "") == "open"
                    and len(participants) < int(out.get("max_players") or 8),
        "full": len(participants) >= int(out.get("max_players") or 8),
        "game": out.get("game_name"),
        "host": out.get("host_name"),
    })
    # Connection info only while there is something to say and it is public-ish.
    if out.get("server_ip") and (out.get("server_status") in {None, "online", "full"}):
        out["connect"] = {"ip": out["server_ip"], "port": out["server_port"],
                         "status": out.get("server_status")}
    else:
        out["connect"] = None
    if not (out["is_host"] or out["is_member"] or is_admin_user()):
        out.pop("server_ip", None)
        out.pop("server_port", None)
    out.pop("password_hash", None)
    return out


def _touch(db, c, room_id: int) -> None:
    db.execute(c, f"UPDATE game_rooms SET last_activity_at = {db.now_sql()} WHERE id = ?", (room_id,)).close()


# --------------------------------------------------------------------------
# browse
# --------------------------------------------------------------------------
@mod.route("", methods=["GET"], auth="user", rate=None)
def list_rooms():
    me = my_id()
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=20)
    where, params = ["r.closed_at IS NULL"], []
    scope = sanitize_text(request.args.get("scope"), max_len=16, strip_newlines=True)
    if scope == "open":
        where.append("r.status = 'open'")
    elif scope == "mine":
        where.append("(r.host_id = ? OR EXISTS (SELECT 1 FROM game_room_members gm "
                     "WHERE gm.room_id = r.id AND gm.user_id = ? AND gm.left_at IS NULL))")
        params.extend([me, me])
    elif scope == "friends":
        friends = sorted(friends_of(db, c, me))
        if not friends:
            return jsonify({"success": True, "rooms": [],
                            "pagination": {"total": 0, "limit": limit, "offset": offset,
                                           "page": page, "pages": 1, "has_more": False}})
        marks = ", ".join("?" for _ in friends)
        where.append(f"r.host_id IN ({marks})")
        params.extend(friends)
    status = sanitize_text(request.args.get("status"), max_len=16, strip_newlines=True)
    if status in {"open", "starting", "ingame", "full", "closed"}:
        where.append("r.status = ?"); params.append(status)
    game_id = int_arg("game_id", src=request.args, lo=1)
    if game_id:
        where.append("r.game_id = ?"); params.append(game_id)
    q = sanitize_text(request.args.get("q"), max_len=48, strip_newlines=True)
    if q:
        where.append(f"({db.ilike('r.name')} OR "
                     f"{db.ilike('r.description')})")
        params.extend(db.ilike_params(q) * 2)
    clause = " AND ".join(where)
    total = db.scalar(c, f"""SELECT COUNT(*) FROM game_rooms r WHERE {clause}
                             AND r.visibility IN ('public','friends')""", params)
    rows = db.query(c, f"""
        SELECT {_cols()},
               (SELECT COUNT(*) FROM game_room_members gm
                 WHERE gm.room_id = r.id AND gm.left_at IS NULL) AS member_count
        FROM game_rooms r
        LEFT JOIN users u ON u.id = r.host_id
        LEFT JOIN games g ON g.id = r.game_id
        LEFT JOIN lan_hosts s ON s.id = r.server_id
        WHERE {clause} AND r.visibility IN ('public','friends')
        ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'starting' THEN 1
                               WHEN 'ingame' THEN 2 ELSE 3 END,
                 r.last_activity_at DESC, r.id DESC
        {db.limit_offset(limit, offset)}""", params)
    out = []
    for r in rows:
        d = dict(r)
        if int(d.get("host_id") or 0) == me:
            out.append(_public(db, c, d, me))
            continue
        if d.get("visibility") == "friends" and int(d["host_id"]) not in friends_of(db, c, me):
            continue
        out.append(_public(db, c, d, me))
    return jsonify({"success": True, "rooms": out, **paginated(total, limit, offset, page)})


@mod.route("/<int:rid>", methods=["GET"], auth="user", rate=None)
def room_detail(rid: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, f"""
        SELECT {_cols()} FROM game_rooms r
        LEFT JOIN users u ON u.id = r.host_id
        LEFT JOIN games g ON g.id = r.game_id
        LEFT JOIN lan_hosts s ON s.id = r.server_id WHERE r.id = ?""", (rid,))
    if row is None:
        raise NotFound("اتاق پیدا نشد")
    d = dict(row)
    member = _member_state(db, c, rid, me)
    if d.get("visibility") in {"private", "invite"} and member is None and int(d.get("host_id") or 0) != me:
        invite = db.query_one(c, "SELECT 1 AS x FROM game_room_invites WHERE room_id = ? AND to_user_id = ? "
                                  "AND state = 'pending'", (rid, me))
        if not invite:
            raise Forbidden("این اتاق خصوصی است", code="PRIVATE_ROOM")
    out = _public(db, c, d, me)
    out["my_state"] = dict(member) if member else None
    out["invites"] = [dict(r) for r in db.query(c, """
        SELECT i.*, u.full_name AS to_name, u.username AS to_username FROM game_room_invites i
        JOIN users u ON u.id = i.to_user_id
        WHERE i.room_id = ? AND i.state = 'pending' ORDER BY i.id DESC LIMIT 30""", (rid,))] \
        if (int(d.get("host_id") or 0) == me or is_admin_user()) else []
    return jsonify({"success": True, "room": out})


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
@mod.route("", methods=["POST"], auth="user", rate=(10, 600), endpoint="create")
def create_room():
    """
    Create Game Room → select game → (optionally) bind a server → publish.

    Every field the spec lists is stored; a room may legally have no server, in
    which case `connect` stays null rather than pointing at a guess.
    """
    me = my_id()
    body = payload()
    name = text_field("name", body, max_len=64)
    if len(name) < 3:
        raise BadRequest("نام اتاق باید حداقل ۳ کاراکتر باشد", code="NAME_TOO_SHORT")
    game_id = int_arg("game_id", src=body, lo=1)
    server_id = int_arg("server_id", src=body, lo=1)
    description = text_field("description", body, max_len=280, required=False, newlines=True)
    region = text_field("region", body, max_len=40, required=False)
    max_players = int_arg("max_players", 8, src=body, lo=2, hi=64)
    visibility = sanitize_text(body.get("visibility"), max_len=16, strip_newlines=True) or "public"
    if visibility not in ROOM_VISIBILITY:
        raise BadRequest("دید اتاق نامعتبر است", code="BAD_VISIBILITY")
    password = str(body.get("password") or "")[:64]
    ttl_minutes = int_arg("ttl_minutes", 0, src=body, lo=0, hi=60 * 72)
    voice = 1 if bool_arg("voice_enabled", True, src=body) else 0
    auto_close_hours = (ttl_minutes / 60) if ttl_minutes else None

    db, c = current_db(), conn()
    if game_id and not db.query_one(c, "SELECT id FROM games WHERE id = ?", (game_id,)):
        raise NotFound("بازی در کاتالوگ نیست", code="GAME_NOT_FOUND")
    if server_id:
        srv = db.query_one(c, "SELECT user_id, status FROM lan_hosts WHERE id = ?", (server_id,))
        if srv is None:
            raise NotFound("سرور انتخابی پیدا نشد", code="SERVER_NOT_FOUND")
        if int(srv["user_id"]) != me and not is_admin_user():
            raise Forbidden("فقط سرور خودتان را می‌توانید به اتاق وصل کنید", code="SERVER_NOT_YOURS")

    rid = db.insert(c, """
        INSERT INTO game_rooms (name, game_id, host_id, server_id, description, max_players,
                                status, visibility, region, password_hash, voice_enabled,
                                created_at, last_activity_at)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (name, game_id, me, server_id, description, max_players, visibility, region,
         hash_password(password) if password else None, voice))
    if auto_close_hours:
        db.execute(c, f"UPDATE game_rooms SET expires_at = {db.hours_ahead_sql(auto_close_hours)} "
                      f"WHERE id = ?", (rid,)).close()
    db.insert(c, "INSERT INTO game_room_members (room_id, user_id, role) VALUES (?, ?, 'host')", (rid, me))
    db.execute(c, """UPDATE gaming_profiles SET rooms_created = COALESCE(rooms_created,0) + 1
                     WHERE user_id = ?""", (me,)).close()
    c.commit()
    from .gaming import _activity
    _activity(db, c, me, "room_create", game_id=game_id, room_id=rid)
    _sio().emit("rooms_changed", {"room_id": rid, "action": "created"})
    log.info("room_created", extra={"ctx": {"room_id": rid, "user_id": me}})
    row = db.query_one(c, f"SELECT {_cols()} FROM game_rooms r LEFT JOIN users u ON u.id = r.host_id "
                          f"LEFT JOIN games g ON g.id = r.game_id WHERE r.id = ?", (rid,))
    return jsonify({"success": True, "id": rid, "room": _public(db, c, dict(row or {}), me),
                    "message": f"اتاق «{name}» ساخته شد"}), 201


@mod.route("/<int:rid>/join", methods=["POST"], auth="user", rate=(30, 300))
def join_room_endpoint(rid: int):
    me = my_id()
    db, c = current_db(), conn()
    room = db.query_one(c, """SELECT id, host_id, status, max_players, password_hash, visibility,
                                     name FROM game_rooms WHERE id = ?""", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    if room.get("status") == "closed":
        raise Conflict("این اتاق بسته شده است", code="ROOM_CLOSED")
    if int(room["host_id"]) == me:
        raise Conflict("شما میزبان این اتاق هستید", code="ALREADY_MEMBER")
    existing = _member_state(db, c, rid, me)
    if existing:
        return jsonify({"success": True, "already": True, "room_id": rid,
                        "message": "از قبل در اتاق هستید"})
    if room.get("visibility") in {"private", "invite"}:
        invited = db.query_one(c, """SELECT id FROM game_room_invites
                                     WHERE room_id = ? AND to_user_id = ? AND state IN ('pending','accepted')""",
                               (rid, me))
        if not invited:
            raise Forbidden("این اتاق فقط با دعوت است", code="INVITE_ONLY")
        if invited and room.get("visibility") == "private":
            db.execute(c, "UPDATE game_room_invites SET state = 'accepted', responded_at = CURRENT_TIMESTAMP "
                          "WHERE id = ?", (invited["id"],)).close()
    if room.get("visibility") == "friends" and int(room["host_id"]) not in ({me} | friends_of(db, c, me)):
        raise Forbidden("اتاق فقط برای دوستان است", code="FRIENDS_ONLY")
    if blocked_between(db, c, me, int(room["host_id"])):
        raise Forbidden("امکان پیوستن وجود ندارد", code="BLOCKED")
    count = db.scalar(c, "SELECT COUNT(*) FROM game_room_members WHERE room_id = ? AND left_at IS NULL", (rid,))
    if int(count) >= int(room.get("max_players") or 8):
        raise Conflict("ظرفیت اتاق پر است", code="ROOM_FULL")
    if room.get("password_hash"):
        supplied = str(payload().get("password") or "")[:64]
        if not supplied:
            raise Forbidden("این اتاق رمز دارد", code="PASSWORD_REQUIRED",
                            details={"requires": "password"})
        if not verify_password(supplied, room["password_hash"]):
            raise Forbidden("رمز اتاق اشتباه است", code="BAD_ROOM_PASSWORD")
    db.execute(c, """INSERT INTO game_room_members (room_id, user_id, role) VALUES (?, ?, 'member')
                     ON CONFLICT (room_id, user_id) DO UPDATE SET left_at = NULL,
                                                                  joined_at = CURRENT_TIMESTAMP""",
               (rid, me)).close()
    prev = int(room.get("max_players") or 8)
    new_count = int(count) + 1
    if new_count >= prev and room.get("status") == "open":
        db.execute(c, "UPDATE game_rooms SET status = 'full' WHERE id = ?", (rid,)).close()
        notify(db, c, user_id=int(room["host_id"]), actor_id=me, ntype="room_full", target_id=rid,
               room_id=rid, target_type="room", body=f"اتاق «{room['name']}» پر شد", socketio=_sio())
    else:
        db.execute(c, "UPDATE game_rooms SET status = 'open' WHERE id = ? AND status = 'full'", (rid,)).close()
    _touch(db, c, rid)
    notify(db, c, user_id=int(room["host_id"]), actor_id=me, ntype="room_join", target_id=rid,
           room_id=rid, target_type="room", body="به اتاق شما پیوست", socketio=_sio())
    db.execute(c, """UPDATE gaming_profiles SET servers_joined = COALESCE(servers_joined,0) + 1
                     WHERE user_id = ?""", (me,)).close()
    from .gaming import _activity
    _activity(db, c, me, "room_join", room_id=rid)
    c.commit()
    _sio().emit("room_member_joined", {"room_id": rid, "user_id": me, "players": new_count},
                room=f"game_room:{rid}")
    _sio().emit("rooms_changed", {"room_id": rid, "action": "join"})
    return jsonify({"success": True, "room_id": rid, "players": new_count,
                    "voice_enabled": 1, "message": "به اتاق پیوستید"})


@mod.route("/<int:rid>/leave", methods=["POST"], auth="user", rate=(30, 300))
def leave_room_endpoint(rid: int):
    me = my_id()
    db, c = current_db(), conn()
    room = db.query_one(c, "SELECT id, host_id, max_players, status FROM game_rooms WHERE id = ?", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    member = _member_state(db, c, rid, me)
    if not member:
        raise Conflict("در این اتاق نیستید", code="NOT_MEMBER")
    is_host = int(room["host_id"]) == me
    if is_host:
        # Host leaving ends the lobby rather than orphaning it.
        db.execute(c, """UPDATE game_room_members SET left_at = CURRENT_TIMESTAMP
                         WHERE room_id = ? AND left_at IS NULL""", (rid,)).close()
        db.execute(c, f"""UPDATE game_rooms SET status = 'closed', closed_at = {db.now_sql()}
                          WHERE id = ?""", (rid,)).close()
        _sio().emit("room_closed", {"room_id": rid, "by": me}, room=f"game_room:{rid}")
    else:
        db.execute(c, """UPDATE game_room_members SET left_at = CURRENT_TIMESTAMP
                         WHERE room_id = ? AND user_id = ?""", (rid, me)).close()
        count = db.scalar(c, "SELECT COUNT(*) FROM game_room_members WHERE room_id = ? AND left_at IS NULL", (rid,))
        if room.get("status") == "full" and count < int(room.get("max_players") or 8):
            db.execute(c, "UPDATE game_rooms SET status = 'open' WHERE id = ?", (rid,)).close()
        notify(db, c, user_id=int(room["host_id"]), actor_id=me, ntype="room_leave", target_id=rid,
               room_id=rid, target_type="room", body="از اتاق شما خارج شد", socketio=_sio())
    _touch(db, c, rid)
    c.commit()
    _sio().emit("room_member_left", {"room_id": rid, "user_id": me}, room=f"game_room:{rid}")
    _sio().emit("rooms_changed", {"room_id": rid, "action": "leave"})
    return jsonify({"success": True, "closed_room": is_host,
                    "message": "اتاق بسته شد" if is_host else "از اتاق خارج شدید"})


@mod.route("/<int:rid>/ready", methods=["POST"], auth="user", rate=(60, 60))
def set_ready(rid: int):
    me = my_id()
    ready = 1 if bool_arg("ready", True, src=payload()) else 0
    db, c = current_db(), conn()
    member = _member_state(db, c, rid, me)
    if member is None:
        raise Forbidden("عضو این اتاق نیستید", code="NOT_MEMBER")
    db.execute(c, "UPDATE game_room_members SET ready = ? WHERE room_id = ? AND user_id = ? AND left_at IS NULL",
               (ready, rid, me)).close()
    _touch(db, c, rid)
    c.commit()
    emit_to_room_participants(_sio(), rid, "room_ready", {"room_id": rid, "user_id": me, "ready": bool(ready)})
    return jsonify({"success": True, "ready": bool(ready)})


@mod.route("/<int:rid>/invite", methods=["POST"], auth="user", rate=(30, 300))
def invite(rid: int):
    """Invite friends (spec §18: “Invite to game”)."""
    me = my_id()
    body = payload()
    db, c = current_db(), conn()
    room = db.query_one(c, "SELECT id, host_id, name, status FROM game_rooms WHERE id = ?", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    is_member = _member_state(db, c, rid, me)
    if int(room["host_id"]) != me and is_member is None:
        raise Forbidden("فقط میزبان یا اعضا می‌توانند دعوت کنند", code="NOT_MEMBER")
    if room.get("status") == "closed":
        raise Conflict("اتاق بسته است", code="ROOM_CLOSED")
    targets = body.get("user_ids") or body.get("user_id") or []
    if isinstance(targets, (int, str)):
        targets = [targets]
    friends = friends_of(db, c, me)
    invited, skipped = [], []
    for raw in list(targets)[:20]:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid == me or (friends and uid not in friends):
            skipped.append({"user_id": uid, "reason": "not_a_friend" if friends else "self"})
            continue
        if blocked_between(db, c, me, uid):
            skipped.append({"user_id": uid, "reason": "blocked"})
            continue
        # Same upsert text on both engines: SQLite >= 3.24 supports ON CONFLICT DO UPDATE.
        db.execute(c, """INSERT INTO game_room_invites (room_id, from_user_id, to_user_id, state)
                         VALUES (?, ?, ?, 'pending')
                         ON CONFLICT (room_id, to_user_id) DO UPDATE SET state = 'pending',
                             created_at = CURRENT_TIMESTAMP, responded_at = NULL""",
                   (rid, me, uid)).close()
        notify(db, c, user_id=uid, actor_id=me, ntype="game_invite", target_id=rid, room_id=rid,
               target_type="room", body=f"به اتاق «{room['name']}» دعوت شدید", socketio=_sio())
        emit_to_room_participants(_sio(), rid, "room_invite_sent", {"room_id": rid, "to": uid})
        invited.append(uid)
    db.execute(c, """UPDATE gaming_profiles SET invites_sent = COALESCE(invites_sent,0) + ?
                     WHERE user_id = ?""", (len(invited), me)).close()
    _touch(db, c, rid)
    c.commit()
    return jsonify({"success": True, "invited": invited, "skipped": skipped,
                    "message": f"{len(invited)} دعوت ارسال شد"})


@mod.route("/<int:rid>/kick", methods=["POST"], auth="user", rate=(30, 300))
def kick(rid: int):
    me = my_id()
    target = int_arg("user_id", src=payload(), lo=1) or 0
    db, c = current_db(), conn()
    room = db.query_one(c, "SELECT host_id FROM game_rooms WHERE id = ?", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    if int(room["host_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان", code="NOT_HOST")
    if target == me:
        raise BadRequest("خودت را نمی‌توانی اخراج کنی", code="SELF_KICK")
    cur = db.execute(c, """UPDATE game_room_members SET left_at = CURRENT_TIMESTAMP
                           WHERE room_id = ? AND user_id = ? AND left_at IS NULL AND role != 'host'""",
                     (rid, target))
    n = cur.rowcount
    cur.close()
    count = db.scalar(c, "SELECT COUNT(*) FROM game_room_members WHERE room_id = ? AND left_at IS NULL", (rid,))
    db.execute(c, "UPDATE game_rooms SET status = 'open' WHERE id = ? AND status = 'full'", (rid,)).close()
    db.execute(c, "UPDATE game_room_invites SET state = 'declined' WHERE room_id = ? AND to_user_id = ? "
                  "AND state = 'pending'", (rid, target)).close()
    _touch(db, c, rid)
    c.commit()
    notify(db, c, user_id=target, actor_id=me, ntype="moderation", target_id=rid, room_id=rid,
           target_type="room", body="از اتاق بازی بیرون انداخته شدید", socketio=_sio())
    emit_to_room_participants(_sio(), rid, "room_member_left", {"room_id": rid, "user_id": target,
                                                                 "kicked": True})
    return jsonify({"success": True, "removed": int(n), "players": int(count)})


@mod.route("/<int:rid>/start", methods=["POST"], auth="user", rate=(20, 300))
def start(rid: int):
    """
    'Start game' does exactly three things: flips status, stamps the time, tells
    members. It cannot launch a process and does not pretend to.
    """
    me = my_id()
    db, c = current_db(), conn()
    room = db.query_one(c, """SELECT r.id, r.host_id, r.status, r.server_id, r.name,
                                     s.ip_address, s.port
                              FROM game_rooms r LEFT JOIN lan_hosts s ON s.id = r.server_id
                              WHERE r.id = ?""", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    if int(room["host_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان می‌تواند بازی را شروع کند", code="NOT_HOST")
    if room.get("status") == "closed":
        raise Conflict("اتاق بسته است", code="ROOM_CLOSED")
    db.execute(c, f"""UPDATE game_rooms SET status = 'ingame', started_at = {db.now_sql()}
                      WHERE id = ?""", (rid,)).close()
    if room.get("server_id"):
        db.execute(c, f"""UPDATE lan_hosts SET status = 'starting',
                          last_status_change = {db.now_sql()} WHERE id = ? AND status != 'online'""",
                   (room["server_id"],)).close()
    _touch(db, c, rid)
    members = [int(r["user_id"]) for r in db.query(c, """
        SELECT user_id FROM game_room_members WHERE room_id = ? AND left_at IS NULL""", (rid,))]
    for uid in members:
        if uid == me:
            continue
        notify(db, c, user_id=uid, actor_id=me, ntype="room_start", target_id=rid, room_id=rid,
               target_type="room", body="بازی شروع شد", socketio=_sio())
    from .gaming import _activity
    _activity(db, c, me, "room_start", room_id=rid)
    c.commit()
    emit_to_room_participants(_sio(), rid, "room_started", {
        "room_id": rid, "by": me, "name": room.get("name"),
        "connect": {"ip": room.get("ip_address"), "port": room.get("port")} if room.get("ip_address") else None,
    })
    return jsonify({"success": True, "status": "ingame",
                    "connect": {"ip": room.get("ip_address"), "port": room.get("port")}
                    if room.get("ip_address") else None,
                    "message": "بازی شروع شد 🎮"})


@mod.route("/<int:rid>/stop", methods=["POST"], auth="user", rate=(20, 300))
def stop(rid: int):
    me = my_id()
    db, c = current_db(), conn()
    room = db.query_one(c, "SELECT host_id, status FROM game_rooms WHERE id = ?", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    if int(room["host_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان", code="NOT_HOST")
    db.execute(c, "UPDATE game_rooms SET status = 'open' WHERE id = ?", (rid,)).close()
    _touch(db, c, rid)
    c.commit()
    emit_to_room_participants(_sio(), rid, "room_stopped", {"room_id": rid, "by": me})
    return jsonify({"success": True, "status": "open"})


@mod.route("/<int:rid>/close", methods=["POST"], auth="user", rate=(20, 300))
def close(rid: int):
    me = my_id()
    db, c = current_db(), conn()
    room = db.query_one(c, "SELECT host_id FROM game_rooms WHERE id = ?", (rid,))
    if room is None:
        raise NotFound("اتاق پیدا نشد")
    if int(room["host_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان یا مدیر", code="NOT_HOST")
    db.execute(c, """UPDATE game_room_members SET left_at = CURRENT_TIMESTAMP
                     WHERE room_id = ? AND left_at IS NULL""", (rid,)).close()
    db.execute(c, f"""UPDATE game_rooms SET status = 'closed', closed_at = {db.now_sql()}
                      WHERE id = ?""", (rid,)).close()
    c.commit()
    _sio().emit("rooms_changed", {"room_id": rid, "action": "closed"})
    _sio().emit("room_closed", {"room_id": rid}, room=f"game_room:{rid}")
    return jsonify({"success": True, "message": "اتاق بسته شد"})


@mod.route("/<int:rid>/messages", methods=["GET", "POST"], auth="user",
           rate=None, endpoint="room_messages")
def room_messages(rid: int):
    """Lobby chat. Reads paginated; writes are membership-checked (see realtime.py)."""
    me = my_id()
    db, c = current_db(), conn()
    member = _member_state(db, c, rid, me)
    if member is None:
        raise Forbidden("عضو این اتاق نیستید", code="NOT_MEMBER")
    if request.method == "POST":
        from ..errors import BadRequest as _BR
        content = text_field("content", payload(), max_len=1000)
        if not content:
            raise _BR("متن خالی است", code="EMPTY")
        mid = db.insert(c, "INSERT INTO room_messages (room_id, user_id, content) VALUES (?, ?, ?)",
                        (rid, me, content))
        _touch(db, c, rid)
        row = db.query_one(c, """SELECT m.*, u.full_name AS sender_name, u.username, u.avatar
                                 FROM room_messages m JOIN users u ON u.id = m.user_id WHERE m.id = ?""", (mid,))
        c.commit()
        emit_to_room_participants(_sio(), rid, "room_message", dict(row or {"id": mid}))
        return jsonify({"success": True, "message": dict(row or {})}), 201
    limit, offset, page = pagination_args(default_size=50)
    total = db.scalar(c, "SELECT COUNT(*) FROM room_messages WHERE room_id = ?", (rid,))
    rows = db.query(c, f"""
        SELECT m.*, u.full_name AS sender_name, u.username, u.avatar
        FROM room_messages m JOIN users u ON u.id = m.user_id
        WHERE m.room_id = ? ORDER BY m.id DESC {db.limit_offset(limit, offset)}""", (rid,))
    out = [dict(r) for r in rows]
    out.reverse()
    return jsonify({"success": True, "messages": out, **paginated(total, limit, offset, page)})


@mod.route("/invites", methods=["GET", "POST"], auth="user", rate=None, endpoint="invites")
def invites():
    me = my_id()
    db, c = current_db(), conn()
    if request.method == "POST":
        body = payload()
        rid = int_arg("room_id", src=body, lo=1) or 0
        accept = bool_arg("accept", True, src=body)
        row = db.query_one(c, """SELECT id, state, room_id FROM game_room_invites
                                 WHERE room_id = ? AND to_user_id = ? AND state = 'pending'""", (rid, me))
        if row is None:
            raise NotFound("دعوتی وجود ندارد", code="NO_INVITE")
        db.execute(c, f"""UPDATE game_room_invites SET state = ?, responded_at = {db.now_sql()}
                          WHERE id = ?""", ("accepted" if accept else "declined", row["id"])).close()
        c.commit()
        return jsonify({"success": True, "state": "accepted" if accept else "declined"})
    rows = db.query(c, """
        SELECT i.id AS invite_id, i.state, i.created_at, r.id AS room_id, r.name AS room_name,
               r.status, r.max_players, g.name AS game_name, g.slug AS game_slug,
               u.id AS host_id, u.full_name AS host_name, u.avatar AS host_avatar
        FROM game_room_invites i
        JOIN game_rooms r ON r.id = i.room_id
        LEFT JOIN games g ON g.id = r.game_id
        JOIN users u ON u.id = i.from_user_id
        WHERE i.to_user_id = ? AND i.state = 'pending' ORDER BY i.id DESC LIMIT 30""", (me,))
    return jsonify({"success": True, "invites": [dict(r) for r in rows]})


@mod.route("/mine", auth="user", rate=None)
def mine():
    me = my_id()
    db, c = current_db(), conn()
    rows = db.query(c, f"""
        SELECT {_cols()} FROM game_rooms r
        LEFT JOIN users u ON u.id = r.host_id
        LEFT JOIN games g ON g.id = r.game_id
        WHERE r.host_id = ? OR EXISTS (SELECT 1 FROM game_room_members gm
                                        WHERE gm.room_id = r.id AND gm.user_id = ? AND gm.left_at IS NULL)
        ORDER BY r.closed_at IS NOT NULL, r.id DESC LIMIT 40""", (me, me))
    return jsonify({"success": True, "rooms": [_public(db, c, dict(r), me) for r in rows]})


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
