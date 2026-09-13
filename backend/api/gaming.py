"""
Game catalog + per-user gaming library.

Everything is local: `games` is a table the admin (or a seed migration) owns.
No Steam/IGDB client, no network dependency — the spec is explicit that the
platform must work without third-party APIs.
"""

from __future__ import annotations

from flask import g, jsonify, request

from . import Module, conn, current_db, int_arg, my_id, payload, text_field
from ..errors import BadRequest, Conflict, NotFound
from ..log import get_logger
from ..security import (pagination_args, paginated, sanitize_text, slugify,
                        valid_slug)
from ..uploads import save_upload

mod = Module("gaming")
log = get_logger("api.games")

#: the four library states from the spec, with a canonical ordering
LIBRARY_STATUSES = ("owned", "playing", "favorite", "wishlist")


def _game_dict(row: dict, *, me: int | None = None, mine: dict | None = None) -> dict:
    out = dict(row)
    for key in ("icon", "cover"):
        val = out.get(key)
        if val and not str(val).startswith(("/", "http")):
            out[f"{key}_url"] = f"/files/games/{val}"
        elif val:
            out[f"{key}_url"] = val
        else:
            out[f"{key}_url"] = None
    out["platforms"] = [p for p in str(out.get("platforms") or "").split(",") if p]
    out["genres"] = [p for p in str(out.get("genre") or "").split(",") if p]
    out["my_status"] = (mine or {}).get("status")
    out["my_hours"] = int((mine or {}).get("hours_real") or 0)
    out["last_played_at"] = (mine or {}).get("last_played_at")
    return out


