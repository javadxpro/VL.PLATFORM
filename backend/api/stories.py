"""
Stories: same feature, properly bounded.

Added on top of the existing behaviour: privacy tiers, explicit expiry state,
viewers list (was owner-only, still is), pagination, delete, and a sweep that
runs from the janitor so expired media stops accumulating on disk.
"""

from __future__ import annotations

from flask import jsonify, request

from . import Module, conn, current_db, is_admin_user, my_id, payload, text_field
from ..errors import BadRequest, Forbidden, NotFound
from ..log import get_logger
from ..security import pagination_args, paginated, sanitize_text
from ..uploads import delete_upload, save_upload
from ..visibility import STORY_PRIVACY, allowed_ids_from_csv, blocked_between

mod = Module("stories")
log = get_logger("api.stories")

TTL_HOURS = 24


def _story_cols() -> str:
    return """
        s.id, s.user_id, s.file_path, s.file_type, s.timestamp, s.expires_at,
        s.caption, s.privacy, s.view_count, s.bg,
        u.full_name, u.username, u.avatar, u.accent,
        (SELECT COUNT(*) FROM story_views sv WHERE sv.story_id = s.id) AS viewers_count
    """


def _audience_ok(db, c, story: dict, viewer: int, *, admin: bool) -> bool:
    if admin or int(story["user_id"]) == viewer:
        return True
    privacy = (story.get("privacy") or "everyone").strip().lower()
    if privacy in {"", "everyone", "public"}:
        return not blocked_between(db, c, viewer, int(story["user_id"]))
    if blocked_between(db, c, viewer, int(story["user_id"])):
        return False
    if privacy == "followers":
        from ..visibility import followed
        return followed(db, c, viewer, int(story["user_id"]))
    if privacy == "friends":
        from ..visibility import is_friend
        return is_friend(db, c, viewer, int(story["user_id"]))
    if privacy == "private":
        allowed = allowed_ids_from_csv(story.get("allowed_user_ids"))
        return viewer in allowed
    return True


def _decorate(db, c, rows: list[dict], viewer: int) -> list[dict]:
    if not rows:
        return rows
    ids = [int(r["id"]) for r in rows]
    marks = ", ".join("?" for _ in ids)
    viewed = {int(r["story_id"]) for r in db.query(
        c, f"SELECT story_id FROM story_views WHERE user_id = ? AND story_id IN ({marks})",
        [viewer, *ids])}
    out = []
    for r in rows:
        d = dict(r)
        rid = int(d["id"])
        d["viewed_by_me"] = rid in viewed
        d["media_url"] = f"/files/stories/{d['file_path']}" if d.get("file_path") else None
        d["seconds_left"] = _seconds_left(d.get("expires_at"))
        d["expired"] = int(d["seconds_left"] or 0) <= 0
        d["can_delete"] = int(d["user_id"]) == viewer or is_admin_user()
        out.append(d)
    return out


def _seconds_left(expires) -> int:
    """Countdown for the ring UI. Negative once expired."""
    if not expires:
        return 0
    import datetime as dt
    text = str(expires).strip().replace("T", " ").split(".")[0]
    try:
        moment = dt.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int((moment - dt.datetime.utcnow()).total_seconds())


