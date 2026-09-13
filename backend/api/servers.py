"""
LAN Gaming Hub — server listings, heartbeat, discovery status.

Honesty rules (spec §36) that this module enforces in code, not prose:

  * `status` can only become `online` through (a) the owner's heartbeat or
    (b) a successful, policy-allowed probe. Nothing else sets it.
  * `player_count` is `None` unless a heartbeat or a protocol probe reported
    it. The API emits `players_known: false` so the UI prints "نامشخص" instead
    of a made-up 0/0.
  * `status: starting` is exclusively a human choice.
  * `password` protects *connection details*: an IP/port pair is only handed to
    someone who knows it. The password itself is stored hashed.
  * offline servers are archived, never deleted (spec §16).

`lan_hosts` is the pre-upgrade table, extended in place; `/lan_hosts`,
`/create_lan_host` and `/delete_lan_host/<id>` keep working verbatim.
"""

from __future__ import annotations

from flask import g, jsonify, request

from . import (Module, bool_arg, conn, current_db, int_arg, is_admin_user,
               my_id, payload, text_field)
from ..config import get_config
from ..discovery import (MANUAL_STATUSES, STATUS_OFFLINE, STATUS_ONLINE,
                         STATUS_UNKNOWN, health_check, probe_server_row)
from ..errors import BadRequest, Conflict, Forbidden, NotFound
from ..log import get_logger
from ..notify import notify
from ..security import (classify_target, clean_port_range, hash_password,
                        normalize_ip, pagination_args, paginated, sanitize_text,
                        verify_password)
from ..visibility import blocked_between

mod = Module("servers")
log = get_logger("api.servers")

ROOM_STATUSES = ("online", "offline", "full", "starting", "unknown")


def _cols() -> str:
    return """
        h.id, h.user_id, h.name, h.game_name, h.game_id, h.ip_address, h.port,
        h.description, h.region, h.max_players, h.player_count, h.players_max,
        h.is_password_protected, h.status, h.visibility, h.is_enabled, h.version,
        h.map_name, h.tags, h.motd, h.latency_ms, h.server_version, h.probe_detail,
        h.last_heartbeat, h.heartbeat_fails, h.last_status_change, h.timestamp,
        h.updated_at, h.archived_at, h.manual_status,
        u.username AS host_username, u.full_name AS host_name, u.avatar AS host_avatar,
        g.slug AS game_slug, g.name AS game_label, g.discover_provider, g.icon AS game_icon
    """


#: `_cols()` projects `g.slug`, `g.discover_provider`, … so every query that
#: selects it has to join the catalog. Four of the five did not, which made
#: creating or listing a server die with `no such column: g.slug` — one
#: constant, so a projection and its joins cannot drift apart again.
HOST_FROM = """FROM lan_hosts h
        JOIN users u ON u.id = h.user_id
        LEFT JOIN games g ON g.id = h.game_id"""


def _public(db, c, row: dict, viewer: int) -> dict:
    """
    Shape one row for the client.

    Connection details are withheld when the server is password-protected and
    the viewer is not the host/admin — see the honesty rules in the header.
    """
    out = dict(row)
    owner = int(out.get("user_id") or -1) == int(viewer)
    admin = is_admin_user()
    locked = bool(int(out.get("is_password_protected") or 0)) and not (owner or admin)
    hb = out.get("last_heartbeat")
    age = _age_seconds(hb)
    out.update({
        "game": out.get("game_label") or out.get("game_name"),
        "room_name": out.get("name") or out.get("game_name"),
        "players": out.get("player_count"),
        "players_known": out.get("player_count") is not None,
        "max_players": int(out.get("max_players") or 0) or None,
        "password_protected": bool(int(out.get("is_password_protected") or 0)),
        "locked": locked,
        "is_owner": owner,
        "can_edit": owner or admin,
        "created_at": out.get("timestamp"),
        "status": out.get("status") or STATUS_UNKNOWN,
        "heartbeat_age_seconds": age,
        "heartbeat_stale": bool(age is not None and age > get_config().heartbeat_timeout_seconds),
        "latency_ms": out.get("latency_ms"),
        "motd": out.get("motd") if not locked else None,
        "version": (out.get("server_version") or out.get("version")) if not locked else None,
        "provider": out.get("discover_provider") or "generic_tcp",
        "tags": [t for t in str(out.get("tags") or "").split(",") if t],
        "discover_url": f"/files/games/{out['game_icon']}" if out.get("game_icon") else None,
    })
    if locked:
        out["ip_address"] = None
        out["port"] = None
    if owner or admin:
        out["probe_detail"] = out.get("probe_detail")
    else:
        out.pop("probe_detail", None)
        out.pop("manual_status", None)
    for key in ("ip_address", "port"):
        if out.get(key) in (None, "", 0) and not locked:
            out[key] = None
    out.pop("password_hash", None)
    return out


