"""
Feed construction and ranking.

Deliberately **deterministic and modular**: a `FeedScorer` turns engagement
signals into a number. Swap the class (or the weights in config) to change the
feed; nothing else in the codebase needs to know. No ML, no third-party API.

Score = Σ weight(signal) × relationship × media bonus, multiplied by a recency
decay with a configurable half-life. Because it is computed from counters that
already live on the row, ranking the whole feed is a single SQL pass — no
per-post loop (see the N+1 defect in docs/AUDIT.md §7).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import get_config
from .db import Database


@dataclass
class Signal:
    """Everything we know about one post, gathered in one query."""
    id: int
    likes: int = 0
    comments: int = 0
    views: int = 0
    reactions: int = 0
    has_media: bool = False
    age_hours: float = 0.0
    author_followed: bool = False
    author_friend: bool = False
    author_id: int = 0
    viewer_id: int = 0
    own_post: bool = False


class FeedScorer:
    """Base contract: signals in, number out."""

    name = "weighted"

    def score(self, s: Signal) -> float:                       # pragma: no cover
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name}


class WeightedDecayScorer(FeedScorer):
    """
    engagement = likes·w_like + comments·w_comment + views·w_view
    relationship multiplies engagement (a friend's post outranks a stranger's)
    recency   = 0.5 ** (age_hours / half_life)  -> 1.0 fresh, ~0 after 5 half-lives
    """

    name = "weighted_decay"

    def __init__(self, weights: dict | None = None):
        self.w = weights or get_config().feed_weights

    def score(self, s: Signal) -> float:
        w = self.w
        engagement = (s.likes * float(w.get("like", 3.0))
                      + s.comments * float(w.get("comment", 4.0))
                      + s.views * float(w.get("view", 0.4))
                      + s.reactions * float(w.get("like", 3.0)) * 0.5)
        if s.has_media:
            engagement += float(w.get("media_boost", 6.0))
        relationship = 1.0
        if s.author_followed:
            relationship += float(w.get("follow_boost", 12.0)) / 100.0 + 0.15
        if s.author_friend:
            relationship += float(w.get("friend_boost", 20.0)) / 100.0 + 0.2
        half_life = max(1.0, float(w.get("recency_hours_half_life", 18.0)))
        recency = 0.5 ** (max(0.0, s.age_hours) / half_life)
        # +1 so a brand-new post with zero engagement is never scored to zero.
        return (engagement + 1.0) * relationship * (0.25 + 0.75 * recency)

    def describe(self) -> dict:
        return {"name": self.name, "weights": dict(self.w)}


class ChronologicalScorer(FeedScorer):
    name = "chronological"

    def score(self, s: Signal) -> float:
        return -s.age_hours

    def describe(self) -> dict:
        return {"name": self.name}


class TrendingScorer(FeedScorer):
    """Engagement-weighted but with a much shorter half-life: 'what's hot now'."""

    name = "trending"

    def __init__(self, half_life_hours: float = 6.0):
        self.half_life = max(1.0, half_life_hours)

    def score(self, s: Signal) -> float:
        engagement = s.likes * 3.0 + s.comments * 4.0 + s.reactions * 1.5 + s.views * 0.3
        recency = 0.5 ** (max(0.0, s.age_hours) / self.half_life)
        return (engagement + 0.5) * recency

    def describe(self) -> dict:
        return {"name": self.name, "half_life_hours": self.half_life}


SCORERS: dict[str, FeedScorer] = {
    "recommended": WeightedDecayScorer(),
    "weighted": WeightedDecayScorer(),
    "recent": ChronologicalScorer(),
    "chronological": ChronologicalScorer(),
    "trending": TrendingScorer(),
    "popular": TrendingScorer(half_life_hours=24 * 14),
}


def get_scorer(mode: str | None) -> FeedScorer:
    """Unknown mode falls back to chronological — predictable, never random."""
    return SCORERS.get((mode or "recent").strip().lower(), ChronologicalScorer())


def age_hours(value, *, now: dt.datetime | None = None) -> float:
    """Hours since a DB timestamp string/datetime. Unknown age -> 0 (treated fresh)."""
    if value is None:
        return 0.0
    if isinstance(value, str):
        text = value.strip().replace("T", " ").split(".")[0].split("+")[0]
        try:
            value = dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                value = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
            except ValueError:
                return 0.0
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        ref = now or dt.datetime.utcnow()
        return max(0.0, (ref - value).total_seconds() / 3600.0)
    return 0.0


