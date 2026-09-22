"""
Users, relationships, and gaming identity.

Two relationship axes coexist, deliberately:
  * `follows`   — the original one-directional graph, untouched
  * `friendships` — bidirectional, with a state machine (spec §5)

`blocked` is enforced everywhere content is read or written, via
`visibility.blocked_between()`, so a block actually silences rather than just
hiding.
"""

from __future__ import annotations

import re

from flask import g, jsonify, request

from . import (Module, bool_arg, conn, current_db, int_arg, is_admin_user,
               my_id, payload, text_field)
from .. import presence
from ..auth import as_user_dict
from ..errors import BadRequest, Conflict, Forbidden, NotFound, ValidationFailed
from ..log import get_logger
from ..notify import notify
from ..security import pagination_args, paginated, sanitize_text
from ..visibility import (FRIEND, NONE, RECEIVED, REQUESTED, blocked_between,
                          ensure_friendship_row, friends_of, friendship_state,
                          pair)

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


mod = Module("users")
log = get_logger("api.users")

USER_PUBLIC_COLS = ("id, username, full_name, bio, avatar, role, created_at, "
                    "last_seen_at, presence, accent, location, website, vl_id")


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------
def _list_users(q: str | None, limit: int, offset: int, viewer: int | None) -> tuple[list[dict], int]:
    db = current_db()
    c = conn()
    where, params = ["1=1"], []
    if q:
        where.append(f"({db.ilike('username')} "
                     f"OR {db.ilike('full_name')})")
        params.extend(db.ilike_params(q) * 2)
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM users WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT {USER_PUBLIC_COLS} FROM users
        WHERE {clause} ORDER BY id ASC {db.limit_offset(limit, offset)}""", params)
    out = [dict(r) for r in rows]
    ids = [int(r["id"]) for r in out]
    if ids and viewer:
        marks = ", ".join("?" for _ in ids)
        followed = {r["following_id"] for r in db.query(
            c, f"SELECT following_id FROM follows WHERE follower_id = ? AND following_id IN ({marks})",
            [viewer, *ids])}
        friends = friends_of(db, c, viewer)
        blocked = {r["blocked_user_id"] for r in db.query(
            c, f"SELECT blocked_user_id FROM user_blocks WHERE user_id = ? AND blocked_user_id IN ({marks})",
            [viewer, *ids])}
        blocking = {r["user_id"] for r in db.query(
            c, f"SELECT user_id FROM user_blocks WHERE blocked_user_id = ? AND user_id IN ({marks})",
            [viewer, *ids])}
        for row in out:
            rid = int(row["id"])
            row["is_following"] = rid in followed
            row["is_friend"] = rid in friends
            row["blocked_by_me"] = rid in blocked
            row["blocks_me"] = rid in blocking
    out = presence.stamp_many(out)
    return out, int(total)


@mod.route("", auth="user", rate=None, legacy=None)
def list_users():
    """Canonical listing: paginated, searchable. `/users` stays array-shaped."""
    limit, offset, page = pagination_args(default_size=30)
    q = sanitize_text(request.args.get("q"), max_len=64) or None
    rows, total = _list_users(q, limit, offset, my_id())
    return jsonify({"success": True, "users": rows, **paginated(total, limit, offset, page)})


@mod.legacy("/users", methods=("GET",), rate=None)
def legacy_users():
    """Pre-upgrade contract: a bare array, every user, `is_online` overlaid."""
    limit, offset, _ = pagination_args(default_size=500, max_size=1000)
    rows, _total = _list_users(None, limit, offset, my_id())
    return jsonify(rows)


@mod.route("/online", auth="user", rate=None)
def online():
    ids = sorted(presence.online_ids())
    return jsonify({"success": True, "online": ids, "count": len(ids)})


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------
def _profile_payload(target: int, viewer: int) -> dict:
    db = current_db()
    c = conn()
    row = db.query_one(c, f"SELECT {USER_PUBLIC_COLS} FROM users WHERE id = ?", (target,))
    if row is None or (row.get("status") or "active") == "deleted":
        raise NotFound("کاربر پیدا نشد")
    prof = dict(row)
    prof["is_online"] = presence.is_online(target)
    prof["presence"] = presence.status_for(target)
    prof["followers"] = db.scalar(c, "SELECT COUNT(*) FROM follows WHERE following_id = ?", (target,))
    prof["following_count"] = db.scalar(c, "SELECT COUNT(*) FROM follows WHERE follower_id = ?", (target,))
    prof["posts_count"] = db.scalar(c, "SELECT COUNT(*) FROM posts WHERE user_id = ? AND deleted_at IS NULL",
                                    (target,))
    prof["friends_count"] = db.scalar(c, """SELECT COUNT(*) FROM friendships
                                            WHERE state = 'friends' AND (user_a = ? OR user_b = ?)""",
                                      (target, target))
    prof["is_following"] = bool(db.query_one(
        c, "SELECT 1 AS x FROM follows WHERE follower_id = ? AND following_id = ?", (viewer, target)))
    prof["follows_me"] = bool(db.query_one(
        c, "SELECT 1 AS x FROM follows WHERE follower_id = ? AND following_id = ?", (target, viewer)))
    state = db.query_one(c, """SELECT state FROM friendships WHERE
                               (user_a = ? AND user_b = ?) OR (user_a = ? AND user_b = ?)""",
                         (min(viewer, target), max(viewer, target), max(viewer, target), min(viewer, target))) \
        if viewer != target else None
    prof["friend_state"] = (state or {}).get("state") or NONE
    prof["is_self"] = viewer == target
    prof["mutual_followers"] = db.scalar(c, """
        SELECT COUNT(*) FROM follows f1 JOIN follows f2 ON f1.follower_id = f2.following_id
        WHERE f1.following_id = ? AND f2.follower_id = ?""", (target, viewer))
    if viewer == target or is_admin_user():
        prof["email_hint"] = None
    return prof


@mod.route("/<int:target>", auth="user", rate=None)
def get_profile(target: int):
    return jsonify({"success": True, "user": _profile_payload(target, my_id())})


@mod.legacy("/user_profile/<int:me_id>/<int:target>", methods=("GET",), rate=None)
def legacy_profile(me_id: int, target: int):
    """Old shape: the bare profile dict, no envelope, `me` cross-checked."""
    if me_id != my_id() and not is_admin_user():
        raise Forbidden("دسترسی غیرمجاز")
    return jsonify(_profile_payload(target, my_id()))


@mod.route("/<int:target>/gaming", auth="user", rate=None)
def gaming_profile(target: int):
    db = current_db()
    c = conn()
    if not db.query_one(c, "SELECT id FROM users WHERE id = ?", (target,)):
        raise NotFound("کاربر پیدا نشد")
    prof = db.query_one(c, """
        SELECT gp.*, g.name AS favorite_game_name, g.slug AS favorite_game_slug
        FROM gaming_profiles gp
        LEFT JOIN games g ON g.id = gp.favorite_game_id
        WHERE gp.user_id = ?""", (target,)) or {}
    owned = db.query(c, """
        SELECT ug.status, ug.hours_real, ug.last_played_at, g.id, g.slug, g.name, g.icon, g.genre,
               g.platforms
        FROM user_games ug JOIN games g ON g.id = ug.game_id
        WHERE ug.user_id = ? ORDER BY ug.updated_at DESC, ug.id DESC""", (target,))
    activity = db.query(c, """
        SELECT a.*, g.name AS game_name, g.slug AS game_slug
        FROM gaming_activity a LEFT JOIN games g ON g.id = a.game_id
        WHERE a.user_id = ? ORDER BY a.id DESC LIMIT 20""", (target,))
    servers = db.query(c, """
        SELECT h.id, h.name, h.game_name, h.status, h.ip_address, h.port, h.region,
               COALESCE(g.name, h.game_name) AS game_label
        FROM lan_hosts h LEFT JOIN games g ON g.id = h.game_id
        WHERE h.user_id = ? AND h.archived_at IS NULL ORDER BY h.id DESC LIMIT 20""", (target,))
    body = {
        "success": True,
        "profile": {k: v for k, v in dict(prof).items() if k != "user_id"} | {"user_id": target},
        "games": [dict(r) for r in owned],
        "activity": [dict(r) for r in activity],
        "servers": [dict(r) for r in servers],
        "stats": {
            "games_played": int((prof or {}).get("games_played") or len(owned)),
            "servers_hosted": int((prof or {}).get("servers_hosted") or 0),
            "servers_joined": int((prof or {}).get("servers_joined") or 0),
            "gaming_hours": int((prof or {}).get("gaming_hours") or 0),
            "rooms_created": int((prof or {}).get("rooms_created") or 0),
            "level": int((prof or {}).get("level") or 1),
            "xp": int((prof or {}).get("xp") or 0),
        },
    }
    return jsonify(body)


# --------------------------------------------------------------------------
# follows
# --------------------------------------------------------------------------
@mod.route("/<int:target>/follow", methods=["POST"], auth="user", rate=(60, 60),
           endpoint="follow")
def follow_user(target: int):
    return jsonify(_toggle_follow(target))


@mod.legacy("/follow/<int:target_id>", methods=("POST",), rate=(60, 60))
def legacy_follow(target_id: int):
    """Exact legacy response: {success, following, followers}."""
    return jsonify(_toggle_follow(target_id))


def _toggle_follow(target: int) -> dict:
    me = my_id()
    if target == me:
        raise BadRequest("نمی‌توانی خودت را دنبال کنی", code="SELF_FOLLOW")
    db = current_db()
    c = conn()
    if not db.query_one(c, "SELECT id FROM users WHERE id = ?", (target,)):
        raise NotFound("کاربر پیدا نشد")
    existing = db.query_one(c, "SELECT id FROM follows WHERE follower_id = ? AND following_id = ?",
                            (me, target))
    if existing:
        db.execute(c, "DELETE FROM follows WHERE id = ?", (existing["id"],)).close()
        following = False
    else:
        db.insert(c, "INSERT INTO follows (follower_id, following_id) VALUES (?, ?)", (me, target))
        following = True
        notify(db, c, user_id=target, actor_id=me, ntype="follow", target_id=me,
               target_type="user", body=f"{g.user.get('full_name') or g.user.get('username')} شما را دنبال کرد",
               socketio=_sio())
    followers = db.scalar(c, "SELECT COUNT(*) FROM follows WHERE following_id = ?", (target,))
    c.commit()
    try:
        _sio().emit("follow_update", {"target_id": target, "followers": followers,
                                      "actor_id": me, "following": following})
    except Exception:
        pass
    return {"success": True, "following": following, "followers": int(followers)}


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]


# --------------------------------------------------------------------------
# friends
# --------------------------------------------------------------------------
@mod.route("/friends", auth="user", rate=None)
def friends_list():
    db = current_db()
    c = conn()
    me = my_id()
    rows = db.query(c, f"""
        SELECT u.id, u.username, u.full_name, u.avatar, u.presence, u.last_seen_at,
               f.state, f.updated_at AS since,
               CASE WHEN f.user_a = ? THEN f.user_b ELSE f.user_a END AS other_id
        FROM friendships f JOIN users u ON u.id =
             (CASE WHEN f.user_a = ? THEN f.user_b ELSE f.user_a END)
        WHERE f.state IN ('friends', 'requested', 'received', 'blocked')
          AND (f.user_a = ? OR f.user_b = ?)
        ORDER BY CASE f.state WHEN 'friends' THEN 0 ELSE 1 END, u.id
        {db.limit_offset(200, 0)}""", (me, me, me, me))
    buckets: dict[str, list[dict]] = {"friends": [], "incoming": [], "outgoing": [], "blocked": []}
    for r in rows:
        d = dict(r)
        other = int(d.pop("other_id"))
        d["is_online"] = presence.is_online(other)
        st = d.pop("state")
        if st == FRIEND:
            buckets["friends"].append(d)
        elif st == RECEIVED and int(d.get("requested_by") or 0) != me:
            buckets["incoming"].append(d)
        elif st == REQUESTED:
            buckets["outgoing"].append(d)
        elif st == "blocked":
            buckets["blocked"].append(d)
    return jsonify({"success": True, **buckets,
                    "counts": {k: len(v) for k, v in buckets.items()}})


@mod.route("/friends/request", methods=["POST"], auth="user", rate=(20, 60))
def friend_request():
    body = payload()
    me = my_id()
    target = int_arg("user_id", src=body)
    if target is None:
        name = text_field("username", body, max_len=32, required=False)
        row = current_db().query_one(conn(), "SELECT id FROM users WHERE username = ?", (name,)) if name else None
        if not row:
            raise NotFound("کاربر پیدا نشد")
        target = int(row["id"])
    if target == me:
        raise BadRequest("نمی‌توانی به خودت درخواست بدهی", code="SELF_REQUEST")
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM users WHERE id = ?", (target,)):
        raise NotFound("کاربر پیدا نشد")
    if blocked_between(db, c, me, target):
        raise Forbidden("امکان ارسال درخواست وجود ندارد", code="BLOCKED")
    ensure_friendship_row(db, c, me, target)
    lo, hi = pair(me, target)
    # `friendship_state` — never the raw column: the row is stored once per pair
    # with `requested_by` deciding the direction, so "requested" from A's seat is
    # "received" from B's seat. Reading the column directly makes an incoming
    # request invisible and let the receiver overwrite the pending row.
    state = friendship_state(db, c, me, target)
    if state == FRIEND:
        raise Conflict("شما از قبل دوست هستید", code="ALREADY_FRIENDS")
    if state == RECEIVED:
        db.execute(c, f"""UPDATE friendships SET state = 'friends', updated_at = {db.now_sql()}
                          WHERE user_a = ? AND user_b = ?""", (lo, hi)).close()
        for a_, b_ in ((me, target), (target, me)):
            db.execute(c, db.ignore_clause(
                "INSERT INTO follows (follower_id, following_id) VALUES (?, ?)",
                on=["follower_id", "following_id"]), (a_, b_)).close()
        notify(db, c, user_id=target, actor_id=me, ntype="friend_accept", target_id=me,
               target_type="user", socketio=_sio())
        c.commit()
        return jsonify({"success": True, "state": FRIEND, "accepted": True,
                        "message": "درخواست دوستی پذیرفته شد"})
    if state == REQUESTED:
        raise Conflict("درخواست شما هنوز در انتظار پاسخ است", code="ALREADY_PENDING")
    db.execute(c, f"""UPDATE friendships SET state = 'requested', requested_by = ?,
                      updated_at = {db.now_sql()} WHERE user_a = ? AND user_b = ?""",
               (me, min(me, target), max(me, target))).close()
    notify(db, c, user_id=target, actor_id=me, ntype="friend_request", target_id=me,
           target_type="user", body="برای شما درخواست دوستی فرستاد", socketio=_sio())
    c.commit()
    return jsonify({"success": True, "state": REQUESTED,
                    "message": "درخواست دوستی ارسال شد"})


@mod.route("/friends/respond", methods=["POST"], auth="user", rate=(30, 60))
def friend_respond():
    body = payload()
    me = my_id()
    target = int_arg("user_id", src=body) or 0
    accept = bool_arg("accept", True, src=body)
    db, c = current_db(), conn()
    # `friendship_state`, not the raw column: the row is stored once per pair, so
    # a pending request reads as 'requested' to its sender and 'received' to the
    # receiver. Reading the column here meant the receiver always got
    # NO_REQUEST and could never accept — while the 'I am the requester' case was
    # already excluded by the state itself, which is why the separate NOT_YOURS
    # guard is gone.
    if friendship_state(db, c, me, target) != RECEIVED:
        raise NotFound("درخواستی وجود ندارد", code="NO_REQUEST")
    new_state = FRIEND if accept else NONE
    db.execute(c, f"""UPDATE friendships SET state = ?, updated_at = {db.now_sql()}
                      WHERE user_a = ? AND user_b = ?""",
               (new_state, min(me, target), max(me, target))).close()
    if accept:
        # A friendship implies mutual follow, so the social graph stays coherent.
        db.execute(c, db.ignore_clause(
            "INSERT INTO follows (follower_id, following_id) VALUES (?, ?)",
            on=["follower_id", "following_id"]), (me, target)).close()
        db.execute(c, db.ignore_clause(
            "INSERT INTO follows (follower_id, following_id) VALUES (?, ?)",
            on=["follower_id", "following_id"]), (target, me)).close()
        notify(db, c, user_id=target, actor_id=me, ntype="friend_accept", target_id=me,
               target_type="user", socketio=_sio())
    c.commit()
    return jsonify({"success": True, "state": new_state,
                    "message": "دوست شدید 🎮" if accept else "درخواست رد شد"})


@mod.route("/friends/<int:target>/remove", methods=["POST"], auth="user", rate=(20, 60))
def friend_remove(target: int):
    me = my_id()
    db, c = current_db(), conn()
    cur = db.execute(c, f"""UPDATE friendships SET state = 'none', updated_at = {db.now_sql()}
                             WHERE ((user_a = ? AND user_b = ?) OR (user_a = ? AND user_b = ?))
                               AND state IN ('friends','requested','received')""",
                     (me, target, target, me))
    n = cur.rowcount
    cur.close()
    c.commit()
    return jsonify({"success": True, "changed": n > 0})


@mod.route("/block", methods=["POST"], auth="user", rate=(20, 60))
def block_user():
    body = payload()
    me = my_id()
    target = int_arg("user_id", src=body) or 0
    if target == me:
        raise BadRequest("خودت را نمی‌توانی بلاک کنی", code="SELF_BLOCK")
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM users WHERE id = ?", (target,)):
        raise NotFound("کاربر پیدا نشد")
    reason = text_field("reason", body, max_len=200, required=False, newlines=True)
    db.execute(c, db.ignore_clause(
        "INSERT INTO user_blocks (user_id, blocked_user_id, reason) VALUES (?, ?, ?)",
        on=["user_id", "blocked_user_id"]), (me, target, reason)).close()
    ensure_friendship_row(db, c, me, target)
    db.execute(c, f"""UPDATE friendships SET state = 'blocked', blocked_by = ?,
                      updated_at = {db.now_sql()} WHERE user_a = ? AND user_b = ?""",
               (me, min(me, target), max(me, target))).close()
    db.execute(c, "DELETE FROM follows WHERE (follower_id = ? AND following_id = ?) "
                  "OR (follower_id = ? AND following_id = ?)", (me, target, target, me)).close()
    c.commit()
    return jsonify({"success": True, "message": "کاربر مسدود شد"})


@mod.route("/unblock", methods=["POST"], auth="user", rate=(20, 60))
def unblock_user():
    body = payload()
    me = my_id()
    target = int_arg("user_id", src=body) or 0
    db, c = current_db(), conn()
    cur = db.execute(c, "DELETE FROM user_blocks WHERE user_id = ? AND blocked_user_id = ?", (me, target))
    n = cur.rowcount
    cur.close()
    db.execute(c, f"""UPDATE friendships SET state = 'none', blocked_by = NULL,
                      updated_at = {db.now_sql()}
                      WHERE user_a = ? AND user_b = ? AND state = 'blocked'""",
               (min(me, target), max(me, target))).close()
    c.commit()
    return jsonify({"success": True, "removed": n})


# --------------------------------------------------------------------------
# profile editing
# --------------------------------------------------------------------------
@mod.route("/me/profile", methods=["POST"], auth="user", rate=(20, 60),
           legacy="/update_profile", legacy_methods=("POST",), endpoint="update_profile")
def update_profile():
    """
    Accepts multipart (the legacy SPA) and JSON alike.

    Avatar upload goes through the storage provider with content sniffing, so a
    renamed `.exe` can never become a profile picture.
    """
    from ..uploads import save_upload
    me = my_id()
    db, c = current_db(), conn()
    body = payload(form=True)
    full_name = text_field("full_name", body, max_len=64, required=False) or None
    bio = text_field("bio", body, max_len=200, required=False, newlines=True) or None
    location = text_field("location", body, max_len=80, required=False)
    website = sanitize_text(body.get("website"), max_len=200)
    from ..security import safe_url
    website = safe_url(website) or None
    accent = sanitize_text(body.get("accent"), max_len=16)
    if accent and not _HEX_RE.match(accent):
        raise ValidationFailed("رنگ پروفایل باید به شکل #rrggbb باشد", code="BAD_ACCENT")
    visibility = sanitize_text(body.get("presence"), max_len=12)
    online_visible = body.get("online_visible")

    sets: list[str] = []
    args: list = []
    if full_name is not None:
        sets.append("full_name = ?"); args.append(full_name)
    if bio is not None:
        sets.append("bio = ?"); args.append(bio)
    if location is not None:
        sets.append("location = ?"); args.append(location)
    if website is not None:
        sets.append("website = ?"); args.append(website)
    if accent:
        sets.append("accent = ?"); args.append(accent)
    if visibility in {"online", "away", "dnd", "invisible", "offline"}:
        sets.append("presence = ?"); args.append(visibility)
    if online_visible is not None:
        sets.append("online_visible = ?"); args.append(1 if str(online_visible).lower() in {"1", "true", "yes"} else 0)

    avatar_field = request.files.get("avatar")
    if avatar_field is not None and getattr(avatar_field, "filename", ""):
        saved = save_upload(avatar_field, user_id=me, category="profiles", kind="avatar")
        sets.append("avatar = ?"); args.append(saved.name)
        try:                                  # best-effort cleanup of the old one
            old = db.query_one(c, "SELECT avatar FROM users WHERE id = ?", (me,))
            prev = (old or {}).get("avatar")
            if prev:
                from ..storage import get_storage
                get_storage().delete("profiles", str(prev))
        except Exception:
            pass
    if not sets:
        raise BadRequest("چیزی برای ذخیره نبود", code="NOTHING_TO_UPDATE")
    sets.append("updated_at = CURRENT_TIMESTAMP")
    db.execute(c, f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*args, me)).close()
    c.commit()
    row = db.query_one(c, "SELECT * FROM users WHERE id = ?", (me,))
    user = as_user_dict(row)
    user["presence"] = presence.status_for(me)
    return jsonify({"success": True, "user": user, "message": "پروفایل به‌روز شد"})
