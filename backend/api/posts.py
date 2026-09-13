"""
Posts, feed, likes, comments, reactions, saves, mentions.

Feed reading is one query + two set lookups instead of the old per-row loop
(defects P1/P2 in docs/AUDIT.md), and `/posts` keeps returning the exact bare
array the existing SPA expects while `/api/posts` adds pagination.
"""

from __future__ import annotations


from flask import g, jsonify, request

from . import (Module, conn, current_db, int_arg, is_admin_user, my_id, payload, text_field)
from ..config import get_config
from ..errors import BadRequest, Forbidden, NotFound
from ..feed import (annotate_viewer, get_scorer, rank, relationships)
from ..log import get_logger
from ..notify import notify
from ..security import (extract_hashtags, extract_mentions, pagination_args,
                        paginated, sanitize_text)
from ..uploads import delete_upload, save_upload
from ..visibility import (blocked_between, ensure_friendship_row, visibility_sql_clause)

mod = Module("posts")
log = get_logger("api.posts")

#: feed modes the client may ask for
FEED_MODES = ("recent", "recommended", "trending", "popular", "following", "friends",
              "media", "mine")

POST_COLS = """
    p.*, u.username, u.full_name, u.avatar, u.accent,
    COALESCE(p.like_count, 0)    AS likes_count,
    COALESCE(p.comment_count, 0) AS comments_count,
    COALESCE(p.view_count, 0)    AS views,
    (SELECT COUNT(*) FROM post_reactions pr WHERE pr.post_id = p.id) AS reactions_count
"""


def _row_public(db, c, row: dict, *, with_comments_preview: bool = False) -> dict:
    """Shape a DB row into the payload the frontend already consumes."""
    out = dict(row)
    out["tags"] = _tags_of(out)
    out["visibility"] = out.get("visibility") or "public"
    out["can_delete"] = bool(out.get("user_id") == my_id() or is_admin_user())
    if with_comments_preview:
        out["recent_comments"] = [dict(r) for r in db.query(c, """
            SELECT pc.id, pc.content, pc.user_id, pc.timestamp,
                   u.full_name, u.username, u.avatar
            FROM post_comments pc JOIN users u ON u.id = pc.user_id
            WHERE pc.post_id = ? ORDER BY pc.id DESC LIMIT 2""", (out["id"],))]
    for key in ("deleted_at", "hashtags"):
        out.pop(key, None)
    return out


