"""
Posts, feed visibility, likes/comments and stories.

The properties that matter here are the ones the legacy build got wrong
(docs/AUDIT.md §3/§6): unbounded feeds, viewer-blind visibility, N+1 counts and
delete that destroyed media still referenced elsewhere.
"""

from __future__ import annotations

import datetime as dt

from tests.conftest import png_bytes


def _post(actor, content="hello volexturn", **extra):
    resp = actor.post("/api/posts", json={"content": content, **extra})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["post"]


# --------------------------------------------------------------------------
# legacy contract
# --------------------------------------------------------------------------
def test_legacy_posts_endpoint_is_a_bare_array(client, users):
    alice = users["alice"]
    _post(alice)
    resp = alice.get("/posts")
    body = resp.get_json()
    assert isinstance(body, list), "the SPA does Array.isArray(res) on /posts"
    first = body[0]
    for key in ("id", "content", "user_id", "timestamp", "username", "full_name", "avatar"):
        assert key in first, f"/posts lost {key}"


def test_legacy_create_post_still_returns_success_and_message(client, users):
    alice = users["alice"]
    resp = alice.post("/create_post", data={"content": "from the old SPA"})
    assert resp.status_code in (200, 201), resp.get_json()
    body = resp.get_json()
    assert body["success"] is True and body.get("message")


def test_users_endpoint_bare_array_with_online_flag(client, users):
    resp = users["alice"].get("/users")
    body = resp.get_json()
    assert isinstance(body, list)
    assert all("is_online" in u for u in body)


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------
def test_feed_is_paginated_and_bounded(app, client, users):
    alice, bob = users["alice"], users["bob"]
    for i in range(6):
        _post(alice, f"post number {i}")
    body = bob.get("/api/posts?limit=2").get_json()
    assert len(body["posts"]) == 2
    pg = body["pagination"]
    assert pg["limit"] == 2 and pg["total"] >= 6 and pg["has_more"] is True
    # a page can never exceed max_page_size, however large the requested limit is
    assert len(bob.get("/api/posts?limit=100000").get_json()["posts"]) <= 6
    assert bob.get("/api/posts?limit=100000").get_json()["pagination"]["limit"] == app.extensions[
        "volexturn_config"].max_page_size


def test_private_post_is_invisible_to_others_but_visible_to_owner(client, users):
    alice, bob = users["alice"], users["bob"]
    post = _post(alice, "private thought", visibility="private")
    bob_ids = {p["id"] for p in bob.get("/api/posts").get_json()["posts"]}
    assert post["id"] not in bob_ids
    alice_ids = {p["id"] for p in alice.get("/api/posts").get_json()["posts"]}
    assert post["id"] in alice_ids
    # and not readable by id either
    assert bob.get(f"/api/posts/{post['id']}").status_code == 404