def rank(rows: list[dict], scorer: FeedScorer, *, viewer_id: int,
         followed: set[int], friends: set[int], now: dt.datetime | None = None) -> list[dict]:
    """Attach `_score`/`feed_reason` and sort. Rows keep every original field."""
    out = []
    for row in rows:
        sig = Signal(
            id=int(row.get("id") or 0),
            likes=int(row.get("likes_count") or 0),
            comments=int(row.get("comments_count") or 0),
            views=int(row.get("views") or row.get("view_count") or 0),
            reactions=int(row.get("reactions_count") or 0),
            has_media=bool(row.get("file_path")),
            age_hours=age_hours(row.get("timestamp") or row.get("created_at"), now=now),
            author_id=int(row.get("user_id") or 0),
            viewer_id=int(viewer_id or 0),
            author_followed=int(row.get("user_id") or 0) in followed,
            author_friend=int(row.get("user_id") or 0) in friends,
            own_post=int(row.get("user_id") or 0) == int(viewer_id or -1),
        )
        row = dict(row)
        row["feed_score"] = round(scorer.score(sig), 4)
        row["feed_reason"] = _reason(sig)
        out.append(row)
    out.sort(key=lambda r: (r["feed_score"], r["id"]), reverse=True)
    return out


def _reason(s: Signal) -> str:
    if s.own_post:
        return "own"
    if s.author_friend:
        return "friend"
    if s.author_followed:
        return "followed"
    if (s.likes + s.comments) == 0 and s.age_hours < 2:
        return "new"
    if s.likes + s.comments > 4:
        return "engaging"
    return "recent"


# --------------------------------------------------------------------------
# SQL builders shared by the feed endpoints
# --------------------------------------------------------------------------
FEED_SELECT = """
    SELECT p.*, u.username, u.full_name, u.avatar, u.accent,
           COALESCE(p.like_count, 0)    AS likes_count,
           COALESCE(p.comment_count, 0) AS comments_count,
           COALESCE(p.view_count, 0)    AS views,
           (SELECT COUNT(*) FROM post_reactions pr WHERE pr.post_id = p.id) AS reactions_count
    FROM posts p
    JOIN users u ON u.id = p.user_id
"""

# one round-trip instead of N: which of these posts has the viewer liked?
def liked_by_me_sql(db: Database) -> str:
    return """
    SELECT post_id FROM post_likes WHERE user_id = ?
      AND post_id IN ({placeholders})
    """


def relationships(db: Database, conn, viewer_id: int) -> tuple[set[int], set[int]]:
    """(followed ids, friend ids) for the viewer — two indexed queries."""
    followed = {r["following_id"] for r in db.query(
        conn, "SELECT following_id FROM follows WHERE follower_id = ?", (viewer_id,))}
    rows = db.query(conn, """
        SELECT user_a, user_b FROM friendships
        WHERE state = 'friends' AND (user_a = ? OR user_b = ?)""", (viewer_id, viewer_id))
    friends = {int(r["user_b"]) if int(r["user_a"]) == viewer_id else int(r["user_a"]) for r in rows}
    return followed, friends


def annotate_viewer(db: Database, conn, rows: list[dict], viewer_id: int,
                    followed: set[int], friends: set[int]) -> list[dict]:
    """
    Attach per-viewer flags in bulk.

    This is the fix for the old per-row correlated-subquery loop: two set
    lookups in Python instead of 2N queries.
    """
    if not rows:
        return rows
    ids = [int(r["id"]) for r in rows]
    marks = ", ".join("?" for _ in ids)
    liked = {r["post_id"] for r in db.query(
        conn, f"SELECT post_id FROM post_likes WHERE user_id = ? AND post_id IN ({marks})",
        [viewer_id, *ids])}
    saved = {r["post_id"] for r in db.query(
        conn, f"SELECT post_id FROM saved_posts WHERE user_id = ? AND post_id IN ({marks})",
        [viewer_id, *ids])}
    viewed = {r["post_id"] for r in db.query(
        conn, f"SELECT post_id FROM post_views WHERE user_id = ? AND post_id IN ({marks})",
        [viewer_id, *ids])}
    for row in rows:
        rid = int(row["id"])
        author = int(row.get("user_id") or 0)
        row["liked_by_me"] = rid in liked
        row["saved_by_me"] = rid in saved
        row["viewed_by_me"] = rid in viewed
        row["followed_by_me"] = author in followed
        row["friend_with_me"] = author in friends
        row.pop("_score", None)
    return rows
