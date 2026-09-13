"""
Relationship states and content-visibility rules, in one place.

Centralised because the same question — "may A see/act on B's thing?" — comes up
in posts, stories, profiles, DMs, rooms and search. Spreading it across modules
is how the first two of those ended up inconsistent.

States (spec §5):

    none → requested / received → friends
    any  → blocked  (either direction)

`requested` always means *I* sent it; `received` means *they* sent it. Rows are
stored canonicalised as (user_a < user_b) so exactly one row per pair exists.
"""

from __future__ import annotations

from .db import Database

NONE = "none"
REQUESTED = "requested"
RECEIVED = "received"
FRIEND = "friends"
BLOCKED = "blocked"

STATES = (NONE, REQUESTED, RECEIVED, FRIEND, BLOCKED)

#: who may see content, in increasing order of intimacy
VISIBILITY_LEVELS = {"public": 0, "followers": 1, "friends": 2, "private": 3, "self": 4}
STORY_PRIVACY = ("everyone", "followers", "friends", "private")


def pair(a: int, b: int) -> tuple[int, int]:
    lo, hi = sorted((int(a), int(b)))
    return lo, hi


def ensure_friendship_row(db: Database, conn, a: int, b: int) -> None:
    """Idempotently create the canonical (lo, hi) row with state 'none'."""
    lo, hi = pair(a, b)
    if db.query_one(conn, "SELECT id FROM friendships WHERE user_a = ? AND user_b = ?", (lo, hi)):
        return
    if db.engine == "postgres":
        db.execute(conn, """INSERT INTO friendships (user_a, user_b, state)
                            VALUES (?, ?, 'none') ON CONFLICT (user_a, user_b) DO NOTHING""",
                   (lo, hi)).close()
    else:
        db.execute(conn, """INSERT OR IGNORE INTO friendships (user_a, user_b, state)
                            VALUES (?, ?, 'none')""", (lo, hi)).close()


def friendship_state(db: Database, conn, viewer: int, author: int) -> str:
    """'none' | 'requested' | 'received' | 'friends' | 'blocked', from `viewer`'s seat."""
    if not viewer or viewer == author:
        return NONE
    lo, hi = pair(viewer, author)
    row = db.query_one(conn, """SELECT state, requested_by, blocked_by FROM friendships
                                WHERE user_a = ? AND user_b = ?""", (lo, hi))
    if row is None:
        return NONE
    state = (row.get("state") or NONE)
    if state == REQUESTED:
        return REQUESTED if int(row.get("requested_by") or 0) == viewer else RECEIVED
    return state


def is_friend(db: Database, conn, a: int, b: int) -> bool:
    return friendship_state(db, conn, a, b) == FRIEND


def followed(db: Database, conn, follower: int, following: int) -> bool:
    return bool(db.query_one(conn, "SELECT 1 AS x FROM follows WHERE follower_id = ? AND following_id = ?",
                             (follower, following)))


def blocked_between(db: Database, conn, a: int, b: int) -> bool:
    """True if *either* direction blocks — a blocked pair never sees each other."""
    if not a or not b or a == b:
        return False
    row = db.query_one(conn, """
        SELECT 1 AS x FROM user_blocks
        WHERE (user_id = ? AND blocked_user_id = ?) OR (user_id = ? AND blocked_user_id = ?)""",
        (a, b, b, a))
    if row:
        return True
    lo, hi = pair(a, b)
    st = db.query_one(conn, "SELECT state FROM friendships WHERE user_a = ? AND user_b = ?", (lo, hi))
    return bool(st) and st["state"] == BLOCKED


def friends_of(db: Database, conn, user_id: int) -> set[int]:
    rows = db.query(conn, """SELECT user_a, user_b FROM friendships
                             WHERE state = 'friends' AND (user_a = ? OR user_b = ?)""",
                    (user_id, user_id))
    out = set()
    for r in rows:
        other = int(r["user_b"]) if int(r["user_a"]) == user_id else int(r["user_a"])
        out.add(other)
    return out


def following_of(db: Database, conn, user_id: int) -> set[int]:
    return {int(r["following_id"]) for r in db.query(
        conn, "SELECT following_id FROM follows WHERE follower_id = ?", (user_id,))}


