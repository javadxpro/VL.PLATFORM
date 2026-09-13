"""
Unified search.

One endpoint, categorised results, every category paginated independently and
capped — the spec's "do not load thousands of records at once" is a hard limit
here, not a guideline: `per_category` is clamped to `max_page_size` and each
category runs its own `LIMIT`.

LIKE wildcards in the query are escaped, so `%%%` cannot turn into a full-table
scan. Visibility is enforced in SQL via `visibility_sql_clause` / membership, so
private content never enters the result set at all.
"""

from __future__ import annotations

from typing import Any

from flask import jsonify, request

from . import Module, conn, current_db, int_arg, is_admin_user, my_id
from .. import presence
from ..errors import BadRequest
from ..security import sanitize_text
from ..visibility import visibility_sql_clause

mod = Module("search")

CATEGORIES = ("users", "posts", "games", "servers", "groups", "rooms", "comments", "messages")


def _params(db: Any, q: str, times: int = 1) -> list:
    """
    Bound parameters for `times` LIKE comparisons.

    `Database.ilike_params` already escapes `%`/`_`, wraps in wildcards and
    case-folds for SQLite, so search never hand-builds a needle.
    """
    return list(db.ilike_params(q)) * times


@mod.route("", methods=["GET", "POST"], auth="user", rate=(40, 60))
def search():
    body = request.args.to_dict(flat=True) if request.method == "GET" else (request.get_json(silent=True) or {})
    q = sanitize_text(body.get("q") or body.get("query"), max_len=64, strip_newlines=True)
    if len(q) < 2:
        raise BadRequest("برای جست‌وجو حداقل ۲ کاراکتر بنویسید", code="QUERY_TOO_SHORT")
    limit = int_arg("per_category", 8, src=body, lo=1, hi=30)
    requested = sanitize_text(body.get("in"), max_len=120, strip_newlines=True)
    wanted = [c.strip() for c in requested.split(",") if c.strip()] if requested else list(CATEGORIES)
    bad = [c for c in wanted if c not in CATEGORIES]
    if bad:
        raise BadRequest("دسته نامعتبر است", code="BAD_CATEGORY", details={"unknown": bad, "allowed": list(CATEGORIES)})

    db, c = current_db(), conn()
    me = my_id()
    results: dict[str, Any] = {}
    total = 0
    for cat in wanted:
        handler = _HANDLERS[cat]
        items, count = handler(db, c, q, limit, me)
        results[cat] = {"items": items, "total": int(count), "truncated": int(count) > limit}
        total += int(count)
    return jsonify({"success": True, "query": q, "results": results,
                    "counts": {k: v["total"] for k, v in results.items()},
                    "total": total})