def _tags_of(row: dict) -> list[str]:
    raw = row.get("hashtags")
    if raw:
        return [t for t in str(raw).split(",") if t][:12]
    return extract_hashtags(row.get("content") or "")


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------
def _fetch_feed(*, mode: str, limit: int, offset: int, tag: str | None,
                author: int | None, cursor: int | None) -> tuple[list[dict], int, bool]:
    db, c = current_db(), conn()
    viewer = my_id()
    where: list[str] = ["p.deleted_at IS NULL"]
    params: list = []

    vis_sql, vis_params = visibility_sql_clause(db, c, viewer, alias="p")
    where.append(vis_sql)
    params.extend(vis_params)

    if tag:
        # `hashtags` holds a normalised csv of bare names; '#' is a display
        # concern. The column is padded with commas so a tag only matches a
        # whole csv element (`art` cannot hit `artwork`), the '#'-prefixed form
        # is matched too for rows written by the old server, and the body text
        # is a last resort for rows that predate the column. Every pattern goes
        # through ilike_params(), which escapes `%`/`_` and case-folds both
        # sides — nothing is assembled by hand here.
        clean = str(tag).strip().lstrip("#")[:60]
        if clean:
            csv = "',' || COALESCE(p.hashtags, '') || ','"
            where.append(f"({db.ilike(csv)} OR {db.ilike(csv)} OR {db.ilike('p.content')})")
            params.extend(db.ilike_params(f",{clean},"))
            params.extend(db.ilike_params(f",#{clean},"))
            params.extend(db.ilike_params(f"#{clean}"))
    if author:
        where.append("p.user_id = ?")
        params.append(int(author))
    if cursor:
        where.append("p.id < ?")
        params.append(int(cursor))

    if mode == "following":
        followed = _followed_ids(db, c, viewer)
        if not followed:
            return [], 0, False
        marks = ", ".join("?" for _ in followed)
        where.append(f"p.user_id IN ({marks})")
        params.extend(sorted(followed))
    elif mode == "friends":
        from ..visibility import friends_of
        friends = friends_of(db, c, viewer)
        if not friends:
            return [], 0, False
        marks = ", ".join("?" for _ in friends)
        where.append(f"p.user_id IN ({marks})")
        params.extend(sorted(friends))
    elif mode == "mine":
        where.append("p.user_id = ?")
        params.append(viewer)
    elif mode == "media":
        where.append("p.file_path IS NOT NULL AND p.file_path != ''")

    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM posts p WHERE {clause}", params)

    # Ordering: pagination needs a stable SQL order; `rank()` only re-scores the
    # page we fetched, so a page never changes size because of scoring.
    order = "p.id DESC"
    fetch_limit = limit
    if mode in {"recommended", "trending", "popular"}:
        # Over-fetch within the page window so scoring has something to sort.
        fetch_limit = min(get_config().max_page_size * 3, max(limit * 4, 40))
        if mode in {"trending", "popular"}:
            order = "(COALESCE(p.like_count,0)*3 + COALESCE(p.comment_count,0)*4 " \
                    "+ COALESCE(p.view_count,0)) DESC, p.id DESC"
    rows = db.query(c, f"""
        SELECT {POST_COLS} FROM posts p JOIN users u ON u.id = p.user_id
        WHERE {clause}
        ORDER BY {order}
        {db.limit_offset(fetch_limit, offset)}""", params)
    posts = [dict(r) for r in rows]

    followed_set, friend_set = relationships(db, c, viewer)
    if mode in {"recommended", "trending", "popular"}:
        scorer = get_scorer(mode)
        posts = rank(posts, scorer, viewer_id=viewer, followed=followed_set,
                     friends=friend_set)
        posts = posts[:limit]
    posts = annotate_viewer(db, c, posts, viewer, followed_set, friend_set)
    has_more = offset + limit < total
    return posts, int(total), has_more


def _followed_ids(db, c, viewer: int) -> set[int]:
    return {int(r["following_id"]) for r in db.query(
        c, "SELECT following_id FROM follows WHERE follower_id = ?", (viewer,))}


@mod.route("", methods=["GET", "HEAD"], auth="user", rate=None)
def feed():
    limit, offset, page = pagination_args(default_size=12)
    mode = sanitize_text(request.args.get("mode"), max_len=24, strip_newlines=True) or "recent"
    if mode not in FEED_MODES:
        mode = "recent"
    tag = sanitize_text(request.args.get("tag"), max_len=40, strip_newlines=True) or None
    author = int_arg("author", src=request.args)
    cursor = int_arg("cursor", src=request.args, lo=0)
    posts, total, has_more = _fetch_feed(mode=mode, limit=limit, offset=offset,
                                        tag=tag, author=author, cursor=cursor)
    body = {"success": True, "posts": [
        {**p, "timestamp": p.get("timestamp"), "media_url": _media_url(p)} for p in posts],
        "mode": mode, **paginated(total, limit, offset, page)}
    body["pagination"]["scorer"] = get_scorer(mode).describe()
    return jsonify(body)


@mod.route("/feed", methods=["GET"], auth="user", rate=None)
def feed_alias():
    return feed()


@mod.legacy("/posts", methods=("GET",), rate=None)
def legacy_posts():
    """
    Pre-upgrade contract: a bare array of posts.

    Capped at one generous page instead of the old unbounded `SELECT *`; the
    SPA only ever renders the first screenful anyway (P1 in docs/AUDIT.md).
    """
    posts, _total, has_more = _fetch_feed(mode="recent", limit=60, offset=0,
                                          tag=None, author=None, cursor=None)
    return jsonify([{**p, "media_url": _media_url(p)} for p in posts])


def _media_url(row: dict) -> str | None:
    fp = row.get("file_path")
    if not fp:
        return None
    return f"/files/posts/{fp}"


# --------------------------------------------------------------------------
# create / delete
# --------------------------------------------------------------------------
@mod.route("", methods=["POST"], auth="user", rate=(20, 300),
           legacy="/create_post", legacy_methods=("POST",), endpoint="create_post")