def _my_games(db, c, user_id: int, game_ids: list[int]) -> dict[int, dict]:
    if not game_ids:
        return {}
    marks = ", ".join("?" for _ in game_ids)
    rows = db.query(c, f"""SELECT game_id, status, hours_real, last_played_at FROM user_games
                           WHERE user_id = ? AND game_id IN ({marks})""", [user_id, *game_ids])
    return {int(r["game_id"]): dict(r) for r in rows}


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------
@mod.route("", methods=["GET"], auth="user", rate=None, endpoint="list_games")
def list_games():
    """Browse/search the catalog. `mine=1` limits it to the viewer's library."""
    limit, offset, page = pagination_args(default_size=24)
    q = sanitize_text(request.args.get("q"), max_len=64, strip_newlines=True)
    genre = sanitize_text(request.args.get("genre"), max_len=32, strip_newlines=True)
    platform = sanitize_text(request.args.get("platform"), max_len=24, strip_newlines=True)
    status = sanitize_text(request.args.get("status"), max_len=16, strip_newlines=True)
    featured = (request.args.get("featured") or "").lower() in {"1", "true", "yes"}
    me = my_id()
    db, c = current_db(), conn()

    where, params = ["1=1"], []
    if q:
        where.append(f"({db.ilike('g.name')} OR {db.ilike('g.slug')})")
        params.extend(db.ilike_params(q) * 2)
    if genre:
        where.append(f"{db.ilike('g.genre')}")
        params.extend(db.ilike_params(genre))
    if platform:
        where.append(f"{db.ilike('g.platforms')}")
        params.extend(db.ilike_params(platform))
    if featured:
        where.append("g.is_featured = 1")
    if status and status in LIBRARY_STATUSES:
        where.append("""g.id IN (SELECT game_id FROM user_games WHERE user_id = ? AND status = ?)""")
        params.extend([me, status])
    clause = " AND ".join(where)

    total = db.scalar(c, f"SELECT COUNT(*) FROM games g WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT g.*,
               (SELECT COUNT(*) FROM user_games ug WHERE ug.game_id = g.id) AS owners,
               (SELECT COUNT(*) FROM lan_hosts h WHERE h.game_id = g.id AND h.archived_at IS NULL
                  AND h.status = 'online') AS online_servers,
               (SELECT COUNT(*) FROM game_rooms r WHERE r.game_id = g.id
                  AND r.status IN ('open','starting','ingame')) AS open_rooms
        FROM games g WHERE {clause}
        ORDER BY g.is_featured DESC, g.name ASC {db.limit_offset(limit, offset)}""", params)
    games = [dict(r) for r in rows]
    mine = _my_games(db, c, me, [int(gg["id"]) for gg in games])
    return jsonify({"success": True, "games": [_game_dict(gg, me=me, mine=mine.get(int(gg["id"])))
                                               for gg in games],
                    **paginated(total, limit, offset, page)})


@mod.route("/<int:game_id>", methods=["GET"], auth="user", rate=None, endpoint="get_game")
def get_game(game_id: int):
    db, c = current_db(), conn()
    row = db.query_one(c, """
        SELECT g.*,
               (SELECT COUNT(*) FROM user_games ug WHERE ug.game_id = g.id) AS owners,
               (SELECT COUNT(*) FROM lan_hosts h WHERE h.game_id = g.id AND h.archived_at IS NULL) AS servers,
               (SELECT COUNT(*) FROM game_rooms r WHERE r.game_id = g.id
                  AND r.status IN ('open','starting','ingame')) AS open_rooms
        FROM games g WHERE g.id = ?""", (game_id,))
    if row is None:
        # accept slug too — nicer for shareable links
        slug = sanitize_text(str(game_id), max_len=64)
        row = db.query_one(c, "SELECT * FROM games WHERE slug = ?", (slug,))
        if row is None:
            raise NotFound("بازی پیدا نشد")
    mine = _my_games(db, c, my_id(), [int(row["id"])])
    out = _game_dict(dict(row), me=my_id(), mine=mine.get(int(row["id"])))
    out["recent_players"] = [dict(r) for r in db.query(c, """
        SELECT u.id, u.username, u.full_name, u.avatar, ug.last_played_at
        FROM user_games ug JOIN users u ON u.id = ug.user_id
        WHERE ug.game_id = ? AND ug.status IN ('playing','favorite')
        ORDER BY ug.last_played_at DESC LIMIT 12""", (row["id"],))]
    return jsonify({"success": True, "game": out})


@mod.route("/slug/<slug>", methods=["GET"], auth="user", rate=None)
def get_by_slug(slug: str):
    db, c = current_db(), conn()
    row = db.query_one(c, """SELECT g.*,
        (SELECT COUNT(*) FROM user_games ug WHERE ug.game_id = g.id) AS owners
        FROM games g WHERE g.slug = ?""", (sanitize_text(slug, max_len=64, strip_newlines=True),))
    if row is None:
        raise NotFound("بازی پیدا نشد")
    return jsonify({"success": True, "game": _game_dict(dict(row), me=my_id())})


@mod.route("/meta", methods=["GET"], auth="user", rate=None)
def catalog_meta():
    """Everything the create-server form needs, in one call."""
    db, c = current_db(), conn()
    rows = db.query(c, "SELECT id, slug, name, icon, default_port, platforms, genre, "
                       "discover_provider FROM games ORDER BY name")
    from ..discovery import providers_info
    return jsonify({
        "success": True,
        "games": [dict(r) for r in rows],
        "genres": sorted({g for r in rows for g in str(r["genre"] or "").split(",") if g}),
        "platforms": sorted({p for r in rows for p in str(r["platforms"] or "").split(",") if p}),
        "providers": providers_info(),
        "library_statuses": list(LIBRARY_STATUSES),
    })


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------
@mod.route("/library", methods=["GET"], auth="user", rate=None)
def library():
    me = my_id()
    db, c = current_db(), conn()
    rows = db.query(c, """
        SELECT ug.status, ug.hours_real, ug.last_played_at, ug.added_at, ug.achievements,
               g.id, g.slug, g.name, g.icon, g.genre, g.platforms, g.default_port,
               (SELECT COUNT(*) FROM lan_hosts h WHERE h.game_id = g.id AND h.status = 'online') AS online_servers
        FROM user_games ug JOIN games g ON g.id = ug.game_id
        WHERE ug.user_id = ?
        ORDER BY CASE ug.status WHEN 'playing' THEN 0 WHEN 'favorite' THEN 1
                                WHEN 'owned' THEN 2 ELSE 3 END, ug.updated_at DESC""", (me,))
    items = [_game_dict(dict(r), me=me, mine={"status": r["status"], "hours_real": r["hours_real"],
                                              "last_played_at": r["last_played_at"]}) for r in rows]
    counts = {s: 0 for s in LIBRARY_STATUSES}
    for it in items:
        counts[it["my_status"] or "owned"] = counts.get(it["my_status"] or "owned", 0) + 1
    return jsonify({"success": True, "games": items, "counts": counts})


@mod.route("/library/<int:game_id>", methods=["POST"], auth="user", rate=(60, 300))
def set_library(game_id: int):
    """Mark a game owned / playing / favorite / wishlist, or remove it."""
    me = my_id()
    body = payload()
    status = sanitize_text(body.get("status"), max_len=16, strip_newlines=True) or "owned"
    remove = status in {"remove", "none", ""} or str(body.get("remove", "")).lower() in {"1", "true"}
    if not remove and status not in LIBRARY_STATUSES:
        raise BadRequest("وضعیت نامعتبر است", code="BAD_STATUS",
                         details={"allowed": list(LIBRARY_STATUSES)})
    db, c = current_db(), conn()
    game = db.query_one(c, "SELECT id, name FROM games WHERE id = ?", (game_id,))
    if game is None:
        raise NotFound("بازی پیدا نشد")
    existing = db.query_one(c, "SELECT id, status FROM user_games WHERE user_id = ? AND game_id = ?",
                            (me, game_id))
    if remove:
        if existing:
            db.execute(c, "DELETE FROM user_games WHERE id = ?", (existing["id"],)).close()
    elif existing:
        db.execute(c, f"""UPDATE user_games SET status = ?, updated_at = {db.now_sql()},
                          last_played_at = CASE WHEN ? = 'playing' THEN {db.now_sql()} ELSE last_played_at END
                          WHERE id = ?""", (status, status, existing["id"])).close()
    else:
        db.execute(c, f"""INSERT INTO user_games (user_id, game_id, status, hours_real, last_played_at)
                          VALUES (?, ?, ?, 0, CASE WHEN ? = 'playing' THEN {db.now_sql()} ELSE NULL END)""",
                   (me, game_id, status, status)).close()
    _bump_profile(db, c, me, game_id=game_id, status=None if remove else status)
    _activity(db, c, me, "library", game_id=game_id, meta={"status": "removed" if remove else status})
    c.commit()
    return jsonify({"success": True, "game_id": game_id, "game": game["name"],
                    "status": None if remove else status,
                    "message": "از کتابخانه حذف شد" if remove else "کتابخانه به‌روز شد"})


@mod.route("/library/<int:game_id>/hours", methods=["POST"], auth="user", rate=(20, 600))
def log_hours(game_id: int):
    """
    Manual play-time entry.

    There is no trusted source of play time for arbitrary LAN games, so this is
    an explicit user action rather than something we invent or infer.
    """
    me = my_id()
    minutes = int_arg("minutes", 0, src=payload(), lo=0, hi=24 * 60)
    if not minutes:
        raise BadRequest("مدت زمان لازم است", code="MINUTES_REQUIRED")
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM games WHERE id = ?", (game_id,)):
        raise NotFound("بازی پیدا نشد")
    if not db.query_one(c, "SELECT id FROM user_games WHERE user_id = ? AND game_id = ?", (me, game_id)):
        db.insert(c, "INSERT INTO user_games (user_id, game_id, status) VALUES (?, ?, 'playing')", (me, game_id))
    db.execute(c, f"""UPDATE user_games SET hours_real = COALESCE(hours_real,0) + ?,
                      last_played_at = {db.now_sql()}, updated_at = {db.now_sql()}
                      WHERE user_id = ? AND game_id = ?""", (minutes, me, game_id)).close()
    first_session = int(db.scalar(c, """SELECT COUNT(*) FROM user_games
                                         WHERE user_id = ? AND COALESCE(hours_real,0) <= ?""",
                                  (me, minutes)) or 0) > 0
    db.execute(c, """UPDATE gaming_profiles
                        SET gaming_hours = COALESCE(gaming_hours,0) + ?,
                            games_played = COALESCE(games_played,0) + ?,
                            updated_at = CURRENT_TIMESTAMP
                      WHERE user_id = ?""",
               (max(1, int(minutes / 60)), 1 if first_session else 0, me)).close()
    _activity(db, c, me, "hours", game_id=game_id, meta={"minutes": minutes})
    c.commit()
    return jsonify({"success": True, "added_minutes": minutes,
                    "total_hours": db.scalar(c, "SELECT COALESCE(SUM(hours_real),0) FROM user_games WHERE user_id = ?", (me,))})


@mod.route("/recent", auth="user", rate=None)
def recent():
    """Recently played, for the sidebar (spec §18)."""
    me = my_id()
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=10)
    rows = db.query(c, f"""
        SELECT g.id, g.slug, g.name, g.icon, ug.status, ug.hours_real, ug.last_played_at
        FROM user_games ug JOIN games g ON g.id = ug.game_id
        WHERE ug.user_id = ? AND ug.last_played_at IS NOT NULL
        ORDER BY ug.last_played_at DESC {db.limit_offset(limit, offset)}""", (me,))
    return jsonify({"success": True, "games": [dict(r) for r in rows],
                    **paginated(db.scalar(c, "SELECT COUNT(*) FROM user_games WHERE user_id = ? AND last_played_at IS NOT NULL", (me,)),
                                limit, offset, page)})


# --------------------------------------------------------------------------
# gaming profile
# --------------------------------------------------------------------------
@mod.route("/profile", methods=["GET", "POST"], auth="user", rate=(30, 600), endpoint="profile")
def gaming_profile():
    me = my_id()
    db, c = current_db(), conn()
    if request.method == "POST":
        body = payload(form=True)
        sets, args = [], []
        tag = text_field("gamertag", body, max_len=32, required=False)
        if tag:
            if not valid_slug(tag.replace(" ", "-").lower()) and len(tag) < 3:
                raise BadRequest("گیم‌تگ نامعتبر است", code="BAD_GAMERTAG")
            clash = db.query_one(c, "SELECT user_id FROM gaming_profiles WHERE gamertag = ? AND user_id != ?",
                                 (tag, me))
            if clash:
                raise Conflict("این گیم‌تگ گرفته شده است", code="GAMERTAG_TAKEN")
            sets.append("gamertag = ?"); args.append(tag)
        for key, col in (("tagline", "tagline"), ("achievements", "achievements")):
            if key in body:
                sets.append(f"{col} = ?")
                args.append(text_field(key, body, max_len=280 if col == "tagline" else 2000, required=False,
                                       newlines=True))
        fav = int_arg("favorite_game_id", src=body, lo=0)
        if fav is not None:
            if fav and not db.query_one(c, "SELECT id FROM games WHERE id = ?", (fav,)):
                raise NotFound("بازی پیدا نشد")
            sets.append("favorite_game_id = ?"); args.append(fav or None)
        banner = request.files.get("banner")
        if banner is not None and getattr(banner, "filename", ""):
            saved = save_upload(banner, user_id=me, category="games", kind="image")
            sets.append("banner = ?"); args.append(saved.name)
        if not sets:
            raise BadRequest("چیزی برای ذخیره نبود", code="NO_CHANGES")
        sets.append("updated_at = CURRENT_TIMESTAMP")
        if not db.query_one(c, "SELECT user_id FROM gaming_profiles WHERE user_id = ?", (me,)):
            db.insert(c, "INSERT INTO gaming_profiles (user_id, gamertag) VALUES (?, ?)",
                      (me, str((g.user or {}).get("username") or me)))
        db.execute(c, f"UPDATE gaming_profiles SET {', '.join(sets)} WHERE user_id = ?",
                   (*args, me)).close()
        c.commit()
    row = db.query_one(c, """
        SELECT gp.*, g.name AS favorite_game_name, g.slug AS favorite_game_slug, g.icon AS favorite_game_icon
        FROM gaming_profiles gp LEFT JOIN games g ON g.id = gp.favorite_game_id
        WHERE gp.user_id = ?""", (me,))
    prof = dict(row or {})
    if prof.get("banner"):
        prof["banner_url"] = f"/files/games/{prof['banner']}"
    stats = {
        "games_owned": db.scalar(c, "SELECT COUNT(*) FROM user_games WHERE user_id = ?", (me,)),
        "games_playing": db.scalar(c, "SELECT COUNT(*) FROM user_games WHERE user_id = ? AND status = 'playing'", (me,)),
        "servers_hosted": db.scalar(c, "SELECT COUNT(*) FROM lan_hosts WHERE user_id = ?", (me,)),
        "rooms_hosted": db.scalar(c, "SELECT COUNT(*) FROM game_rooms WHERE host_id = ?", (me,)),
    }
    prof["stats"] = {**{k: int(v or 0) for k, v in stats.items()},
                     "level": int(prof.get("level") or 1), "xp": int(prof.get("xp") or 0)}
    return jsonify({"success": True, "profile": prof})


# --------------------------------------------------------------------------
# admin catalog management
# --------------------------------------------------------------------------
@mod.route("", methods=["POST"], auth="admin", rate=(20, 600), endpoint="create_game")
def create_game():
    body = payload(form=True)
    name = text_field("name", body, max_len=80)
    slug = text_field("slug", body, max_len=64, required=False) or slugify(name)
    if not valid_slug(slug):
        raise BadRequest("اسلاگ نامعتبر است", code="BAD_SLUG")
    db, c = current_db(), conn()
    if db.query_one(c, "SELECT id FROM games WHERE slug = ?", (slug,)):
        raise Conflict("این اسلاگ موجود است", code="SLUG_TAKEN")
    port = int_arg("default_port", src=body, lo=1, hi=65535)
    icon = cover = None
    for field_name, col in (("icon", "icon"), ("cover", "cover")):
        up = request.files.get(field_name)
        if up is not None and getattr(up, "filename", ""):
            saved = save_upload(up, user_id=my_id(), category="games", kind="image")
            if col == "icon":
                icon = saved.name
            else:
                cover = saved.name
    from ..discovery import provider_keys
    provider = sanitize_text(body.get("discover_provider"), max_len=32, strip_newlines=True) or "generic_tcp"
    if provider not in provider_keys():
        raise BadRequest("ارائه‌دهنده اکتشاف نامعتبر است", code="BAD_PROVIDER",
                         details={"allowed": provider_keys()})
    gid = db.insert(c, """
        INSERT INTO games (slug, name, description, icon, cover, platforms, genre,
                           is_featured, discover_provider, default_port)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, name, text_field("description", body, max_len=1000, required=False, newlines=True),
         icon, cover,
         ",".join([sanitize_text(p, max_len=16) for p in str(body.get("platforms") or "").split(",") if p][:8]),
         sanitize_text(body.get("genre"), max_len=40),
         1 if str(body.get("is_featured", "")).lower() in {"1", "true", "yes"} else 0,
         provider, port))
    c.commit()
    log.info("game_created", extra={"ctx": {"game_id": gid, "slug": slug, "by": my_id()}})
    return jsonify({"success": True, "id": gid, "slug": slug, "message": "بازی افزوده شد"}), 201