def follower_ids(db: Database, conn, user_id: int) -> set[int]:
    return {int(r["follower_id"]) for r in db.query(
        conn, "SELECT follower_id FROM follows WHERE following_id = ?", (user_id,))}


def allowed_ids_from_csv(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out = set()
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


def may_see_author(db: Database, conn, viewer_id: int, author_id: int,
                   visibility: str | None, *, explicit_ids: set[int] | None = None,
                   admin: bool = False) -> bool:
    """
    The single answer used by posts/stories.

    `visibility` accepts both vocabularies (posts use 'everyone'/'public'
    interchangeably) so the frontend never has to translate.
    """
    if viewer_id == author_id or admin:
        return True
    vis = (visibility or "public").strip().lower()
    if vis in {"", "public", "everyone"}:
        return not blocked_between(db, conn, viewer_id, author_id)
    if blocked_between(db, conn, viewer_id, author_id):
        return False
    if vis in {"followers", "followers_only"}:
        return followed(db, conn, viewer_id, author_id)
    if vis == "friends":
        return is_friend(db, conn, viewer_id, author_id)
    if vis in {"private", "close"}:
        return bool(explicit_ids) and viewer_id in explicit_ids
    if vis in {"self", "private_only"}:
        return False
    return True


def visibility_sql_clause(db: Database, conn, viewer_id: int, *,
                          alias: str = "p", author_col: str = "user_id") -> tuple[str, list]:
    """
    Push visibility into SQL so a hidden row never leaves the database.

    Filtering in Python *after* a LIMIT would both leak private content into the
    page and silently shrink pages, which is the trap this avoids.

    Returns `(sql_fragment, params)` to be AND-ed into the WHERE clause.
    """
    a, col = alias, author_col
    # Every branch must name the visibility it allows. The earlier version ended
    # with an unconditional "NOT IN ('private','self')" OR-branch, which made
    # *every* row match and quietly defeated the whole clause — so a followers-
    # only post appeared in a stranger's feed.
    parts = [
        f"COALESCE({a}.{col}, 0) = ?",          # own content, any visibility
        f"COALESCE({a}.visibility, 'public') IN ('public', 'everyone', '')",
    ]
    params: list = [viewer_id]

    if viewer_id:
        friends = sorted(friends_of(db, conn, viewer_id))
        following = sorted(following_of(db, conn, viewer_id))
        if friends:
            marks = ", ".join("?" for _ in friends)
            parts.append(f"(COALESCE({a}.visibility,'') = 'friends' AND {a}.{col} IN ({marks}))")
            params.extend(friends)
        if following:
            marks = ", ".join("?" for _ in following)
            parts.append(f"(COALESCE({a}.visibility,'') = 'followers' AND {a}.{col} IN ({marks}))")
            params.extend(following)
        # 'private'/'self' intentionally have no branch: only the author sees them.

    sql = "(" + " OR ".join(parts) + ")"

    blocked = _blocked_pair_ids(db, conn, viewer_id) if viewer_id else []
    if blocked:
        marks = ", ".join("?" for _ in blocked)
        sql += f" AND {a}.{col} NOT IN ({marks})"
        params.extend(blocked)
    return sql, params


def _blocked_pair_ids(db: Database, conn, viewer_id: int) -> list[int]:
    """Everyone in a blocking relationship with the viewer, in either direction."""
    others: list[int] = []
    others += [int(r["other"]) for r in db.query(
        conn, "SELECT blocked_user_id AS other FROM user_blocks WHERE user_id = ?", (viewer_id,))]
    others += [int(r["other"]) for r in db.query(
        conn, "SELECT user_id AS other FROM user_blocks WHERE blocked_user_id = ?", (viewer_id,))]
    # a blocked friendship stores the *pair*, so pick the far side of it
    others += [int(r["user_b"]) if int(r["user_a"]) == viewer_id else int(r["user_a"])
               for r in db.query(conn, """SELECT user_a, user_b FROM friendships
                                          WHERE state = 'blocked' AND (user_a = ? OR user_b = ?)""",
                                 (viewer_id, viewer_id))]
    return sorted({o for o in others if o})


def level_of(visibility: str | None) -> int:
    return VISIBILITY_LEVELS.get((visibility or "public").strip().lower(), 0)