def _age_seconds(value) -> int | None:
    """Seconds since `value`; None when there is no timestamp at all."""
    if not value:
        return None
    from ..auth import _iso
    moment = _iso(value)
    if moment is None:
        return None
    import datetime as dt
    return max(0, int((dt.datetime.utcnow() - moment).total_seconds()))


def _visible_to(db, c, viewer: int, row: dict) -> bool:
    if int(row.get("user_id") or -1) == viewer or is_admin_user():
        return True
    vis = (row.get("visibility") or "public").strip().lower()
    if blocked_between(db, c, viewer, int(row.get("user_id") or 0)):
        return False
    if vis in {"", "public"}:
        return True
    if vis == "friends":
        from ..visibility import friends_of
        return viewer in friends_of(db, c, int(row["user_id"]))
    if vis == "private":
        return bool(db.query_one(c, "SELECT 1 AS x FROM server_follows WHERE server_id = ? AND user_id = ?",
                                 (row["id"], viewer)))
    return True


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------
@mod.route("", methods=["GET"], auth="user", rate=None, endpoint="list")
def list_servers():
    limit, offset, page = pagination_args(default_size=20)
    db, c = current_db(), conn()
    me = my_id()
    where = ["h.archived_at IS NULL", "COALESCE(h.is_enabled,1) = 1"]
    params: list = []
    status = sanitize_text(request.args.get("status"), max_len=16, strip_newlines=True)
    if status in ROOM_STATUSES:
        where.append("h.status = ?"); params.append(status)
    game_id = int_arg("game_id", src=request.args, lo=1)
    if game_id:
        where.append("h.game_id = ?"); params.append(game_id)
    q = sanitize_text(request.args.get("q"), max_len=48, strip_newlines=True)
    if q:
        where.append(f"({db.ilike('h.name')} OR "
                     f"{db.ilike('h.game_name')} OR "
                     f"{db.ilike('h.description')})")
        params.extend(db.ilike_params(q) * 3)
    region = sanitize_text(request.args.get("region"), max_len=32, strip_newlines=True)
    if region:
        where.append(f"{db.ilike('h.region')}")
        params.extend(db.ilike_params(region))
    host = int_arg("host_id", src=request.args, lo=1)
    if host:
        where.append("h.user_id = ?"); params.append(host)
    scope = sanitize_text(request.args.get("scope"), max_len=16, strip_newlines=True)
    if scope == "mine":
        where.append("h.user_id = ?"); params.append(me)
    elif scope == "following":
        ids = sorted({int(r["server_id"]) for r in db.query(
            c, "SELECT server_id FROM server_follows WHERE user_id = ?", (me,))})
        if not ids:
            return jsonify({"success": True, "servers": [],
                            "pagination": {"total": 0, "limit": limit, "offset": offset,
                                           "page": page, "pages": 1, "has_more": False}})
        marks = ", ".join("?" for _ in ids)
        where.append(f"h.id IN ({marks})"); params.extend(ids)
    online_only = str(request.args.get("online", "")).lower() in {"1", "true", "yes"}
    if online_only:
        where.append("h.status = 'online'")

    clause = " AND ".join(where)
    total = db.scalar(c, f"""SELECT COUNT(*) FROM lan_hosts h
                             JOIN users u ON u.id = h.user_id WHERE {clause}""", params)
    order = {"new": "h.id DESC", "active": "h.last_heartbeat IS NULL, h.last_heartbeat DESC",
             "players": "h.player_count IS NULL, h.player_count DESC"}.get(
        sanitize_text(request.args.get("sort"), max_len=12, strip_newlines=True) or "new", "h.id DESC")
    rows = db.query(c, f"""
        SELECT {_cols()},
               (SELECT COUNT(*) FROM server_follows sf WHERE sf.server_id = h.id) AS followers,
               (SELECT COUNT(*) FROM server_players sp WHERE sp.server_id = h.id
                  AND sp.left_at IS NULL) AS joined_players
        {HOST_FROM}
        WHERE {clause}
        ORDER BY CASE h.status WHEN 'online' THEN 0 WHEN 'starting' THEN 1
                               WHEN 'full' THEN 2 WHEN 'unknown' THEN 3 ELSE 4 END,
                 {order} {db.limit_offset(limit, offset)}""", params)
    out = []
    followed_ids = {int(r["server_id"]) for r in db.query(
        c, "SELECT server_id FROM server_follows WHERE user_id = ?", (me,))}
    for r in rows:
        d = dict(r)
        if not _visible_to(db, c, me, d):
            continue
        item = _public(db, c, d, me)
        item["followed_by_me"] = int(d["id"]) in followed_ids
        out.append(item)
    body = {"success": True, "servers": out, **paginated(total, limit, offset, page)}
    body["discovery"] = health_check()
    return jsonify(body)