def create_post():
    me = my_id()
    db, c = current_db(), conn()
    body = payload(form=True)
    content = text_field("content", body, max_len=4000, required=False, newlines=True)
    visibility = sanitize_text(body.get("visibility"), max_len=16, strip_newlines=True) or "public"
    if visibility not in {"public", "followers", "friends", "private", "self"}:
        raise BadRequest("وضوح پست نامعتبر است", code="BAD_VISIBILITY")

    saved = None
    file_field = request.files.get("file")
    if file_field is not None and getattr(file_field, "filename", ""):
        saved = save_upload(file_field, user_id=me, category="posts")
    if not content and saved is None:
        raise BadRequest("متن یا فایل لازم است", code="EMPTY_POST")

    tags = extract_hashtags(content)
    pid = db.insert(c, """
        INSERT INTO posts (user_id, content, file_path, file_type, visibility, hashtags, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (me, content, saved.name if saved else None, saved.kind if saved else None,
         visibility, ",".join(tags) if tags else None))

    _record_mentions(db, c, content, post_id=pid, comment_id=None, author=me)
    c.commit()
    row = db.query_one(c, f"SELECT {POST_COLS} FROM posts p JOIN users u ON u.id = p.user_id "
                          f"WHERE p.id = ?", (pid,))
    post = _row_public(db, c, dict(row or {"id": pid}))
    if saved:
        post["media_url"] = saved.url
    post["liked_by_me"] = False
    post["comments"] = 0
    return jsonify({"success": True, "message": "پست با موفقیت منتشر شد",
                    "post": post, "id": pid}), 201


@mod.route("/<int:post_id>", methods=["GET"], auth="optional", rate=None)
def post_detail(post_id: int):
    """
    One post by id — the permalink/refresh path the SPA has no endpoint for.

    Visibility is enforced here too: a private post answers 404 for everyone but
    its author, so the id itself never confirms that content exists.
    """
    me = getattr(g, "user_id", None)
    db, c = current_db(), conn()
    row = db.query_one(c, f"""SELECT {POST_COLS}, p.deleted_at FROM posts p
                              JOIN users u ON u.id = p.user_id WHERE p.id = ?""", (post_id,))
    if row is None or row.get("deleted_at"):
        raise NotFound("پست پیدا نشد")
    author = int(row["user_id"])
    admin = is_admin_user()
    if author != (me or 0) and not admin:
        vis = (row.get("visibility") or "public").lower()
        if vis not in {"public", "everyone", ""}:
            from ..visibility import may_see_author
            if not may_see_author(db, c, me or 0, author, vis, admin=admin):
                raise NotFound("پست پیدا نشد")
    out = _row_public(db, c, dict(row), with_comments_preview=True)
    if me:
        from ..feed import annotate_viewer, relationships
        followed_set, friend_set = relationships(db, c, me)
        annotate_viewer(db, c, [out], me, followed_set, friend_set)
    return jsonify({"success": True, "post": out})


@mod.route("/<int:post_id>", methods=["DELETE"], auth="user", rate=(30, 60))
def delete_post(post_id: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, user_id, file_path FROM posts WHERE id = ?", (post_id,))
    if row is None:
        raise NotFound("پست پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط نویسنده یا مدیر می‌تواند پست را حذف کند")
    _soft_delete_post(db, c, post_id)
    c.commit()
    _sio().emit("post_deleted", {"post_id": post_id})
    return jsonify({"success": True, "message": "پست حذف شد"})


def _soft_delete_post(db, c, post_id: int) -> None:
    """
    Soft delete, then the janitor-style cleanup drops the dependants.

    Soft keeps `/api/users/<id>/posts` consistent for anyone holding the id and
    leaves the row available to an admin audit; the media file is removed only
    once nothing references it.
    """
    db.execute(c, "UPDATE posts SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (post_id,)).close()
    for table in ("post_likes", "post_comments", "post_views", "post_reactions", "saved_posts"):
        if db.has_table(c, table):
            col = "post_id"
            try:
                db.execute(c, f"DELETE FROM {table} WHERE {col} = ?", (post_id,)).close()
            except Exception:
                pass
    row = db.query_one(c, "SELECT file_path FROM posts WHERE id = ?", (post_id,))
    if row and row.get("file_path"):
        still_used = db.scalar(c, "SELECT COUNT(*) FROM posts WHERE file_path = ? AND id != ? "
                                  "AND deleted_at IS NULL", (row["file_path"], post_id))
        if not still_used:
            delete_upload("posts", row["file_path"])


# --------------------------------------------------------------------------
# likes / reactions
# --------------------------------------------------------------------------
@mod.route("/<int:post_id>/like", methods=["POST"], auth="user", rate=(90, 60),
           endpoint="like")
def like(post_id: int):
    return jsonify(_toggle_like(post_id))


@mod.legacy("/like_post/<int:post_id>", methods=("POST",), rate=(90, 60))
def legacy_like(post_id: int):
    """Legacy shape: {success, liked, likes_count}."""
    return jsonify(_toggle_like(post_id))


def _toggle_like(post_id: int) -> dict:
    me = my_id()
    db, c = current_db(), conn()
    post = db.query_one(c, "SELECT id, user_id, deleted_at FROM posts WHERE id = ?", (post_id,))
    if post is None or post.get("deleted_at"):
        raise NotFound("پست پیدا نشد")
    if blocked_between(db, c, me, int(post["user_id"])) and me != int(post["user_id"]):
        raise Forbidden("امکان تعامل وجود ندارد", code="BLOCKED")
    existing = db.query_one(c, "SELECT id FROM post_likes WHERE post_id = ? AND user_id = ?",
                            (post_id, me))
    if existing:
        db.execute(c, "DELETE FROM post_likes WHERE id = ?", (existing["id"],)).close()
        liked = False
    else:
        # Two tabs can like the same post in the same instant; the unique index
        # plus INSERT-or-ignore makes that a no-op instead of a 500.
        db.execute(c, db.ignore_clause("INSERT INTO post_likes (post_id, user_id) VALUES (?, ?)",
                                     on=["post_id", "user_id"]), (post_id, me)).close()
        liked = True
    count = db.scalar(c, "SELECT COUNT(*) FROM post_likes WHERE post_id = ?", (post_id,))
    db.execute(c, "UPDATE posts SET like_count = ? WHERE id = ?", (count, post_id)).close()
    if liked:
        notify(db, c, user_id=int(post["user_id"]), actor_id=me, ntype="like",
               target_id=post_id, target_type="post", socketio=_sio())
    c.commit()
    _sio().emit("post_like", {"post_id": post_id, "likes_count": int(count), "actor_id": me})
    return {"success": True, "liked": liked, "likes_count": int(count)}


@mod.route("/<int:post_id>/react", methods=["POST"], auth="user", rate=(90, 60))
def react(post_id: int):
    body = payload()
    emoji = text_field("emoji", body, max_len=16)[:8]
    me = my_id()
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM posts WHERE id = ? AND deleted_at IS NULL", (post_id,)):
        raise NotFound("پست پیدا نشد")
    row = db.query_one(c, "SELECT id FROM post_reactions WHERE post_id = ? AND user_id = ? AND emoji = ?",
                       (post_id, me, emoji))
    if row:
        db.execute(c, "DELETE FROM post_reactions WHERE id = ?", (row["id"],)).close()
        active = False
    else:
        db.execute(c, db.ignore_clause(
            "INSERT INTO post_reactions (post_id, user_id, emoji) VALUES (?, ?, ?)",
            on=["post_id", "user_id", "emoji"]), (post_id, me, emoji)).close()
        active = True
    tally = db.query(c, "SELECT emoji, COUNT(*) AS n FROM post_reactions WHERE post_id = ? GROUP BY emoji",
                     (post_id,))
    c.commit()
    counts = {r["emoji"]: int(r["n"]) for r in tally if r.get("emoji")}
    _sio().emit("post_reactions", {"post_id": post_id, "counts": counts})
    return jsonify({"success": True, "active": active, "counts": counts})


# --------------------------------------------------------------------------
# comments
# --------------------------------------------------------------------------
@mod.route("/<int:post_id>/comments", methods=["GET"], auth="user", rate=None)
def comments(post_id: int):
    return jsonify({"success": True, **_comments_page(post_id)})


@mod.legacy("/post_comments/<int:post_id>", methods=("GET",), rate=None)
def legacy_comments(post_id: int):
    """Legacy shape: bare array of comment rows."""
    data = _comments_page(post_id)
    return jsonify(data["comments"])


def _comments_page(post_id: int) -> dict:
    db, c = current_db(), conn()
    post = db.query_one(c, "SELECT id, user_id, visibility FROM posts WHERE id = ?", (post_id,))
    if post is None or post.get("visibility") in {"private", "self"}:
        viewer = my_id()
        if post is None or (int(post["user_id"]) != viewer and not is_admin_user()):
            raise NotFound("پست پیدا نشد")
    limit, offset, page = pagination_args(default_size=25)
    total = db.scalar(c, "SELECT COUNT(*) FROM post_comments WHERE post_id = ?", (post_id,))
    rows = db.query(c, f"""
        SELECT pc.*, u.full_name, u.username, u.avatar,
               pr.content AS parent_content, pu.full_name AS parent_name
        FROM post_comments pc
        JOIN users u ON u.id = pc.user_id
        LEFT JOIN post_comments pr ON pr.id = pc.parent_id
        LEFT JOIN users pu ON pu.id = pr.user_id
        WHERE pc.post_id = ? ORDER BY pc.id ASC {db.limit_offset(limit, offset)}""",
        (post_id,))
    return {"comments": [dict(r) for r in rows], **paginated(total, limit, offset, page)}


@mod.route("/<int:post_id>/comments", methods=["POST"], auth="user", rate=(40, 60),
           endpoint="comment")
def comment(post_id: int):
    result = _add_comment(post_id)
    return jsonify(result)


@mod.legacy("/comment_post/<int:post_id>", methods=("POST",), rate=(40, 60))
def legacy_comment(post_id: int):
    """Legacy shape: {success, comment, comments_count}."""
    out = _add_comment(post_id)
    return jsonify({"success": True, "comment": out["comment"],
                    "comments_count": out["comments_count"]})


def _add_comment(post_id: int) -> dict:
    me = my_id()
    body = payload()
    content = text_field("content", body, max_len=1000, newlines=True)
    parent_id = int_arg("parent_id", src=body, lo=0)
    db, c = current_db(), conn()
    post = db.query_one(c, "SELECT id, user_id, deleted_at FROM posts WHERE id = ?", (post_id,))
    if post is None or post.get("deleted_at"):
        raise NotFound("پست پیدا نشد")
    if blocked_between(db, c, me, int(post["user_id"])) and me != int(post["user_id"]):
        raise Forbidden("امکان ثبت نظر وجود ندارد", code="BLOCKED")
    cid = db.insert(c, "INSERT INTO post_comments (post_id, user_id, content, parent_id) VALUES (?, ?, ?, ?)",
                    (post_id, me, content, parent_id))
    count = db.scalar(c, "SELECT COUNT(*) FROM post_comments WHERE post_id = ?", (post_id,))
    db.execute(c, "UPDATE posts SET comment_count = ? WHERE id = ?", (count, post_id)).close()
    row = db.query_one(c, """
        SELECT pc.*, u.full_name, u.username, u.avatar FROM post_comments pc
        JOIN users u ON u.id = pc.user_id WHERE pc.id = ?""", (cid,))
    comment_row = dict(row or {"id": cid, "content": content, "user_id": me})
    if parent_id:
        db.execute(c, "UPDATE post_comments SET reply_count = reply_count + 1 WHERE id = ?",
                   (parent_id,)).close()
    notify(db, c, user_id=int(post["user_id"]), actor_id=me, ntype="comment",
           target_id=post_id, target_type="post", body=content[:120], socketio=_sio())
    _record_mentions(db, c, content, post_id=post_id, comment_id=cid, author=me)
    c.commit()
    _sio().emit("post_comment", {"post_id": post_id, "comments_count": int(count),
                                 "comment": comment_row})
    return {"success": True, "comment": comment_row, "comments_count": int(count),
            "message": "نظر ثبت شد"}


@mod.route("/comments/<int:comment_id>", methods=["DELETE"], auth="user", rate=(40, 60))
def delete_comment(comment_id: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, """SELECT pc.id, pc.user_id, pc.post_id FROM post_comments pc
                             WHERE pc.id = ?""", (comment_id,))
    if row is None:
        raise NotFound("نظر پیدا نشد")
    post = db.query_one(c, "SELECT user_id FROM posts WHERE id = ?", (row["post_id"],))
    # a comment can go: its author, the post author (moderating their thread), admin
    if me not in (int(row["user_id"]), int((post or {}).get("user_id") or -1)) and not is_admin_user():
        raise Forbidden("مجاز به حذف این نظر نیستید")
    db.execute(c, "DELETE FROM post_comments WHERE id = ?", (comment_id,)).close()
    count = db.scalar(c, "SELECT COUNT(*) FROM post_comments WHERE post_id = ?", (row["post_id"],))
    db.execute(c, "UPDATE posts SET comment_count = ? WHERE id = ?", (count, row["post_id"])).close()
    c.commit()
    _sio().emit("post_comment_deleted", {"post_id": row["post_id"], "comment_id": comment_id,
                                         "comments_count": int(count)})
    return jsonify({"success": True})


# --------------------------------------------------------------------------
# views / saves / pin
# --------------------------------------------------------------------------
@mod.route("/<int:post_id>/view", methods=["POST"], auth="user", rate=(240, 60),
           legacy="/view_post/<int:post_id>", legacy_methods=("POST",), endpoint="view_post")
def view_post(post_id: int):
    """
    Idempotent per viewer (UNIQUE(post_id,user_id)), so refresh-spam cannot
    inflate a view count — which is what makes the trending score honest.
    """
    me = my_id()
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM posts WHERE id = ?", (post_id,)):
        raise NotFound("پست پیدا نشد")
    db.execute(c, db.ignore_clause("INSERT INTO post_views (post_id, user_id) VALUES (?, ?)",
                                   on=["post_id", "user_id"]), (post_id, me)).close()
    n = db.scalar(c, "SELECT COUNT(*) FROM post_views WHERE post_id = ?", (post_id,))
    db.execute(c, "UPDATE posts SET view_count = ? WHERE id = ?", (n, post_id)).close()
    c.commit()
    _sio().emit("post_view", {"post_id": post_id, "views": int(n)})
    return jsonify({"success": True, "views": int(n)})


@mod.route("/<int:post_id>/save", methods=["POST"], auth="user", rate=(60, 60))
def save_post(post_id: int):
    me = my_id()
    db, c = current_db(), conn()
    if not db.query_one(c, "SELECT id FROM posts WHERE id = ? AND deleted_at IS NULL", (post_id,)):
        raise NotFound("پست پیدا نشد")
    existing = db.query_one(c, "SELECT id FROM saved_posts WHERE user_id = ? AND post_id = ?", (me, post_id))
    if existing:
        db.execute(c, "DELETE FROM saved_posts WHERE id = ?", (existing["id"],)).close()
        saved = False
    else:
        db.insert(c, "INSERT INTO saved_posts (user_id, post_id) VALUES (?, ?)", (me, post_id))
        saved = True
    c.commit()
    return jsonify({"success": True, "saved": saved})


@mod.route("/saved", auth="user", rate=None)
def saved_list():
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=20)
    total = db.scalar(c, "SELECT COUNT(*) FROM saved_posts WHERE user_id = ?", (my_id(),))
    rows = db.query(c, f"""
        SELECT {POST_COLS}, sp.created_at AS saved_at FROM saved_posts sp
        JOIN posts p ON p.id = sp.post_id JOIN users u ON u.id = p.user_id
        WHERE sp.user_id = ? AND p.deleted_at IS NULL
        ORDER BY sp.id DESC {db.limit_offset(limit, offset)}""", (my_id(),))
    posts = [dict(r) for r in rows]
    followed, friends = relationships(db, c, my_id())
    posts = annotate_viewer(db, c, posts, my_id(), followed, friends)
    return jsonify({"success": True, "posts": posts, **paginated(total, limit, offset, page)})


@mod.route("/<int:post_id>/pin", methods=["POST"], auth="user", rate=(10, 300))
def pin_post(post_id: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id FROM posts WHERE id = ?", (post_id,))
    if row is None:
        raise NotFound("پست پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط نویسنده یا مدیر")
    val = 0 if db.scalar(c, "SELECT COALESCE(is_pinned,0) FROM posts WHERE id = ?", (post_id,)) else 1
    db.execute(c, "UPDATE posts SET is_pinned = ? WHERE id = ?", (val, post_id)).close()
    c.commit()
    return jsonify({"success": True, "pinned": bool(val)})


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------
@mod.legacy("/history/<int:me_id>", methods=("GET",), rate=None)
def legacy_history(me_id: int):
    if me_id != my_id() and not is_admin_user():
        raise Forbidden("دسترسی غیرمجاز")
    return jsonify(_history(me_id))


@mod.route("/history", auth="user", rate=None)
def history():
    limit, offset, page = pagination_args(default_size=30)
    rows = _history(my_id(), limit=limit, offset=offset)
    total = current_db().scalar(conn(), "SELECT COUNT(*) FROM post_views WHERE user_id = ?", (my_id(),))
    return jsonify({"success": True, "posts": rows, **paginated(total, limit, offset, page)})


def _history(user_id: int, *, limit: int = 100, offset: int = 0) -> list[dict]:
    db, c = current_db(), conn()
    rows = db.query(c, f"""
        SELECT {POST_COLS}, pv.timestamp AS viewed_at
        FROM post_views pv
        JOIN posts p ON p.id = pv.post_id
        JOIN users u ON u.id = p.user_id
        WHERE pv.user_id = ? AND p.deleted_at IS NULL
        ORDER BY pv.timestamp DESC, pv.id DESC {db.limit_offset(limit, offset)}""", (user_id,))
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# user's posts
# --------------------------------------------------------------------------
@mod.route("/by/<int:user_id>", auth="user", rate=None)
def user_posts(user_id: int):
    viewer = my_id()
    db, c = current_db(), conn()
    if blocked_between(db, c, viewer, user_id) and viewer != user_id:
        return jsonify({"success": True, "posts": [], "pagination": {"total": 0, "hidden": True}})
    limit, offset, page = pagination_args(default_size=12)
    vis_sql, vis_params = visibility_sql_clause(db, c, viewer, alias="p")
    total = db.scalar(c, f"""SELECT COUNT(*) FROM posts p WHERE p.user_id = ?
                             AND p.deleted_at IS NULL AND {vis_sql}""", [user_id, *vis_params])
    rows = db.query(c, f"""
        SELECT {POST_COLS} FROM posts p JOIN users u ON u.id = p.user_id
        WHERE p.user_id = ? AND p.deleted_at IS NULL AND {vis_sql}
        ORDER BY p.is_pinned DESC, p.id DESC {db.limit_offset(limit, offset)}""",
        [user_id, *vis_params])
    posts = [dict(r) for r in rows]
    followed, friends = relationships(db, c, viewer)
    posts = annotate_viewer(db, c, posts, viewer, followed, friends)
    return jsonify({"success": True, "posts": posts, **paginated(total, limit, offset, page)})


# --------------------------------------------------------------------------
# mentions
# --------------------------------------------------------------------------
def _record_mentions(db, c, text: str, *, post_id: int | None, comment_id: int | None,
                      author: int) -> int:
    names = extract_mentions(text or "")
    if not names:
        return 0
    made = 0
    for name in names[:10]:
        row = db.query_one(c, "SELECT id FROM users WHERE username = ? AND id != ?", (name, author))
        if not row:
            continue
        target = int(row["id"])
        if blocked_between(db, c, author, target):
            continue
        db.insert(c, """INSERT INTO mentions (post_id, comment_id, mentioned_user_id, mentioned_by)
                        VALUES (?, ?, ?, ?)""", (post_id, comment_id, target, author))
        notify(db, c, user_id=target, actor_id=author, ntype="mention",
               target_id=post_id or comment_id,
               target_type="post" if post_id else "comment", socketio=_sio())
        ensure_friendship_row(db, c, author, target)
        made += 1
    return made


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