def test_followers_only_post_requires_follow(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    post = _post(alice, "followers only", visibility="followers")
    assert post["id"] not in {p["id"] for p in bob.get("/api/posts").get_json()["posts"]}
    alice.post(f"/follow/{bob.id}", json={})            # alice follows bob? need bob->alice
    bob.post(f"/follow/{alice.id}", json={})
    assert post["id"] in {p["id"] for p in bob.get("/api/posts").get_json()["posts"]}
    # a third party still cannot see it
    assert post["id"] not in {p["id"] for p in carol.get("/api/posts").get_json()["posts"]}


def test_blocked_author_is_removed_from_the_feed(client, users):
    alice, bob = users["alice"], users["bob"]
    post = _post(alice, "block me if you can")
    assert post["id"] in {p["id"] for p in bob.get("/api/posts").get_json()["posts"]}
    bob.post("/api/users/block", json={"user_id": alice.id})
    assert post["id"] not in {p["id"] for p in bob.get("/api/posts").get_json()["posts"]}
    # blocking must not shrink *the author's* own feed
    assert post["id"] in {p["id"] for p in alice.get("/api/posts").get_json()["posts"]}


def test_feed_modes_and_scorer_are_reported(client, users):
    alice = users["alice"]
    _post(alice, "trend me")
    for mode in ("recent", "trending", "popular", "recommended", "following", "mine"):
        body = alice.get(f"/api/posts?mode={mode}").get_json()
        assert body["success"] is True and isinstance(body["posts"], list), mode
    body = alice.get("/api/posts?mode=trending").get_json()
    assert body["pagination"]["scorer"]["name"] == "trending", body["pagination"]
    # the scoring weights are stated, never hidden behind a black box
    assert "half_life_hours" in body["pagination"]["scorer"]


def test_hashtags_and_mentions_are_extracted(client, users):
    alice, bob = users["alice"], users["bob"]
    post = _post(alice, f"launch night @{bob.username} #game #night")
    assert set(post["tags"]) >= {"game", "night"}, post["tags"]
    got = alice.get("/api/posts?tag=game").get_json()
    assert any(p["id"] == post["id"] for p in got["posts"])


# --------------------------------------------------------------------------
# likes / comments
# --------------------------------------------------------------------------
def test_like_is_a_toggle_and_the_count_matches(client, users):
    alice, bob = users["alice"], users["bob"]
    post = _post(alice)
    pid = post["id"]
    first = bob.post(f"/api/posts/{pid}/like", json={}).get_json()
    assert first["liked"] is True and first["likes_count"] == 1
    second = bob.post(f"/api/posts/{pid}/like", json={}).get_json()
    assert second["liked"] is False and second["likes_count"] == 0
    bob.post(f"/like_post/{pid}", json={})                # legacy path, same effect
    shown = bob.get(f"/api/posts/{pid}").get_json()["post"]
    assert shown["likes_count"] == 1, shown
    assert shown.get("liked_by_me") is True or shown.get("is_liked") is True


def test_reaction_is_stored_separately_from_like(client, users):
    alice = users["alice"]
    pid = _post(alice)["id"]
    body = alice.post(f"/api/posts/{pid}/react", json={"emoji": "🔥"}).get_json()
    assert body["success"] and body["active"] is True
    detail = alice.get(f"/api/posts/{pid}").get_json()["post"]
    assert detail["reactions_count"] == 1, detail


def test_comments_nest_and_count_replies(client, users):
    alice, bob = users["alice"], users["bob"]
    pid = _post(alice)["id"]
    top = bob.post(f"/api/posts/{pid}/comments", json={"content": "first"}).get_json()["comment"]
    reply = alice.post(f"/api/posts/{pid}/comments",
                       json={"content": "reply", "parent_id": top["id"]}).get_json()["comment"]
    assert reply["parent_id"] == top["id"]
    comments = alice.get(f"/api/posts/{pid}/comments").get_json()["comments"]
    by_id = {c["id"]: c for c in comments}
    assert by_id[top["id"]]["reply_count"] == 1
    # the reply carries the parent's author inline so the UI renders
    # "پاسخ به …" without a second request per comment
    assert by_id[reply["id"]]["parent_id"] == top["id"]
    assert by_id[reply["id"]]["parent_name"] == top["full_name"]
    legacy = alice.get(f"/post_comments/{pid}").get_json()
    assert isinstance(legacy, list) and len(legacy) == 2      # bare array, flat


def test_post_author_can_delete_someone_elses_comment(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    pid = _post(alice)["id"]
    cid = bob.post(f"/api/posts/{pid}/comments", json={"content": "spam"}).get_json()["comment"]["id"]
    # a stranger cannot delete it
    assert carol.delete(f"/api/posts/comments/{cid}").status_code == 403
    assert alice.delete(f"/api/posts/comments/{cid}").status_code == 200
    assert all(c["id"] != cid for c in alice.get(f"/api/posts/{pid}/comments").get_json()["comments"])


def test_view_is_counted_once_per_user(client, users):
    alice, bob = users["alice"], users["bob"]
    pid = _post(alice)["id"]
    for _ in range(3):
        bob.post(f"/view_post/{pid}", json={})
    post = alice.get(f"/api/posts/{pid}").get_json()["post"]
    assert post["views"] == 1, post
    history = bob.get("/api/posts/history").get_json()
    assert any(h["id"] == pid for h in history["posts"]), history
    assert history["posts"][0]["viewed_at"]


def test_soft_delete_hides_everywhere_but_keeps_the_row(app, client, users):
    alice, bob = users["alice"], users["bob"]
    pid = _post(alice)["id"]
    alice.delete(f"/api/posts/{pid}")
    assert pid not in {p["id"] for p in bob.get("/api/posts").get_json()["posts"]}
    assert bob.get(f"/api/posts/{pid}").status_code == 404
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        row = db.query_one(conn, "SELECT deleted_at FROM posts WHERE id = ?", (pid,))
        assert row is not None and row["deleted_at"], "delete must be reversible, not annihilating"
    finally:
        db.close(conn)


def test_saved_and_pinned_are_per_user(client, users):
    alice, bob = users["alice"], users["bob"]
    pid = _post(alice)["id"]
    bob.post(f"/api/posts/{pid}/save", json={})
    assert pid in {p["id"] for p in bob.get("/api/posts/saved").get_json()["posts"]}
    assert pid not in {p["id"] for p in alice.get("/api/posts/saved").get_json()["posts"]}
    alice.post(f"/api/posts/{pid}/pin", json={})
    first = alice.get("/api/posts?mode=recent").get_json()["posts"][0]
    assert first["id"] == pid and first["is_pinned"]


# --------------------------------------------------------------------------
# stories
# --------------------------------------------------------------------------
def test_story_lifecycle_and_expiry(app, client, users):
    alice, bob = users["alice"], users["bob"]
    created = alice.upload("/api/stories", {"file": ("today.png", png_bytes())},
                           data={"caption": "today", "privacy": "everyone"})
    assert created.status_code in (200, 201), created.get_json()
    sid = created.get_json()["story"]["id"]
    assert sid in {s["id"] for s in bob.get("/api/stories").get_json()["stories"]}
    bob.post(f"/api/stories/{sid}/view", json={})
    bob.post(f"/api/stories/{sid}/view", json={})      # replayed -> still one view
    assert bob.get(f"/api/stories/{sid}/viewers").status_code == 403   # owner only
    viewers = alice.get(f"/api/stories/{sid}/viewers").get_json()
    assert {v["id"] for v in viewers["viewers"]} == {bob.id}
    assert created.get_json()["story"]["media_url"].startswith("/files/stories/")

    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        past = (dt.datetime.utcnow() - dt.timedelta(minutes=5)).isoformat(sep=" ")
        db.execute(conn, "UPDATE stories SET expires_at = ? WHERE id = ?", (past, sid)).close()
        conn.commit()
    finally:
        db.close(conn)
    assert sid not in {s["id"] for s in alice.get("/api/stories").get_json()["stories"]}
    archived = alice.get("/api/stories/me").get_json()
    assert any(s["id"] == sid and s.get("expired") for s in archived["stories"])


def test_story_followers_only_audience(client, users):
    alice, carol = users["alice"], users["carol"]
    sid = alice.upload("/api/stories", {"file": ("close.png", png_bytes())},
                       data={"caption": "close friends", "privacy": "followers"}
                       ).get_json()["story"]["id"]
    assert sid not in {s["id"] for s in carol.get("/api/stories").get_json()["stories"]}
    carol.post(f"/follow/{alice.id}", json={})
    assert sid in {s["id"] for s in carol.get("/api/stories").get_json()["stories"]}


def test_legacy_story_endpoints(client, users):
    alice = users["alice"]
    resp = alice.upload("/create_story", {"file": ("legacy.png", png_bytes())},
                        data={"caption": "legacy"})
    assert resp.status_code in (200, 201), resp.get_json()
    body = alice.get("/stories").get_json()
    assert isinstance(body, list) and body[0]["caption"] == "legacy"