@mod.legacy("/lan_hosts", methods=("GET",), rate=None)
def legacy_lan_hosts():
    """
    Pre-upgrade contract: bare array with exactly the old keys.

    Extra keys ride along harmlessly; the ones the old SPA reads
    (`id, user_id, game_name, ip_address, port, description, timestamp, full_name`)
    are all present and unchanged in meaning.
    """
    db, c = current_db(), conn()
    me = my_id()
    rows = db.query(c, f"""
        SELECT {_cols()} {HOST_FROM}
        WHERE h.archived_at IS NULL ORDER BY h.id DESC LIMIT 200""")
    out = []
    for r in rows:
        d = dict(r)
        if not _visible_to(db, c, me, d):
            continue
        item = _public(db, c, d, me)
        item["full_name"] = d.get("host_name")            # legacy key
        out.append(item)
    return jsonify(out)


@mod.route("/<int:sid>", methods=["GET"], auth="user", rate=None, endpoint="detail")
def server_detail(sid: int):
    db, c = current_db(), conn()
    row = db.query_one(c, f"SELECT {_cols()} {HOST_FROM} "
                          f"WHERE h.id = ?", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    me = my_id()
    archived = bool(row.get("archived_at"))
    owner_or_admin = int(row["user_id"]) == me or is_admin_user()
    if archived:
        # An archived server was deleted. Skipping the visibility check because
        # it is no longer published made every private address of a deleted
        # server readable by anyone holding its id; owners and admins still see
        # it, everyone else gets the same answer as if the row were gone.
        if not owner_or_admin:
            raise NotFound("سرور پیدا نشد")
    elif not _visible_to(db, c, me, dict(row)):
        raise Forbidden("دسترسی به این سرور مجاز نیست", code="NOT_VISIBLE")
    out = _public(db, c, dict(row), me)
    out["players_list"] = [dict(r) for r in db.query(c, """
        SELECT sp.player_name, sp.joined_at, sp.left_at, u.id AS user_id, u.username,
               u.full_name, u.avatar
        FROM server_players sp LEFT JOIN users u ON u.id = sp.user_id
        WHERE sp.server_id = ? ORDER BY sp.joined_at DESC LIMIT 64""", (sid,))]
    out["followers"] = db.scalar(c, "SELECT COUNT(*) FROM server_follows WHERE server_id = ?", (sid,))
    out["followed_by_me"] = bool(db.query_one(c, "SELECT 1 AS x FROM server_follows WHERE server_id = ? AND user_id = ?",
                                              (sid, me)))
    out["history"] = [dict(r) for r in db.query(c, """
        SELECT status, created_at FROM server_status_log WHERE server_id = ?
        ORDER BY id DESC LIMIT 20""", (sid,))] if db.has_table(c, "server_status_log") else []
    return jsonify({"success": True, "server": out})


# --------------------------------------------------------------------------
# create / update / delete
# --------------------------------------------------------------------------
def _validate_target(ip_raw, port_raw) -> tuple[str, int]:
    ip = normalize_ip(ip_raw)
    if ip is None:
        raise BadRequest("آدرس IP معتبر نیست (فقط IPv4/IPv6 مستقیم)", code="BAD_IP")
    port = clean_port_range(port_raw, 0)
    if not port:
        raise BadRequest("پورت باید بین ۱ تا ۶۵۵۳۵ باشد", code="BAD_PORT")
    return ip, port


@mod.route("", methods=["POST"], auth="user", rate=(10, 600),
           legacy="/create_lan_host", legacy_methods=("POST",), endpoint="create")
def create_server():
    """Publish a game room/server (spec §14)."""
    me = my_id()
    body = payload()
    game_name = text_field("game_name", body, max_len=64, required=False)
    game_id = int_arg("game_id", src=body, lo=1)
    room_name = text_field("name", body, max_len=80, required=False)
    description = text_field("description", body, max_len=280, required=False, newlines=True)
    region = text_field("region", body, max_len=40, required=False)
    ip, port = _validate_target(body.get("ip_address"), body.get("port"))
    max_players = int_arg("max_players", 16, src=body, lo=1, hi=4096)
    visibility = sanitize_text(body.get("visibility"), max_len=16, strip_newlines=True) or "public"
    if visibility not in {"public", "friends", "private"}:
        raise BadRequest("دید نامعتبر است", code="BAD_VISIBILITY")
    password = str(body.get("password") or "")[:64]

    db, c = current_db(), conn()
    game = None
    if game_id:
        game = db.query_one(c, "SELECT id, name, default_port FROM games WHERE id = ?", (game_id,))
        if game is None:
            raise NotFound("بازی انتخاب‌شده در کاتالوگ نیست", code="GAME_NOT_FOUND")
        game_name = game_name or game["name"]
    if not game_name:
        raise BadRequest("نام بازی لازم است", code="GAME_NAME_REQUIRED")
    if game and not port and game.get("default_port"):
        port = int(game["default_port"])

    # The IP is stored; probing is a separate, policy-gated step (never here).
    status = STATUS_UNKNOWN
    if not get_config().discovery_enabled:
        status = STATUS_UNKNOWN
    sid = db.insert(c, """
        INSERT INTO lan_hosts (user_id, game_id, game_name, name, ip_address, port, description,
                               region, max_players, player_count, status, visibility,
                               password_hash, is_password_protected, manual_status, tags,
                               last_status_change, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 0, ?, ?, CURRENT_TIMESTAMP)""",
        (me, game_id, game_name, room_name or game_name, ip, port, description, region,
         max_players, status, visibility,
         hash_password(password) if password else None, 1 if password else 0,
         ",".join([t for t in str(body.get("tags") or "").split(",") if t.strip()][:8]),
         db.now_sql()))
    db.execute(c, db.ignore_clause("INSERT INTO server_follows (server_id, user_id) VALUES (?, ?)",
                                   on=["server_id", "user_id"]), (sid, me)).close()
    db.execute(c, """UPDATE gaming_profiles SET servers_hosted = COALESCE(servers_hosted,0) + 1
                     WHERE user_id = ?""", (me,)).close()
    _log_status(db, c, sid, status, me)
    c.commit()
    from .gaming import _activity
    _activity(db, c, me, "server_create", game_id=game_id, server_id=sid)
    _sio().emit("servers_changed", {"id": sid, "action": "created"})
    log.info("server_created", extra={"ctx": {"server_id": sid, "user_id": me, "port": port}})
    row = db.query_one(c, f"SELECT {_cols()} {HOST_FROM} "
                          f"WHERE h.id = ?", (sid,))
    return jsonify({"success": True, "id": sid, "server": _public(db, c, dict(row or {}), me),
                    "message": "سرور ثبت شد",
                    "status_note": ("وضعیت فعلاً unknown است؛ نخستین heartbeat یا probe معتبر آن را online می‌کند"
                                     if status == STATUS_UNKNOWN else None)}), 201


@mod.route("/<int:sid>", methods=["PATCH", "POST"], auth="user", rate=(20, 600), endpoint="update")
def update_server(sid: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id, status FROM lan_hosts WHERE id = ?", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان یا مدیر می‌تواند سرور را ویرایش کند", code="NOT_OWNER")
    body = payload()
    sets, args = [], []
    for key, col, mx in (("name", "name", 80), ("game_name", "game_name", 64),
                         ("description", "description", 280), ("region", "region", 40),
                         ("version", "version", 40), ("map_name", "map_name", 60)):
        if key in body:
            sets.append(f"{col} = ?")
            args.append(text_field(key, body, max_len=mx, required=False, newlines=True))
    if "ip_address" in body:
        ip, _ = _validate_target(body.get("ip_address"), row.get("port") or body.get("port") or 0)
        sets.append("ip_address = ?"); args.append(ip)
    if "port" in body:
        port = clean_port_range(body.get("port"), 0)
        if not port:
            raise BadRequest("پورت نامعتبر است", code="BAD_PORT")
        sets.append("port = ?"); args.append(port)
    if "max_players" in body:
        sets.append("max_players = ?")
        args.append(int_arg("max_players", 16, src=body, lo=1, hi=4096))
    if "visibility" in body:
        vis = sanitize_text(body.get("visibility"), max_len=16, strip_newlines=True)
        if vis not in {"public", "friends", "private"}:
            raise BadRequest("دید نامعتبر است", code="BAD_VISIBILITY")
        sets.append("visibility = ?"); args.append(vis)
    if "password" in body:
        pw = str(body.get("password") or "")[:64]
        sets.extend(["password_hash = ?", "is_password_protected = ?"])
        args.extend([hash_password(pw) if pw else None, 1 if pw else 0])
    if "status" in body:
        new = sanitize_text(body.get("status"), max_len=16, strip_newlines=True)
        if new not in MANUAL_STATUSES:
            raise BadRequest("وضعیت دستی نامعتبر است", code="BAD_STATUS",
                             details={"allowed": list(MANUAL_STATUSES)})
        sets.extend(["status = ?", "manual_status = 1"])
        args.append(new)
    if not sets:
        raise BadRequest("چیزی برای تغییر نبود", code="NO_CHANGES")
    sets.append(f"updated_at = {db.now_sql()}")
    db.execute(c, f"UPDATE lan_hosts SET {', '.join(sets)} WHERE id = ?", (*args, sid)).close()
    if "status" in body:
        _log_status(db, c, sid, sanitize_text(body.get("status"), max_len=16), me)
        db.execute(c, f"UPDATE lan_hosts SET last_status_change = {db.now_sql()} WHERE id = ?", (sid,)).close()
    c.commit()
    _sio().emit("servers_changed", {"id": sid, "action": "updated"})
    return jsonify({"success": True, "message": "سرور به‌روزرسانی شد"})


@mod.route("/<int:sid>", methods=["DELETE"], auth="user", rate=(10, 600),
           legacy="/delete_lan_host/<int:host_id>", legacy_methods=("DELETE",), endpoint="delete")
def delete_server(sid: int):
    """
    Owner/admin removal.

    A server with participants or followers is archived rather than deleted so
    the history stays auditable; `?purge=1` (admin) removes it outright.
    """
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id FROM lan_hosts WHERE id = ?", (sid,))
    if row is None:
        raise NotFound("پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط سازنده یا ادمین", code="NOT_OWNER")
    purge = bool_arg("purge", False) and is_admin_user()
    if purge:
        db.execute(c, "DELETE FROM server_players WHERE server_id = ?", (sid,)).close()
        db.execute(c, "DELETE FROM server_follows WHERE server_id = ?", (sid,)).close()
        if db.has_table(c, "server_status_log"):
            db.execute(c, "DELETE FROM server_status_log WHERE server_id = ?", (sid,)).close()
        db.execute(c, "DELETE FROM lan_hosts WHERE id = ?", (sid,)).close()
    else:
        db.execute(c, f"""UPDATE lan_hosts SET archived_at = {db.now_sql()}, status = 'offline',
                          is_enabled = 0 WHERE id = ?""", (sid,)).close()
    c.commit()
    _sio().emit("servers_changed", {"id": sid, "action": "archived" if not purge else "deleted"})
    return jsonify({"success": True, "mode": "purged" if purge else "archived"})


# --------------------------------------------------------------------------
# heartbeat (spec §16)
# --------------------------------------------------------------------------
@mod.route("/<int:sid>/heartbeat", methods=["POST"], auth="user", rate=(120, 60))
def heartbeat(sid: int):
    """
    REST twin of the `heartbeat` socket event, for hosts that prefer HTTP.

    Only the owner may report. The heartbeat is what turns `unknown`/`offline`
    into `online`; nothing here invents a player count.
    """
    me = my_id()
    body = payload()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id, status FROM lan_hosts WHERE id = ?", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان می‌تواند heartbeat بفرستد", code="NOT_OWNER")
    players = int_arg("players_online", src=body, lo=0, hi=100000)
    players_max = int_arg("players_max", src=body, lo=0, hi=100000)
    note = text_field("note", body, max_len=120, required=False)

    maxp = db.scalar(c, "SELECT COALESCE(max_players,0) FROM lan_hosts WHERE id = ?", (sid,))
    new_status = STATUS_ONLINE
    if players is not None and maxp and int(players) >= int(maxp):
        new_status = "full"
    db.execute(c, f"""
        UPDATE lan_hosts
           SET last_heartbeat = {db.now_sql()}, status = ?, heartbeat_fails = 0,
               player_count = COALESCE(?, player_count),
               players_max = COALESCE(?, players_max),
               probe_detail = ?,
               last_status_change = CASE WHEN status = ? THEN last_status_change ELSE {db.now_sql()} END,
               manual_status = 0, archived_at = NULL, is_enabled = 1,
               updated_at = {db.now_sql()}
         WHERE id = ?""", (new_status, players, players_max, note or None, new_status, sid)).close()
    _log_status(db, c, sid, new_status, me)
    if row.get("status") != new_status:
        _notify_followers(db, c, sid, row.get("user_id"), new_status)
    c.commit()
    _sio().emit("server_status", {"id": sid, "status": new_status, "players": players,
                                  "max": players_max, "source": "heartbeat"})
    ttl = get_config().heartbeat_timeout_seconds
    return jsonify({"success": True, "status": new_status, "server_id": sid,
                    "next_by_seconds": ttl, "expires_in": ttl})