@mod.route("/<int:game_id>", methods=["PATCH", "POST"], auth="admin", rate=(30, 600),
           endpoint="update_game")
def update_game(game_id: int):
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM games WHERE id = ?", (game_id,)):
        raise NotFound("بازی پیدا نشد")
    body = payload()
    sets, args = [], []
    for key, col, mx in (("name", "name", 80), ("description", "description", 1000),
                         ("genre", "genre", 40), ("discover_provider", "discover_provider", 32)):
        if key in body:
            sets.append(f"{col} = ?")
            args.append(text_field(key, body, max_len=mx, required=False, newlines=True))
    if "platforms" in body:
        plats = ",".join([sanitize_text(p, max_len=16) for p in str(body.get("platforms") or "").split(",") if p][:8])
        sets.append("platforms = ?"); args.append(plats)
    if "default_port" in body:
        sets.append("default_port = ?")
        args.append(int_arg("default_port", src=body, lo=1, hi=65535))
    if "is_featured" in body:
        sets.append("is_featured = ?")
        args.append(1 if str(body.get("is_featured")).lower() in {"1", "true", "yes"} else 0)
    if not sets:
        raise BadRequest("چیزی برای تغییر نبود", code="NO_CHANGES")
    sets.append("updated_at = CURRENT_TIMESTAMP")
    db.execute(c, f"UPDATE games SET {', '.join(sets)} WHERE id = ?", (*args, game_id)).close()
    c.commit()
    return jsonify({"success": True, "message": "بازی به‌روزرسانی شد"})