def _fetch(*, include_expired: bool = False, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    db, c = current_db(), conn()
    viewer = my_id()
    where = ["s.deleted_at IS NULL"]
    params: list = []
    if not include_expired:
        where.append(f"(s.expires_at IS NULL OR s.expires_at > {db.now_sql()})")
    if not is_admin_user():
        # Same shape as posts: the audience rule belongs in SQL, because a
        # Python-side filter *after* LIMIT both leaks and silently shrinks
        # pages. The 'private' tier stores a csv allow-list, which is matched
        # with a padded LIKE so it stays parameterised on both engines.
        from ..visibility import friends_of, following_of
        parts = ["s.user_id = ?", "COALESCE(s.privacy, '') IN ('everyone', 'public', '')"]
        params.append(viewer)
        friends = sorted(friends_of(db, c, viewer))
        following = sorted(following_of(db, c, viewer))
        if following:
            marks = ", ".join("?" for _ in following)
            parts.append(f"(s.privacy = 'followers' AND s.user_id IN ({marks}))")
            params.extend(following)
        if friends:
            marks = ", ".join("?" for _ in friends)
            parts.append(f"(s.privacy = 'friends' AND s.user_id IN ({marks}))")
            params.extend(friends)
        # `||` and NULL behave identically on SQLite and PostgreSQL here: a NULL
        # allow-list makes the predicate NULL, i.e. not visible.
        parts.append("(s.privacy = 'private' AND (',' || COALESCE(s.allowed_user_ids, '') || ',') LIKE ?)")
        params.append(f"%,{int(viewer)},%")
        where.append("(" + " OR ".join(parts) + ")")
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM stories s WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT {_story_cols()} FROM stories s JOIN users u ON u.id = s.user_id
        WHERE {clause} ORDER BY s.id DESC {db.limit_offset(limit, offset)}""", params)
    out = [dict(r) for r in rows]
    # Second tier (followers/friends/private) is cheap to evaluate per row and
    # only applies to a bounded page, so no SQL gymnastics are needed.
    out = [r for r in out if _audience_ok(db, c, r, viewer, admin=is_admin_user())]
    return out, int(total)


@mod.route("", methods=["GET"], auth="user", rate=None)
def list_stories():
    limit, offset, page = pagination_args(default_size=30)
    include_expired = (request.args.get("include_expired") or "").lower() in {"1", "true", "archived"}
    archived_only = (request.args.get("archived") or "").lower() in {"1", "true"}
    db, c = current_db(), conn()
    if archived_only:
        rows = db.query(c, f"""
            SELECT {_story_cols()} FROM stories s JOIN users u ON u.id = s.user_id
            WHERE s.user_id = ? AND s.is_archived = 1 ORDER BY s.id DESC
            {db.limit_offset(limit, offset)}""", (my_id(),))
        items = _decorate(db, c, [dict(r) for r in rows], my_id())
        total = db.scalar(c, "SELECT COUNT(*) FROM stories WHERE user_id = ? AND is_archived = 1",
                          (my_id(),))
        return jsonify({"success": True, "stories": items, **paginated(total, limit, offset, page)})
    items, total = _fetch(include_expired=include_expired, limit=limit, offset=offset)
    return jsonify({"success": True, "stories": items, **paginated(total, limit, offset, page)})


@mod.legacy("/stories", methods=("GET",), rate=None)
def legacy_stories():
    """Legacy contract: bare array, newest first, viewed_by_me included."""
    items, _total = _fetch(limit=60, offset=0)
    return jsonify(items)


@mod.route("", methods=["POST"], auth="user", rate=(20, 300),
           legacy="/create_story", legacy_methods=("POST",), endpoint="create_story")
def create_story():
    me = my_id()
    db, c = current_db(), conn()
    body = payload(form=True)
    privacy = sanitize_text(body.get("privacy"), max_len=16, strip_newlines=True) or "everyone"
    if privacy not in STORY_PRIVACY:
        raise BadRequest("وضوح استوری نامعتبر است", code="BAD_PRIVACY")
    caption = text_field("caption", body, max_len=280, required=False, newlines=True)
    allowed = body.get("allowed_user_ids")
    csv = ""
    if privacy == "private":
        ids = []
        raw = allowed if isinstance(allowed, (list, tuple)) else str(allowed or "").replace(";", ",").split(",")
        for chunk in raw:
            try:
                ids.append(int(str(chunk).strip()))
            except (TypeError, ValueError):
                continue
        ids = sorted({i for i in ids if i > 0})[:100]
        if not ids:
            raise BadRequest("برای استوری خصوصی حداقل یک مخاطب لازم است", code="AUDIENCE_REQUIRED")
        csv = ",".join(str(i) for i in ids)

    field = request.files.get("file")
    if field is None or not getattr(field, "filename", ""):
        raise BadRequest("فایلی ارسال نشده است", code="FILE_MISSING")
    saved = save_upload(field, user_id=me, category="stories")

    expires_at = db.hours_ahead_sql(TTL_HOURS)
    sid = db.insert(c, f"""
        INSERT INTO stories (user_id, file_path, file_type, caption, privacy,
                             allowed_user_ids, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, {expires_at})""",
        (me, saved.name, saved.kind, caption, privacy, csv or None))
    row = db.query_one(c, f"SELECT {_story_cols()} FROM stories s JOIN users u ON u.id = s.user_id "
                          f"WHERE s.id = ?", (sid,))
    item = dict(row or {"id": sid})
    item["media_url"] = saved.url
    item["seconds_left"] = TTL_HOURS * 3600
    item["viewed_by_me"] = True
    c.commit()
    log.info("story_created", extra={"ctx": {"user_id": me, "story_id": sid, "privacy": privacy}})
    return jsonify({"success": True, "message": "استوری با موفقیت قرار گرفت",
                    "story": item, "id": sid}), 201


@mod.route("/<int:story_id>", methods=["DELETE"], auth="user", rate=(30, 60))
def delete_story(story_id: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, user_id, file_path FROM stories WHERE id = ?", (story_id,))
    if row is None:
        raise NotFound("استوری پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط صاحب استوری یا مدیر")
    db.execute(c, "DELETE FROM story_views WHERE story_id = ?", (story_id,)).close()
    db.execute(c, "DELETE FROM stories WHERE id = ?", (story_id,)).close()
    if row.get("file_path"):
        delete_upload("stories", row["file_path"])
    c.commit()
    _sio().emit("story_deleted", {"story_id": story_id})
    return jsonify({"success": True, "message": "استوری حذف شد"})


@mod.route("/<int:story_id>/archive", methods=["POST"], auth="user", rate=(20, 120))
def archive_story(story_id: int):
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id, is_archived FROM stories WHERE id = ?", (story_id,))
    if row is None:
        raise NotFound("استوری پیدا نشد")
    if int(row["user_id"]) != me:
        raise Forbidden("فقط صاحب استوری")
    val = 0 if int(row.get("is_archived") or 0) else 1
    db.execute(c, "UPDATE stories SET is_archived = ? WHERE id = ?", (val, story_id)).close()
    c.commit()
    return jsonify({"success": True, "archived": bool(val)})


@mod.route("/<int:story_id>/view", methods=["POST"], auth="user", rate=(600, 60),
           legacy="/view_story/<int:story_id>", legacy_methods=("POST",), endpoint="view_story")
def view_story(story_id: int):
    """
    Idempotent per viewer via UNIQUE(story_id,user_id); the count is derived,
    never incremented blind, so replays cannot inflate it.
    """
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT id, user_id, privacy, allowed_user_ids FROM stories WHERE id = ?",
                       (story_id,))
    if row is None:
        raise NotFound("استوری پیدا نشد")
    if not _audience_ok(db, c, dict(row), me, admin=is_admin_user()):
        raise Forbidden("دسترسی به این استوری مجاز نیست", code="NOT_IN_AUDIENCE")
    db.execute(c, db.ignore_clause(
        "INSERT INTO story_views (story_id, user_id) VALUES (?, ?)",
        on=["story_id", "user_id"]), (story_id, me)).close()
    n = db.scalar(c, "SELECT COUNT(*) FROM story_views WHERE story_id = ?", (story_id,))
    db.execute(c, "UPDATE stories SET view_count = ? WHERE id = ?", (n, story_id)).close()
    c.commit()
    return jsonify({"success": True, "viewers": int(n)})


@mod.legacy("/story_views/<int:story_id>", methods=("GET",), rate=None)
def legacy_story_views(story_id: int):
    """Legacy: same rows, `{success, viewers}` only (the old SPA ignores counts)."""
    body = story_viewers(story_id).get_json()
    return jsonify({"success": True, "viewers": body.get("viewers", [])})


@mod.route("/<int:story_id>/viewers", auth="user", rate=None, endpoint="story_viewers")
def story_viewers(story_id: int):
    """Owner-only list — the pre-upgrade rule, kept exactly."""
    me = my_id()
    db, c = current_db(), conn()
    row = db.query_one(c, "SELECT user_id FROM stories WHERE id = ?", (story_id,))
    if row is None:
        raise NotFound("استوری پیدا نشد")
    if int(row["user_id"]) != me and not is_admin_user():
        raise Forbidden("فقط صاحب استوری می‌تواند بیننده‌ها را ببیند")
    limit, offset, page = pagination_args(default_size=50)
    total = db.scalar(c, "SELECT COUNT(*) FROM story_views WHERE story_id = ?", (story_id,))
    rows = db.query(c, """
        SELECT u.id, u.full_name, u.username, u.avatar, sv.timestamp FROM story_views sv
        JOIN users u ON u.id = sv.user_id WHERE sv.story_id = ?
        ORDER BY sv.timestamp DESC, sv.id DESC LIMIT ? OFFSET ?""", (story_id, limit, offset))
    viewers = [dict(r) for r in rows]
    return jsonify({"success": True, "viewers": viewers,
                    **paginated(total, limit, offset, page)})


@mod.route("/me", auth="user", rate=None)
def my_stories():
    db, c = current_db(), conn()
    rows = db.query(c, f"""
        SELECT {_story_cols()},
               (SELECT COUNT(*) FROM story_views sv WHERE sv.story_id = s.id) AS views
        FROM stories s JOIN users u ON u.id = s.user_id
        WHERE s.user_id = ? ORDER BY s.id DESC LIMIT 100""", (my_id(),))
    return jsonify({"success": True, "stories": _decorate(db, c, [dict(r) for r in rows], my_id())})


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