@mod.route("/<int:sid>/offline", methods=["POST"], auth="user", rate=(20, 300))
def mark_offline(sid: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id, status FROM lan_hosts WHERE id = ?", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان یا مدیر")
    db.execute(c, f"""UPDATE lan_hosts SET status = 'offline', last_status_change = {db.now_sql()},
                      manual_status = 1, updated_at = {db.now_sql()} WHERE id = ?""", (sid,)).close()
    _log_status(db, c, sid, STATUS_OFFLINE, me)
    _notify_followers(db, c, sid, row["user_id"], STATUS_OFFLINE)
    c.commit()
    _sio().emit("server_status", {"id": sid, "status": STATUS_OFFLINE, "source": "manual"})
    return jsonify({"success": True, "status": STATUS_OFFLINE})


@mod.route("/<int:sid>/probe", methods=["POST"], auth="user", rate=(6, 120))
def probe_now(sid: int):
    """
    On-demand probe of your own server.

    Runs the same policy-gated provider as the janitor, so pressing this cannot
    turn the app into a scanner; disabled discovery returns `unknown`, not a
    fake result.
    """
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, """SELECT h.id, h.user_id, h.ip_address, h.port, h.password_hash,
                                    h.status, COALESCE(g.discover_provider,'generic_tcp') AS discover_provider
                             FROM lan_hosts h LEFT JOIN games g ON g.id = h.game_id
                             WHERE h.id = ?""", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط میزبان می‌تواند probe کند", code="NOT_OWNER")
    ok, reason = classify_target(str(row.get("ip_address") or ""), int(row.get("port") or 0))
    if not ok:
        return jsonify({"success": True, "probed": False, "reason": reason,
                        "status": row.get("status") or STATUS_UNKNOWN,
                        "policy": health_check(),
                        "message": "اکتشاف غیرفعال است یا هدف در شبکه مجاز نیست — وضعیتی تغییر نمی‌کند"})
    res = probe_server_row(dict(row))
    db.execute(c, f"""UPDATE lan_hosts SET status = ?, last_heartbeat = {db.now_sql()},
                      latency_ms = ?, player_count = COALESCE(?, player_count),
                      players_max = COALESCE(?, players_max), motd = ?, server_version = ?,
                      probe_detail = ?, last_status_change = {db.now_sql()},
                      updated_at = {db.now_sql()} WHERE id = ?""",
               (res.status, res.latency_ms, res.players_online, res.players_max,
                (res.motd or "")[:200] or None, (res.version or "")[:64] or None,
                (res.detail or "")[:120], sid)).close()
    _log_status(db, c, sid, res.status, me)
    c.commit()
    _sio().emit("server_status", {"id": sid, "status": res.status, "source": "probe"})
    return jsonify({"success": True, "probed": True, "status": res.status,
                    "players_online": res.players_online, "players_max": res.players_max,
                    "latency_ms": res.latency_ms, "motd": res.motd,
                    "version": res.version, "detail": res.detail,
                    "provider": row.get("discover_provider")})