@mod.route("/<int:game_id>", methods=["DELETE"], auth="admin", rate=(10, 600))
def delete_game(game_id: int):
    """
    Games with any history are archived rather than deleted.

    A hard delete would cascade into `lan_hosts.game_id`, `user_games` and
    `game_rooms`; losing a catalog entry to keep referential sanity is the wrong
    trade, so we refuse and tell the admin what to do instead.
    """
    db, c = current_db(), conn()
    game = db.query_one(c, "SELECT id, name, slug FROM games WHERE id = ?", (game_id,))
    if game is None:
        raise NotFound("بازی پیدا نشد")
    used = {
        "servers": db.scalar(c, "SELECT COUNT(*) FROM lan_hosts WHERE game_id = ?", (game_id,)),
        "library": db.scalar(c, "SELECT COUNT(*) FROM user_games WHERE game_id = ?", (game_id,)),
        "rooms": db.scalar(c, "SELECT COUNT(*) FROM game_rooms WHERE game_id = ?", (game_id,)),
    }
    if any(int(v or 0) for v in used.values()):
        db.execute(c, "UPDATE games SET is_featured = 0 WHERE id = ?", (game_id,)).close()
        c.commit()
        raise Conflict(
            "این بازی در حال استفاده است؛ از لیست ویژه خارج شد اما حذف نشد",
            code="GAME_IN_USE", details=used)
    db.execute(c, "DELETE FROM games WHERE id = ?", (game_id,)).close()
    c.commit()
    return jsonify({"success": True, "message": "بازی حذف شد"})


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _bump_profile(db, c, user_id: int, *, game_id: int | None, status: str | None) -> None:
    if not db.query_one(c, "SELECT user_id FROM gaming_profiles WHERE user_id = ?", (user_id,)):
        db.insert(c, "INSERT INTO gaming_profiles (user_id, gamertag) VALUES (?, ?)", (user_id, user_id))
    played = db.scalar(c, "SELECT COUNT(DISTINCT game_id) FROM user_games WHERE user_id = ? "
                          "AND status IN ('owned','playing','favorite')", (user_id,))
    db.execute(c, """UPDATE gaming_profiles SET games_played = ?, updated_at = CURRENT_TIMESTAMP
                     WHERE user_id = ?""", (played, user_id)).close()


def _activity(db, c, user_id: int, event: str, *, game_id: int | None = None,
              server_id: int | None = None, room_id: int | None = None,
              meta: dict | None = None) -> None:
    import json
    db.execute(c, """INSERT INTO gaming_activity (user_id, event, game_id, server_id, room_id, meta)
                     VALUES (?, ?, ?, ?, ?, ?)""",
               (user_id, event[:24], game_id, server_id, room_id,
                json.dumps(meta or {}, ensure_ascii=False)[:1000] if meta else None)).close()
    # XP is a small, legible level curve — no hidden scoring model.
    gain = {"library": 5, "hours": 10, "server_create": 20, "room_create": 15,
            "room_join": 5, "invite": 3}.get(event, 2)
    db.execute(c, """UPDATE gaming_profiles SET xp = COALESCE(xp,0) + ?,
                     level = 1 + (COALESCE(xp,0) + ?) / 100 WHERE user_id = ?""",
               (gain, gain, user_id)).close()


__all__ = ["mod", "LIBRARY_STATUSES", "_activity", "_game_dict"]
