"""
Socket.IO layer.

The pre-upgrade build trusted the client for identity on every event
(docs/AUDIT.md §3/§7). Here:

  * a connection is authenticated from the handshake (token in
    `auth.token` / `auth` query / `vx_session` cookie); anonymous sockets are
    rejected outright
  * identity comes from that session — `user_id`, `sender`, `from` fields in a
    payload are *ignored*, never authoritative
  * every room join is membership-checked, including WebRTC signaling, so a
    stranger cannot inject an offer into someone else's call
  * private traffic is emitted to the two humans involved, not to the world

Event map (old → new) lives in docs/websocket.md. Names are unchanged where an
old client is still in the wild; `join` still works but now derives identity
server-side.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from flask import request
from flask_socketio import emit, join_room, leave_room

from .config import get_config
from .db import Database
from .log import get_logger
from .notify import emit_to_user, user_room
from .security import client_ip, sanitize_text, throttle
from . import presence

log = get_logger("realtime")

DEFAULT_VOICE_ROOM = "global_voice"
#: voice rooms any authenticated member may join (legacy default behaviour)
OPEN_VOICE_ROOMS = {DEFAULT_VOICE_ROOM, "global", "lobby"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _db() -> Database:
    from flask import current_app
    return current_app.extensions["volexturn_db"]


def _socketio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]


def _handshake_token() -> str | None:
    """auth.token (socket.io v4) > auth.query > websocket subprotocol > cookie."""
    auth = request.auth if hasattr(request, "auth") else None
    if isinstance(auth, dict):
        tok = auth.get("token") or auth.get("access_token")
        if tok:
            return str(tok)[:128]
    for key in ("token", "access_token"):
        tok = request.args.get(key)
        if tok:
            return str(tok)[:128]
    hdr = request.headers.get("Authorization", "")
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip()[:128]
    cookie = request.cookies.get("vx_session")
    return str(cookie)[:128] if cookie else None


def authed(fn):
    """Decorator: run the handler only for an identified, unbanned user."""
    import functools

    @functools.wraps(fn)
    def wrapper(data: Any = None, *args, **kwargs):
        user = _USER_BY_SID.get(request.sid)
        if user is None:
            emit("error", {"code": "UNAUTHORIZED",
                           "message": "نشست معتبر نیست — دوباره وارد شوید"})
            return {"success": False, "error": "unauthorized"}
        return fn(user, data if data is not None else {}, *args, **kwargs)

    return wrapper


#: sid -> user row (session-scoped). Cleared on disconnect.
_USER_BY_SID: dict[str, dict] = {}
#: sid -> joined voice room, for cleanup on abrupt disconnect
_VOICE_BY_SID: dict[str, str] = {}
#: room key -> sids currently in it (our own registry; no adapter internals)
_ROOM_MEMBERS: dict[str, set[str]] = defaultdict(set)


def _membership_ok(user: dict, room: str) -> bool:
    """
    Authorisation for any named room: `voice:x`, `group:<id>`, `game_room:<id>`.
    """
    uid = int(user["id"])
    db = _db()
    conn = db.connect()
    try:
        if room.startswith("group:"):
            gid = _as_int(room.split(":", 1)[1])
            if gid is None:
                return False
            return bool(db.query_one(conn,
                "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?", (gid, uid)))
        if room.startswith("game_room:"):
            rid = _as_int(room.split(":", 1)[1])
            if rid is None:
                return False
            row = db.query_one(conn, """
                SELECT host_id FROM game_rooms WHERE id = ? AND status != 'closed'""", (rid,))
            if row is None:
                return False
            if int(row["host_id"]) == uid:
                return True
            return bool(db.query_one(conn, """
                SELECT 1 AS x FROM game_room_members
                 WHERE room_id = ? AND user_id = ? AND left_at IS NULL""", (rid, uid)))
        if room.startswith("voice:"):
            name = room.split(":", 1)[1]
            if name in OPEN_VOICE_ROOMS:
                return True                     # the app's shared lobby call
            # a named voice room maps to a game room or a group
            if name.startswith("room-"):
                rid = _as_int(name.split("-", 1)[1])
                if rid is None:
                    return False
            elif name.startswith("group-"):
                gid = _as_int(name.split("-", 1)[1])
                if gid is None:
                    return False
                return bool(db.query_one(conn,
                    "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?", (gid, uid)))
            else:
                return False
            return _membership_ok(user, f"game_room:{rid}")
        return False
    finally:
        db.close(conn)


def _as_int(raw: Any) -> int | None:
    try:
        n = int(str(raw).strip())
        return n if 0 < n < 10**12 else None
    except (TypeError, ValueError):
        return None


def _room_of(raw: Any, *, default: str = DEFAULT_VOICE_ROOM) -> str:
    """Normalise a client-supplied room name into a namespaced socketio room."""
    name = sanitize_text(raw, max_len=48) or default
    if ":" in name or name.startswith("user-"):
        # `user-<id>` was the legacy DM voice tag; map it onto the DM room pair.
        other = _as_int(name.split("-", 1)[1]) if name.startswith("user-") else None
        return f"voice:{default}" if other is None else f"voice:dm-{other}"
    if name.startswith("voice-") or name.startswith("room-") or name.startswith("group-"):
        return f"voice:{name}"
    return f"voice:{name}"


def _voice_room_key(raw: Any, uid: int | None = None) -> str:
    """
    Final voice room key.

    A `dm-<peer>` style name is folded into a *shared pair* key so two peers land
    in the same tiny room and cannot listen in on somebody else's call — the old
    build used one global room for everything.
    """
    name = sanitize_text(raw, max_len=48) or DEFAULT_VOICE_ROOM
    base = name.split(":", 1)[-1]
    if base.startswith("dm-") and uid is not None:
        peer = _as_int(base.split("-", 1)[1])
        if peer:
            lo, hi = sorted((int(uid), peer))
            return f"voice:dm-{lo}_{hi}"
    if base in OPEN_VOICE_ROOMS or base in {"global", "lobby", ""}:
        return f"voice:{DEFAULT_VOICE_ROOM}"
    if base.startswith(("room-", "group-")):
        return f"voice:{base}"
    return f"voice:{DEFAULT_VOICE_ROOM}"


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------
def register_realtime(socketio, app) -> None:
    """Wire every handler. Called once from create_app()."""

    @socketio.on("connect")
    def on_connect(auth=None):
        """
        Identity is resolved from the token alone. A socket with no valid
        session is refused, so no anonymous listener can harvest traffic.
        """
        token = None
        if isinstance(auth, dict):
            token = auth.get("token") or auth.get("access_token")
        if not token:
            token = _handshake_token()
        from .auth import authenticate
        db = _db()
        try:
            with app.app_context():
                user = authenticate(db=db, token=str(token) if token else None)
        except Exception as exc:
            log.info("socket_connect_rejected", extra={"ctx": {"err": type(exc).__name__}})
            return False
        if user is None:
            log.info("socket_connect_unauthorized", extra={"ctx": {"ip": client_ip()}})
            return False
        uid = int(user["id"])
        _USER_BY_SID[request.sid] = {
            "id": uid, "username": user.get("username"), "full_name": user.get("full_name"),
            "role": user.get("role") or "user",
        }
        _USER_BY_SID[request.sid]["_uid"] = uid
        join_room(user_room(uid))
        _join_groups_and_rooms(request.sid, uid)
        came_online = presence.attach(uid, request.sid)
        _persist_presence(uid, "online")
        if came_online:
            _broadcast_presence()
        emit("ready", {
            "user_id": uid, "username": user.get("username"),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": get_config().app_version,
            "rooms": sorted(_VOICE_BY_SID.get(request.sid, [])),
        })
        log.info("socket_connected", extra={"ctx": {"user_id": uid}})
        return True

    @socketio.on("disconnect")
    def on_disconnect():
        user = _USER_BY_SID.pop(request.sid, None)
        voice = _VOICE_BY_SID.pop(request.sid, None)
        if voice:
            leave_room(voice)
            _ROOM_MEMBERS.get(voice, set()).discard(request.sid)
        if not user:
            return
        uid = int(user["id"])
        if presence.detach(uid, request.sid):
            _persist_presence(uid, "offline")
            _broadcast_presence()
        log.info("socket_disconnected", extra={"ctx": {"user_id": uid}})

    # ------------------------------------------------------------------ legacy
    @socketio.on("join")
    @authed
    def on_join(user, data: dict):
        """
        Kept for the existing client.

        The payload's `user_id` is ignored on purpose — identity is the socket's.
        A client can no longer mark somebody else online.
        """
        uid = int(user["id"])
        join_room(user_room(uid))
        _join_groups_and_rooms(request.sid, uid)
        presence.touch(uid)
        return {"success": True, "user_id": uid}

    @socketio.on("subscribe")
    @authed
    def on_subscribe(user, data: dict):
        """Explicit room subscription for a group / room / server."""
        target = sanitize_text(data.get("room"), max_len=48)
        if not target:
            return {"success": False, "error": "room_required"}
        room = target if ":" in target else f"sub:{target}"
        if room.startswith("sub:"):
            return {"success": False, "error": "unknown_room"}
        if not _membership_ok(user, room):
            emit("denied", {"room": room, "reason": "not_a_member"})
            return {"success": False, "error": "forbidden"}
        join_room(room)
        return {"success": True, "room": room}

    @socketio.on("unsubscribe")
    @authed
    def on_unsubscribe(user, data: dict):
        target = sanitize_text(data.get("room"), max_len=48)
        if target:
            leave_room(target if ":" in target else f"sub:{target}")
        return {"success": True}

    @socketio.on("ping")
    @authed
    def on_ping(user, data: dict):
        presence.touch(int(user["id"]))
        return {"pong": time.time()}

    # ----------------------------------------------------------------- typing
    @socketio.on("typing")
    @authed
    def on_typing(user, data: dict):
        """
        Routed, throttled, and shaped for the existing client
        (`{from, to, scope, name}`).
        """
        uid = int(user["id"])
        throttle("socket:typing", capacity=30, window=10.0, extra=f"u{uid}")
        scope = (data.get("scope") or "user")
        target = _as_int(data.get("to"))
        if target is None:
            return None
        name = sanitize_text(data.get("name"), max_len=48) or str(user.get("full_name") or "")[:48]
        packet = {"from": uid, "to": target, "scope": "group" if scope == "group" else "user",
                  "name": name}
        if scope == "group":
            if not _membership_ok(user, f"group:{target}"):
                return None
            from .notify import emit_group
            emit_group(_socketio(), target, "typing", packet)
            return None
        # DM: deliver to the peer's room and to the sender's other tabs.
        emit_to_user(_socketio(), target, "typing", packet)
        emit_to_user(_socketio(), uid, "typing", packet)
        return None

    @socketio.on("stop_typing")
    @authed
    def on_stop_typing(user, data: dict):
        uid = int(user["id"])
        target = _as_int(data.get("to"))
        if target is None:
            return None
        scope = "group" if (data.get("scope") or "") == "group" else "user"
        packet = {"from": uid, "to": target, "scope": scope, "stop": True}
        if scope == "group":
            if _membership_ok(user, f"group:{target}"):
                from .notify import emit_group
                emit_group(_socketio(), target, "typing", packet)
        else:
            emit_to_user(_socketio(), target, "typing", packet)
            emit_to_user(_socketio(), uid, "typing", packet)
        return None

    # ------------------------------------------------------------ voice calls
    @socketio.on("join_voice")
    @authed
    def on_join_voice(user, data: dict):
        """
        Signalling only — no media ever passes through the server.

        Membership in the target room is verified before the join, and peers in
        the room are told who arrived so they start an offer.
        """
        uid = int(user["id"])
        room = _voice_room_key(data.get("room", DEFAULT_VOICE_ROOM), uid)
        if not _voice_allowed(user, room):
            emit("voice_error", {"code": "FORBIDDEN", "message": "عضو این اتاق نیستید"})
            return {"success": False, "error": "forbidden"}
        throttle("socket:voice_join", capacity=20, window=60.0, extra=f"u{uid}")
        previous = _VOICE_BY_SID.get(request.sid)
        if previous and previous != room:
            leave_room(previous)
            _ROOM_MEMBERS.get(previous, set()).discard(request.sid)
            _leave_notify(uid, previous)
        join_room(room)
        _VOICE_BY_SID[request.sid] = room
        _ROOM_MEMBERS[room].add(request.sid)
        peers = _voice_peers(room, exclude_uid=uid)
        emit("voice_joined", {"room": room, "peers": peers, "you": uid}, room=room)
        emit("voice_peers", {"room": room, "peers": peers + [{"id": uid, "name": user.get("full_name")}]},
             to=request.sid)
        log.info("voice_join", extra={"ctx": {"user_id": uid, "room": room}})
        return {"success": True, "room": room, "peers": peers}

    @socketio.on("leave_voice")
    @authed
    def on_leave_voice(user, data: dict):
        uid = int(user["id"])
        room = _VOICE_BY_SID.pop(request.sid, None) or _voice_room_key(data.get("room"), uid)
        leave_room(room)
        _ROOM_MEMBERS.get(room, set()).discard(request.sid)
        emit("voice_left", {"room": room, "user_id": uid}, room=room)
        log.info("voice_leave", extra={"ctx": {"user_id": uid, "room": room}})
        return {"success": True}

    @socketio.on("mute")
    @authed
    def on_mute(user, data: dict):
        """Local mute state broadcast so peers can show it (and the room persists it)."""
        uid = int(user["id"])
        room = _VOICE_BY_SID.get(request.sid)
        if not room:
            return {"success": False, "error": "not_in_voice"}
        mic = bool(data.get("mic", True))
        spk = bool(data.get("speaker", True))
        emit("voice_mute", {"user_id": uid, "mic": mic, "speaker": spk}, room=room)
        _persist_voice_state(uid, room, mic, spk)
        return {"success": True, "mic": mic, "speaker": spk}

    @socketio.on("voice_signal")
    @authed
    def on_voice_signal(user, data: dict):
        """
        WebRTC signalling relay.

        The relay only forwards an SDP/ICE blob to *other members of the sender's
        authenticated voice room*. `data['sender']` is ignored — the socket's own
        identity is stamped in — which is what stops signal spoofing and
        cross-room injection.
        """
        uid = int(user["id"])
        room = _VOICE_BY_SID.get(request.sid)
        if not room:
            room = _voice_room_key((data or {}).get("room"), uid)
            if not _voice_allowed(user, room):
                emit("voice_error", {"code": "FORBIDDEN", "message": "اتصال صوتی معتبر نیست"})
                return {"success": False, "error": "forbidden"}
            join_room(room)
            _VOICE_BY_SID[request.sid] = room
            _ROOM_MEMBERS[room].add(request.sid)
        signal = (data or {}).get("signal")
        if not _valid_signal(signal):
            emit("voice_error", {"code": "BAD_SIGNAL", "message": "ساختار سیگنال نامعتبر است"})
            return {"success": False, "error": "bad_signal"}
        throttle("socket:voice_signal", capacity=120, window=30.0, extra=f"u{uid}")
        emit("voice_signal", {"room": room, "sender": uid, "signal": signal},
             room=room, include_self=False)
        return {"success": True}

    def _valid_signal(signal: Any) -> bool:
        if not isinstance(signal, dict):
            return False
        if "sdp" in signal:
            sdp = signal.get("sdp") or {}
            if not isinstance(sdp, dict):
                return False
            if str(sdp.get("type", "")) not in {"offer", "answer", "pranswer", "rollback"}:
                return False
            return len(str(sdp.get("sdp") or "")) < 64_000
        cand = signal.get("candidate")
        if cand is None:
            return False
        return isinstance(cand, dict) and len(str(cand)) < 4000

    # ---------------------------------------------------------------- gaming
    @socketio.on("heartbeat")
    @authed
    def on_heartbeat(user, data: dict):
        """
        Host keeps Volexturn told it is alive (spec §16).

        Accepts the host's own report only for servers it owns; `server_id`
        from the payload is cross-checked against `lan_hosts.user_id`.
        """
        uid = int(user["id"])
        sid = _as_int(data.get("server_id"))
        if sid is None:
            return {"success": False, "error": "server_id_required"}
        throttle("socket:heartbeat", capacity=60, window=60.0, extra=f"u{uid}")
        db = _db()
        conn = db.connect()
        try:
            row = db.query_one(conn, "SELECT user_id, status FROM lan_hosts WHERE id = ?", (sid,))
            if row is None:
                return {"success": False, "error": "not_found"}
            if int(row["user_id"]) != uid:
                emit("denied", {"reason": "not_owner", "server_id": sid})
                return {"success": False, "error": "forbidden"}
            players = data.get("players_online")
            with db.tx(conn):
                db.execute(conn, f"""
                    UPDATE lan_hosts
                       SET last_heartbeat = {db.now_sql()}, status = 'online',
                           heartbeat_fails = 0,
                           player_count = COALESCE(?, player_count),
                           players_max  = COALESCE(?, players_max),
                           last_status_change = CASE WHEN status = 'online'
                                                     THEN last_status_change ELSE {db.now_sql()} END
                     WHERE id = ?""", (
                    _as_int(players) if players is not None else None,
                    _as_int(data.get("players_max")) if data.get("players_max") is not None else None,
                    sid)).close()
            prev = row.get("status")
            if prev != "online":
                from .notify import emit_public
                emit_public(_socketio(), "server_status", {"id": sid, "status": "online"})
            return {"success": True, "server_id": sid, "status": "online"}
        finally:
            db.close(conn)

    @socketio.on("room_join")
    @authed
    def on_room_join(user, data: dict):
        uid = int(user["id"])
        rid = _as_int(data.get("room_id"))
        if rid is None:
            return {"success": False, "error": "room_id_required"}
        join_room(f"game_room:{rid}")
        emit("room_presence", {"room_id": rid, "user_id": uid, "action": "watching"},
             room=f"game_room:{rid}", include_self=False)
        return {"success": True}

    @socketio.on("room_leave")
    @authed
    def on_room_leave(user, data: dict):
        rid = _as_int(data.get("room_id"))
        if rid is not None:
            leave_room(f"game_room:{rid}")
        return {"success": True}

    @socketio.on("room_message")
    @authed
    def on_room_message(user, data: dict):
        """Lobby chat. Persisted, membership-checked, rate-limited."""
        uid = int(user["id"])
        rid = _as_int(data.get("room_id"))
        body = sanitize_text(data.get("content"), max_len=1000)
        if rid is None or not body:
            return {"success": False, "error": "invalid"}
        throttle("socket:room_msg", capacity=25, window=30.0, extra=f"u{uid}")
        db = _db()
        conn = db.connect()
        try:
            member = db.query_one(conn, """
                SELECT 1 AS x FROM game_room_members
                 WHERE room_id = ? AND user_id = ? AND left_at IS NULL""", (rid, uid))
            if not member:
                emit("denied", {"reason": "not_in_room", "room_id": rid})
                return {"success": False, "error": "forbidden"}
            room_row = db.query_one(conn, "SELECT id, name, status FROM game_rooms WHERE id = ?", (rid,))
            if room_row is None or room_row["status"] == "closed":
                return {"success": False, "error": "room_closed"}
            mid = db.insert(conn, """
                INSERT INTO room_messages (room_id, user_id, content)
                VALUES (?, ?, ?)""", (rid, uid, body))
            msg = db.query_one(conn, """
                SELECT m.*, u.full_name AS sender_name, u.username, u.avatar
                FROM room_messages m JOIN users u ON u.id = m.user_id WHERE m.id = ?""", (mid,))
            conn.commit()
            payload = dict(msg or {})
            payload["room_name"] = (room_row or {}).get("name")
            emit("room_message", payload, room=f"game_room:{rid}")
            return {"success": True, "message": payload}
        finally:
            db.close(conn)

    # ------------------------------------------------------------ presence
    @socketio.on("set_presence")
    @authed
    def on_set_presence(user, data: dict):
        uid = int(user["id"])
        status = sanitize_text(data.get("status"), max_len=16, strip_newlines=True)
        presence.set_intent(uid, status)
        _persist_presence(uid, status or "online")
        _broadcast_presence()
        return {"success": True, "status": presence.status_for(uid)}

    @socketio.on_error_default
    def on_error(exc: Exception):
        # A raising handler must not kill the connection; log it and move on.
        log.warning("socket_handler_error", extra={"ctx": {"type": type(exc).__name__,
                                                            "err": str(exc)[:200]}})


# --------------------------------------------------------------------------
# shared internals
# --------------------------------------------------------------------------
def _voice_allowed(user: dict, room: str) -> bool:
    """`voice:*` rooms map to a group or a game room; policy lives there."""
    name = room.split(":", 1)[-1]
    if name in OPEN_VOICE_ROOMS or name.startswith("dm-"):
        return True
    if name.startswith("group-"):
        gid = _as_int(name.split("-", 1)[1])
        return gid is not None and _membership_ok(user, f"group:{gid}")
    if name.startswith("room-"):
        rid = _as_int(name.split("-", 1)[1])
        return rid is not None and _membership_ok(user, f"game_room:{rid}")
    return False


def _voice_peers(room: str, *, exclude_uid: int | None = None) -> list[dict]:
    """
    Who is in this voice room right now.

    Read from the server's own room registry (maintained on join/leave), never
    from a client claim, and never from python-socketio internals — so this
    keeps working across flask-socketio versions.
    """
    out, seen = [], set()
    for sid in _ROOM_MEMBERS.get(room, set()):
        u = _USER_BY_SID.get(sid)
        if not u or u["id"] in seen:
            continue
        if exclude_uid is not None and int(u["id"]) == int(exclude_uid):
            continue
        seen.add(u["id"])
        out.append({"id": u["id"], "name": u.get("full_name") or u.get("username"),
                    "username": u.get("username")})
    return out


def _leave_notify(uid: int, room: str) -> None:
    try:
        emit("voice_left", {"room": room, "user_id": uid}, room=room)
    except Exception:
        pass


def _persist_voice_state(uid: int, room: str, mic: bool, spk: bool) -> None:
    try:
        db = _db()
        conn = db.connect()
        try:
            db.execute(conn, """
                UPDATE game_room_members SET mic_muted = ?
                 WHERE user_id = ? AND left_at IS NULL
                   AND room_id IN (SELECT id FROM game_rooms WHERE voice_enabled = 1)""",
                   (0 if mic else 1, uid)).close()
            conn.commit()
        finally:
            db.close(conn)
    except Exception:                                   # non-critical UI hint
        pass


def _join_groups_and_rooms(sid: str, uid: int) -> None:
    """Subscribe the socket to every group/room it legitimately belongs to."""
    db = _db()
    conn = db.connect()
    try:
        for row in db.query(conn, "SELECT group_id FROM group_members WHERE user_id = ?", (uid,)):
            join_room(f"group:{int(row['group_id'])}")
        for row in db.query(conn, """
            SELECT room_id FROM game_room_members WHERE user_id = ? AND left_at IS NULL""", (uid,)):
            join_room(f"game_room:{int(row['room_id'])}")
        for row in db.query(conn, "SELECT id FROM game_rooms WHERE host_id = ? AND status != 'closed'", (uid,)):
            join_room(f"game_room:{int(row['id'])}")
    except Exception as exc:
        log.debug("room_subscribe_failed", extra={"ctx": {"err": str(exc)[:120]}})
    finally:
        db.close(conn)


def _persist_presence(uid: int, status: str) -> None:
    db = _db()
    conn = db.connect()
    try:
        if status == "offline":
            db.execute(conn, f"""UPDATE users SET presence = 'offline',
                                last_seen_at = {db.now_sql()} WHERE id = ?""", (uid,)).close()
        else:
            db.execute(conn, f"""UPDATE users SET presence = ?, last_seen_at = {db.now_sql()}
                                WHERE id = ?""", (status or "online", uid)).close()
        conn.commit()
    except Exception as exc:
        log.debug("presence_persist_failed", extra={"ctx": {"err": str(exc)[:120]}})
    finally:
        db.close(conn)


def _broadcast_presence() -> None:
    """
    Presence is genuinely global information in this app, so this one broadcast
    is intentional. Payload carries the online set so a client can reconcile
    without re-fetching `/users`.
    """
    try:
        emit_to_all = _socketio()
        emit_to_all.emit("user_status", {"online": sorted(presence.online_ids())})
    except Exception:
        pass


def user_for_sid(sid: str) -> dict | None:
    return _USER_BY_SID.get(sid)


def online_via_socket() -> set[int]:
    return presence.online_ids()