# --------------------------------------------------------------------------
# password unlock / follow / join
# --------------------------------------------------------------------------
@mod.route("/<int:sid>/unlock", methods=["POST"], auth="user", rate=(10, 300))
def unlock(sid: int):
    """
    Reveal connection details for a password-protected server.

    The supplied password is verified against the stored hash; on success the
    client gets ip/port/motd. A wrong password costs nothing but a rate-limit
    tick — no hint is leaked.
    """
    me = my_id()
    password = str(payload().get("password") or "")[:64]
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id, password_hash, ip_address, port, motd, version "
                          "FROM lan_hosts WHERE id = ?", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    if int(row["user_id"]) == me or is_admin_user():
        stored_hash = None
    else:
        stored_hash = row.get("password_hash")
        if not stored_hash:
            raise Conflict("این سرور رمز ندارد", code="NO_PASSWORD")
        if not verify_password(password, stored_hash):
            raise Forbidden("رمز سرور اشتباه است", code="BAD_SERVER_PASSWORD")
    return jsonify({"success": True, "server_id": sid, "ip_address": row["ip_address"],
                    "port": row["port"], "motd": row.get("motd"), "version": row.get("version"),
                    "connect_string": f"{row['ip_address']}:{row['port']}" if row.get("ip_address") else None})