# --------------------------------------------------------------------------
# per-category queries
# --------------------------------------------------------------------------
def _search_users(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    where = f"(users.id = {int(q) if q.isdigit() else -1} OR " \
            f"{db.ilike('users.username')} OR " \
            f"{db.ilike('users.full_name')} OR " \
            f"{db.ilike('users.bio')})"
    params = _params(db, q, 3) if not q.isdigit() else [int(q), *_params(db, q, 3)]
    total = db.scalar(c, f"SELECT COUNT(*) FROM users WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT id, username, full_name, bio, avatar, presence, last_seen_at,
               (SELECT COUNT(*) FROM follows f WHERE f.following_id = users.id) AS followers
        FROM users WHERE {where} ORDER BY followers DESC, id ASC LIMIT ?""", [*params, limit])
    items = [dict(r) for r in rows]
    for it in items:
        it["is_online"] = presence.is_online(int(it["id"]))
    return items, total


def _search_posts(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    vis, vis_params = visibility_sql_clause(db, c, me, alias="p")
    where = f"""p.deleted_at IS NULL AND {vis} AND (
        {db.ilike('p.content')} OR
        {db.ilike('u.full_name')} OR
        {db.ilike('u.username')})"""
    params = [*vis_params, *_params(db, q, 3)]
    total = db.scalar(c, f"""SELECT COUNT(*) FROM posts p JOIN users u ON u.id = p.user_id
                             WHERE {where}""", params)
    rows = db.query(c, f"""
        SELECT p.id, p.content, p.file_path, p.file_type, p.timestamp, p.user_id,
               p.like_count, p.comment_count, p.view_count,
               u.username, u.full_name, u.avatar
        FROM posts p JOIN users u ON u.id = p.user_id
        WHERE {where} ORDER BY (COALESCE(p.like_count,0)*3 + COALESCE(p.comment_count,0)*4) DESC,
                 p.id DESC LIMIT ?""", [*params, limit])
    out = []
    for r in rows:
        d = dict(r)
        d["excerpt"] = _excerpt(d.get("content") or "", q)
        d["likes_count"] = int(d.pop("like_count", 0) or 0)
        d["comments_count"] = int(d.pop("comment_count", 0) or 0)
        d["views"] = int(d.pop("view_count", 0) or 0)
        out.append(d)
    return out, total


def _search_games(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    where = f"""({db.ilike('g.name')} OR
                 {db.ilike('g.slug')} OR
                 {db.ilike('g.genre')})"""
    params = _params(db, q, 3)
    total = db.scalar(c, f"SELECT COUNT(*) FROM games g WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT g.id, g.slug, g.name, g.genre, g.platforms, g.icon, g.description,
               (SELECT COUNT(*) FROM user_games ug WHERE ug.game_id = g.id) AS owners,
               (SELECT COUNT(*) FROM lan_hosts h WHERE h.game_id = g.id
                  AND h.status = 'online' AND h.archived_at IS NULL) AS online_servers,
               (SELECT COUNT(*) FROM game_rooms r WHERE r.game_id = g.id
                  AND r.status IN ('open','starting','ingame')) AS open_rooms
        FROM games g WHERE {where} ORDER BY owners DESC, g.name ASC LIMIT ?""", [*params, limit])
    out = []
    for r in rows:
        d = dict(r)
        d["icon_url"] = f"/files/games/{d['icon']}" if d.get("icon") else None
        out.append(d)
    return out, total


def _search_servers(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    where = f"""h.archived_at IS NULL AND COALESCE(h.is_enabled,1)=1 AND (
        {db.ilike('h.name')} OR
        {db.ilike('h.game_name')} OR
        {db.ilike('h.description')} OR
        {db.ilike('h.region')})"""
    params = _params(db, q, 4)
    total = db.scalar(c, f"SELECT COUNT(*) FROM lan_hosts h WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT h.id, h.name, h.game_name, h.status, h.ip_address, h.port, h.region,
               h.player_count, h.players_max, h.max_players, h.is_password_protected,
               h.last_heartbeat, h.description, u.full_name AS host_name, u.id AS host_id,
               COALESCE(g.name, h.game_name) AS game
        FROM lan_hosts h JOIN users u ON u.id = h.user_id
        LEFT JOIN games g ON g.id = h.game_id
        WHERE {where}
        ORDER BY CASE h.status WHEN 'online' THEN 0 WHEN 'full' THEN 1 ELSE 2 END, h.id DESC
        LIMIT ?""", [*params, limit])
    out = []
    for r in rows:
        d = dict(r)
        owner = int(d.get("host_id") or -1) == me
        locked = bool(int(d.get("is_password_protected") or 0)) and not (owner or is_admin_user())
        d["locked"] = locked
        if locked:
            d["ip_address"] = None
            d["port"] = None
        d["players_known"] = d.get("player_count") is not None
        out.append(d)
    return out, total


def _search_groups(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    """
    Only groups you belong to, plus public ones you could join by invite code.

    Searching every group's name would disclose the existence of private
    communities, so membership (or an explicit invite code match) is required.
    """
    where = f"""g.deleted_at IS NULL AND (
        (EXISTS (SELECT 1 FROM group_members gm WHERE gm.group_id = g.id AND gm.user_id = ?))
        OR (COALESCE(g.is_private,0) = 0 AND g.invite_code = ?)) AND (
        {db.ilike('g.name')} OR {db.ilike('g.description')})"""
    params = [me, q, *_params(db, q, 2)]
    total = db.scalar(c, f"SELECT COUNT(*) FROM groups g WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT g.id, g.name, g.description, g.avatar, g.created_at, g.creator_id,
               (SELECT COUNT(*) FROM group_members x WHERE x.group_id = g.id) AS member_count,
               (SELECT role FROM group_members gm WHERE gm.group_id = g.id AND gm.user_id = ?) AS my_role
        FROM groups g WHERE {where} ORDER BY member_count DESC, g.id DESC LIMIT ?""",
        [me, *params, limit])
    return [dict(r) for r in rows], total


def _search_rooms(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    where = f"""r.closed_at IS NULL AND r.visibility IN ('public','friends') AND (
        {db.ilike('r.name')} OR {db.ilike('r.description')})"""
    params = _params(db, q, 2)
    total = db.scalar(c, f"SELECT COUNT(*) FROM game_rooms r WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT r.id, r.name, r.status, r.max_players, r.region, r.created_at, r.host_id,
               (r.password_hash IS NOT NULL) AS is_locked,
               u.full_name AS host_name, u.username AS host_username, u.avatar AS host_avatar,
               COALESCE(g.name, '—') AS game,
               (SELECT COUNT(*) FROM game_room_members gm WHERE gm.room_id = r.id
                  AND gm.left_at IS NULL) AS players
        FROM game_rooms r JOIN users u ON u.id = r.host_id
        LEFT JOIN games g ON g.id = r.game_id
        WHERE {where}
        ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'starting' THEN 1 ELSE 2 END, r.id DESC
        LIMIT ?""", [*params, limit])
    return [dict(r) for r in rows], total


def _search_comments(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    where = f"{db.ilike('pc.content')}"
    params = _params(db, q, 1)
    total = db.scalar(c, f"SELECT COUNT(*) FROM post_comments pc WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT pc.id, pc.post_id, pc.content, pc.timestamp, pc.user_id,
               u.username, u.full_name, u.avatar
        FROM post_comments pc JOIN users u ON u.id = pc.user_id
        JOIN posts p ON p.id = pc.post_id AND p.deleted_at IS NULL
        WHERE {where} ORDER BY pc.id DESC LIMIT ?""", [*params, limit])
    out = []
    for r in rows:
        d = dict(r)
        d["excerpt"] = _excerpt(d.get("content") or "", q)
        out.append(d)
    return out, total


def _search_messages(db, c, q: str, limit: int, me: int) -> tuple[list[dict], int]:
    where = f"""m.deleted_for_everyone = 0 AND
        (m.sender_id = ? OR m.receiver_id = ? OR m.group_id IN
            (SELECT group_id FROM group_members WHERE user_id = ?)) AND
        {db.ilike('COALESCE(m.search_text, m.content)')}"""
    params = [me, me, me, *_params(db, q, 1)]
    total = db.scalar(c, f"SELECT COUNT(*) FROM messages m WHERE {where}", params)
    rows = db.query(c, f"""
        SELECT m.id, m.content, m.timestamp, m.sender_id, m.group_id, m.file_type,
               u.full_name AS sender_name, u.username, u.avatar
        FROM messages m JOIN users u ON u.id = m.sender_id
        WHERE {where} ORDER BY m.id DESC LIMIT ?""", [*params, limit])
    out = []
    for r in rows:
        d = dict(r)
        d["excerpt"] = _excerpt(d.get("content") or "", q)
        out.append(d)
    return out, total


def _excerpt(text: str, q: str, radius: int = 70) -> str:
    low, ql = text.lower(), q.lower()
    idx = low.find(ql)
    if idx < 0:
        return text[:radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(q) + radius)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


_HANDLERS = {
    "users": _search_users, "posts": _search_posts, "games": _search_games,
    "servers": _search_servers, "groups": _search_groups, "rooms": _search_rooms,
    "comments": _search_comments, "messages": _search_messages,
}


@mod.route("/suggestions", auth="user", rate=(60, 60))
def suggestions():
    """
    Typeahead: names + games only, 8 items, no bodies.

    Separate from /search so a keystroke-per-request pattern does not run eight
    COUNT(*) queries per character.
    """
    q = sanitize_text(request.args.get("q"), max_len=40, strip_newlines=True)
    if len(q) < 1:
        return jsonify({"success": True, "users": [], "games": []})
    db, c = current_db(), conn()
    users = db.query(c, f"""
        SELECT id, username, full_name, avatar FROM users
        WHERE {db.ilike('username')} OR {db.ilike('full_name')}
        ORDER BY id ASC LIMIT 8""", _params(db, q, 2))
    games = db.query(c, f"""
        SELECT id, slug, name, icon FROM games
        WHERE {db.ilike('name')} OR {db.ilike('slug')}
        ORDER BY name ASC LIMIT 8""", _params(db, q, 2))
    return jsonify({"success": True,
                    "users": [dict(r) for r in users],
                    "games": [dict(r) for r in games]})


@mod.route("/trending", auth="user", rate=None)
def trending():
    """
    Trending hashtags over the last 24h.

    Purely deterministic: frequency of a tag in recent posts, no engagement
    modelling, no external signal.
    """
    db, c = current_db(), conn()
    rows = db.query(c, f"""
        SELECT hashtags FROM posts
        WHERE hashtags IS NOT NULL AND hashtags != ''
          AND deleted_at IS NULL AND timestamp > {db.hours_ahead_sql(-24)}
        ORDER BY id DESC LIMIT 500""")
    counts: dict[str, int] = {}
    for r in rows:
        for tag in str(r["hashtags"]).split(","):
            tag = tag.strip()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:20]
    return jsonify({"success": True, "tags": [{"tag": t, "posts": n} for t, n in top],
                    "window_hours": 24})