@mod.route("/<int:sid>/follow", methods=["POST"], auth="user", rate=(30, 300))
def follow_server(sid: int):
    me = my_id()
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM lan_hosts WHERE id = ?", (sid,)):
        raise NotFound("سرور پیدا نشد")
    existing = db.query_one(c, "SELECT id FROM server_follows WHERE server_id = ? AND user_id = ?", (sid, me))
    if existing:
        db.execute(c, "DELETE FROM server_follows WHERE id = ?", (existing["id"],)).close()
        following = False
    else:
        db.insert(c, "INSERT INTO server_follows (server_id, user_id) VALUES (?, ?)", (sid, me))
        following = True
    c.commit()
    return jsonify({"success": True, "following": following,
                    "followers": db.scalar(c, "SELECT COUNT(*) FROM server_follows WHERE server_id = ?", (sid,))})


@mod.route("/<int:sid>/join", methods=["POST"], auth="user", rate=(20, 300))
def join_server(sid: int):
    """
    Record intent to play, i.e. a join request the host can see.

    This is *not* a claim that the game server accepted anyone — Volexturn has
    no authority over the game's own admission. The row is a signed-up player.
    """
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, """SELECT h.user_id, h.max_players, h.status, h.name,
                                    COALESCE(u.full_name, u.username) AS host_name
                             FROM lan_hosts h JOIN users u ON u.id = h.user_id WHERE h.id = ?""", (sid,))
    if row is None:
        raise NotFound("سرور پیدا نشد")
    if row.get("status") == "offline":
        raise Conflict("سرور آفلاین گزارش شده است", code="SERVER_OFFLINE")
    name = sanitize_text((g.user or {}).get("full_name") or (g.user or {}).get("username"),
                         max_len=48)
    taken = db.scalar(c, "SELECT COUNT(*) FROM server_players WHERE server_id = ? AND left_at IS NULL", (sid,))
    if int(row.get("max_players") or 0) and int(taken) >= int(row["max_players"]):
        raise Conflict("ظرفیت پر است", code="SERVER_FULL")
    existing = db.query_one(c, "SELECT id, left_at FROM server_players WHERE server_id = ? AND player_name = ?",
                            (sid, name))
    if existing and not existing.get("left_at"):
        raise Conflict("از قبل در لیست هستید", code="ALREADY_JOINED")
    if existing:
        db.execute(c, """UPDATE server_players SET left_at = NULL, joined_at = CURRENT_TIMESTAMP,
                          user_id = ? WHERE id = ?""", (me, existing["id"])).close()
    else:
        db.insert(c, """INSERT INTO server_players (server_id, user_id, player_name)
                        VALUES (?, ?, ?)""", (sid, me, name))
    db.execute(c, """UPDATE gaming_profiles SET servers_joined = COALESCE(servers_joined,0) + 1
                     WHERE user_id = ?""", (me,)).close()
    notify(db, c, user_id=int(row["user_id"]), actor_id=me, ntype="room_join", target_id=sid,
           server_id=sid, target_type="server", body=f"به سرور «{row['name'] or ''}» پیوست",
           socketio=_sio())
    from .gaming import _activity
    _activity(db, c, me, "server_join", server_id=sid)
    c.commit()
    _sio().emit("server_players_changed", {"server_id": sid})
    return jsonify({"success": True, "joined": True, "players": int(taken) + 1,
                    "message": "به لیست بازیکنان اضافه شدید",
                    "note": "این ثبت در Volexturn است؛ ورود به خود سرور را بازی انجام می‌دهد"})


@mod.route("/<int:sid>/leave", methods=["POST"], auth="user", rate=(20, 300))
def leave_server(sid: int):
    me = my_id()
    name = sanitize_text((g.user or {}).get("full_name") or (g.user or {}).get("username"), max_len=48)
    db, c = current_db(), conn()
    # Match on the account *or* the display name: a row can exist from a join
    # made before the user was known (guest join), and both belong to this caller.
    cur = db.execute(c, """UPDATE server_players SET left_at = CURRENT_TIMESTAMP
                           WHERE server_id = ? AND left_at IS NULL
                             AND (user_id = ? OR player_name = ?)""", (sid, me, name))
    n = cur.rowcount
    cur.close()
    c.commit()
    _sio().emit("server_players_changed", {"server_id": sid})
    return jsonify({"success": True, "removed": int(n)})


# --------------------------------------------------------------------------
# discovery reporting
# --------------------------------------------------------------------------
@mod.route("/discovery", auth="user", rate=None)
def discovery_status():
    return jsonify({"success": True, **health_check()})


@mod.route("/stats", auth="user", rate=None)
def server_stats():
    db, c = current_db(), conn()
    rows = db.query(c, """
        SELECT COALESCE(g.name, h.game_name) AS game, COUNT(*) AS total,
               SUM(CASE WHEN h.status = 'online' THEN 1 ELSE 0 END) AS online
        FROM lan_hosts h LEFT JOIN games g ON g.id = h.game_id
        WHERE h.archived_at IS NULL GROUP BY COALESCE(g.name, h.game_name)
        ORDER BY total DESC LIMIT 20""")
    counts = {r["status"]: int(r["n"]) for r in db.query(c, """
        SELECT status, COUNT(*) AS n FROM lan_hosts
        WHERE archived_at IS NULL GROUP BY status""")}
    return jsonify({"success": True, "by_game": [
        {"game": r["game"], "total": int(r["total"]), "online": int(r["online"] or 0)} for r in rows],
        "totals": {"online": counts.get("online", 0), "full": counts.get("full", 0),
                   "starting": counts.get("starting", 0), "unknown": counts.get("unknown", 0),
                   "offline": counts.get("offline", 0), "total": sum(counts.values())}})


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _log_status(db, c, sid: int, status: str, by: int) -> None:
    """Status history, so 'do not fake server status' is auditable after the fact."""
    if not db.has_table(c, "server_status_log"):
        return
    try:
        db.execute(c, "INSERT INTO server_status_log (server_id, status, changed_by) VALUES (?, ?, ?)",
                   (sid, (status or "unknown")[:16], by)).close()
        db.execute(c, """DELETE FROM server_status_log WHERE server_id = ? AND id NOT IN
                         (SELECT id FROM server_status_log WHERE server_id = ? ORDER BY id DESC LIMIT 50)""",
                  (sid, sid)).close()
    except Exception:
        pass


def _notify_followers(db, c, sid: int, owner_id: int, new_status: str) -> None:
    if new_status not in {STATUS_ONLINE, STATUS_OFFLINE, "full"}:
        return
    targets = {int(owner_id)} if owner_id else set()
    targets |= {int(r["user_id"]) for r in db.query(
        c, "SELECT user_id FROM server_follows WHERE server_id = ?", (sid,))}
    ntype = "server_online" if new_status == STATUS_ONLINE else "server_offline"
    for uid in list(targets)[:300]:
        if uid == owner_id and new_status != STATUS_ONLINE:
            continue
        notify(db, c, user_id=uid, actor_id=owner_id, ntype=ntype, target_id=sid, server_id=sid,
               target_type="server", body="آنلاین شد" if new_status == STATUS_ONLINE else "آفلاین شد",
               socketio=_sio())


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
