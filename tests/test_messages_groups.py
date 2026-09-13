"""
Direct messages and groups: the parts that used to be trust-based.

The old server let any client name its own `sender_id`, let a removed member
keep reading `group_messages/<id>` forever, and had no mute/ban/invite at all.
These tests pin the new rules down: identity comes from the session, membership
is re-checked on every read, and moderation actually sticks.
"""

from __future__ import annotations


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _dm(sender, to_id, text, **extra):
    resp = sender.post("/api/messages", json={"receiver_id": to_id, "content": text, **extra})
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()["message"]


def _group(leader, name="release crew", **extra):
    resp = leader.post("/api/groups", json={"name": name, **extra})
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()["group_id"]


def _say(actor, gid, text):
    resp = actor.post("/api/messages", json={"group_id": gid, "content": text})
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()["message"]


def _ids(body):
    return [m["id"] for m in (body.get("messages") if isinstance(body, dict) else body)]


# --------------------------------------------------------------------------
# direct messages
# --------------------------------------------------------------------------
def test_dm_is_visible_to_both_sides_and_legacy_shape_holds(client, users):
    alice, bob = users["alice"], users["bob"]
    msg = _dm(alice, bob.id, "ship it tonight")
    assert msg["sender_id"] == alice.id and msg["receiver_id"] == bob.id
    assert "file_url" not in msg, "a text message must not advertise an empty media url"

    thread = bob.get(f"/api/messages/thread/{alice.id}").get_json()
    assert thread["messages"][0]["content"] == "ship it tonight"
    assert thread["messages"][0]["can_delete"] is True          # either side may remove
    assert thread["messages"][0]["can_edit"] is False           # only the sender may edit

    legacy = bob.get(f"/messages/{bob.id}/{alice.id}").get_json()
    assert isinstance(legacy, list), "legacy thread contract is a bare array"
    assert legacy[0]["id"] == msg["id"]


def test_sender_identity_cannot_be_forged(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    resp = alice.post("/send_message", data={"receiver_id": bob.id, "content": "hi",
                                            "sender_id": carol.id})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "SENDER_MISMATCH"
    # the honest legacy call still works, and the session wins over the form
    ok = alice.post("/send_message", data={"receiver_id": bob.id, "content": "hi",
                                          "sender_id": alice.id})
    assert ok.status_code in (200, 201), ok.get_json()
    assert ok.get_json()["message"]["sender_id"] == alice.id


def test_message_needs_exactly_one_destination(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice)
    neither = alice.post("/api/messages", json={"content": "hello?"})
    assert neither.status_code == 400
    assert neither.get_json()["error"]["code"] == "DESTINATION_REQUIRED"
    both = alice.post("/api/messages", json={"content": "x", "receiver_id": bob.id,
                                            "group_id": gid})
    assert both.status_code == 400
    # empty text and no file is rejected rather than stored as a blank row
    blank = alice.post("/api/messages", json={"receiver_id": bob.id, "content": "   "})
    assert blank.status_code == 400
    assert blank.get_json()["error"]["code"] == "EMPTY_MESSAGE"


def test_blocked_users_cannot_message_each_other(client, users):
    alice, bob = users["alice"], users["bob"]
    assert bob.post("/api/users/block", json={"user_id": alice.id}).status_code == 200
    resp = alice.post("/api/messages", json={"receiver_id": bob.id, "content": "let me in"})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "BLOCKED"
    bob.post("/api/users/unblock", json={"user_id": alice.id})
    assert alice.post("/api/messages", json={"receiver_id": bob.id, "content": "hi"}).status_code == 201


def test_closed_dms_block_strangers_but_not_friends(client, users):
    alice, bob = users["alice"], users["bob"]
    assert bob.post("/api/notifications/preferences", json={"dm_from": 1}).status_code == 200
    resp = alice.post("/api/messages", json={"receiver_id": bob.id, "content": "yo"})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "DM_CLOSED"
    # becoming friends reopens the door (a friendship implies mutual follows)
    alice.post("/api/users/friends/request", json={"user_id": bob.id})
    assert bob.post("/api/users/friends/respond", json={"user_id": alice.id, "accept": True}).status_code == 200
    assert alice.post("/api/messages", json={"receiver_id": bob.id, "content": "yo"}).status_code == 201


def _raw_rows(app, ids):
    """Rows as the database still holds them, ignoring every read filter."""
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        marks = ", ".join("?" for _ in ids)
        return [dict(r) for r in db.query(
            conn, f"SELECT * FROM messages WHERE id IN ({marks})", tuple(ids))]
    finally:
        db.close(conn)


def test_edit_and_delete_scopes(app, client, users):
    alice, bob = users["alice"], users["bob"]
    mine = _dm(alice, bob.id, "typo")
    theirs = _dm(bob, alice.id, "not yours")

    edited = alice.patch(f"/api/messages/{mine['id']}", json={"content": "fixed"}).get_json()
    assert edited["message"]["content"] == "fixed"
    assert bob.get(f"/api/messages/thread/{alice.id}").get_json()["messages"][0]["edited_at"]

    forbidden = alice.patch(f"/api/messages/{theirs['id']}", json={"content": "hijack"})
    assert forbidden.status_code == 403

    # "for me" hides it on one side only
    alice.delete(f"/api/messages/{mine['id']}", json={"scope": "me"})
    assert mine["id"] not in _ids(alice.get(f"/api/messages/thread/{bob.id}").get_json())
    assert mine["id"] in _ids(bob.get(f"/api/messages/thread/{alice.id}").get_json())

    everyone = alice.delete(f"/api/messages/{mine['id']}", json={"scope": "everyone"}).get_json()
    assert everyone["mode"] == "everyone"
    # the tombstone leaves both threads, but the row survives with its id, so
    # replies and forwards made before the deletion still resolve
    assert mine["id"] not in _ids(bob.get(f"/api/messages/thread/{alice.id}").get_json())
    assert mine["id"] in {r["id"] for r in _raw_rows(app, [mine["id"]])}
    assert _raw_rows(app, [mine["id"]])[0]["deleted_for_everyone"] == 1
    assert _raw_rows(app, [mine["id"]])[0]["content"] == ""


def test_reactions_are_counts_not_a_single_flag(client, users):
    alice, bob = users["alice"], users["bob"]
    msg = _dm(alice, bob.id, "🎉 party at 9")
    first = bob.post(f"/api/messages/{msg['id']}/react", json={"emoji": "🎉"}).get_json()
    assert first["active"] is True and first["counts"] == {"🎉": 1}
    second = alice.post(f"/api/messages/{msg['id']}/react", json={"emoji": "🎉"}).get_json()
    assert second["counts"] == {"🎉": 2}
    off = alice.post(f"/api/messages/{msg['id']}/react", json={"emoji": "🎉"}).get_json()
    assert off["active"] is False and off["counts"] == {"🎉": 1}
    outsider = users["carol"].post(f"/api/messages/{msg['id']}/react", json={"emoji": "🎉"})
    assert outsider.status_code == 403          # not a participant


def test_unread_counts_and_seen_marks(client, users):
    alice, bob = users["alice"], users["bob"]
    a = _dm(alice, bob.id, "one")
    _dm(alice, bob.id, "two")
    counts = bob.get("/api/messages/unread").get_json()
    assert counts[str(alice.id)] == 2, "legacy shape is a bare sender->count map"
    assert a["id"]

    seen = bob.post(f"/api/messages/seen/{alice.id}", json={}).get_json()
    assert seen["seen"] == 2
    assert bob.get("/api/messages/unread").get_json() == {}
    # the legacy alias agrees
    assert alice.get(f"/unread_counts/{alice.id}").get_json() == {}
    assert alice.get(f"/api/messages/{a['id']}/seen-by").get_json()["seen"] is True


def test_partners_list_carries_last_message_and_unread(client, users):
    alice, bob = users["alice"], users["bob"]
    _dm(alice, bob.id, "last words")
    out = alice.get("/api/messages/partners").get_json()["partners"]
    row = [p for p in out if p["id"] == bob.id][0]
    assert row["last_message"]["content"] == "last words"
    assert row["unread"] == 0


def test_forward_keeps_original_author(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    msg = _dm(alice, bob.id, "forwardable note")
    res = bob.post("/api/messages/forward", json={"message_id": msg["id"],
                                                 "targets": [{"type": "user", "id": carol.id}]}).get_json()
    assert res["sent"] == 1, res          # the SPA prints this number directly
    thread = carol.get(f"/api/messages/thread/{bob.id}").get_json()["messages"]
    copy = [m for m in thread if m["id"] != msg["id"]][-1]
    assert copy["sender_id"] == bob.id, "the forwarder is the sender of the copy"
    assert "forwardable note" in copy["content"]
    assert copy["forwarded"] == 1 or copy.get("forwarded_from")


def test_search_uses_escaped_casefolded_like(client, users):
    alice, bob = users["alice"], users["bob"]
    _dm(alice, bob.id, "deploy at 21:00 %70 off")
    hit = bob.get("/api/messages/search?q=deploy").get_json()
    assert hit["query"] == "deploy" and hit["messages"], hit
    # `_` and `%` are literals, not wildcards — this is what the old LIKE did wrong
    assert bob.get("/api/messages/search?q=%2570").get_json()["messages"]
    too_short = alice.get("/api/messages/search?q=d").get_json()
    assert too_short["error"]["code"] == "QUERY_TOO_SHORT"


def test_pinned_messages_are_per_scope(client, users):
    alice, bob = users["alice"], users["bob"]
    keep = _dm(alice, bob.id, "the address is 22 oak st")
    other = _dm(alice, bob.id, "temporary")
    pin = alice.post(f"/api/messages/{keep['id']}/pin", json={}).get_json()
    assert pin["pinned"] is True
    pinned = alice.get(f"/api/messages/pinned?partner={bob.id}").get_json()["messages"]
    assert [m["id"] for m in pinned] == [keep["id"]]
    assert alice.post(f"/api/messages/{keep['id']}/pin", json={}).get_json()["pinned"] is False
    assert not alice.get(f"/api/messages/pinned?partner={bob.id}").get_json()["messages"]
    assert other["id"] in [m["id"] for m in
                           alice.get(f"/api/messages/thread/{bob.id}").get_json()["messages"]]


# --------------------------------------------------------------------------
# groups
# --------------------------------------------------------------------------
def test_group_creation_and_legacy_listing(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "launch team", members=[bob.id])
    mine = alice.get("/api/groups").get_json()["groups"]
    row = [g for g in mine if g["id"] == gid][0]
    assert row["role"] == "owner" and row["member_count"] == 2
    legacy = bob.get("/my_groups").get_json()
    assert isinstance(legacy, list) and legacy[0]["id"] == gid
    info = bob.get(f"/api/groups/{gid}").get_json()
    assert {m["id"] for m in info["members"]} == {alice.id, bob.id}
    assert info["group"]["name"] == "launch team"
    # the SPA reads `info.group` / `info.members` off /group_info too, so the
    # legacy alias must keep the wrapped shape rather than a flattened one
    assert bob.get(f"/group_info/{gid}").get_json()["group"]["creator_id"] == alice.id


def test_removed_member_loses_read_access_immediately(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "closed room", members=[bob.id])
    _say(bob, gid, "before removal")
    assert gid in [g["id"] for g in bob.get("/my_groups").get_json()]
    alice.post(f"/api/groups/{gid}/members/{bob.id}/remove", json={})
    assert bob.get(f"/api/groups/{gid}/messages").status_code == 403
    assert bob.post("/api/messages", json={"group_id": gid, "content": "sneak"}).status_code == 403
    assert gid not in [g["id"] for g in bob.get("/my_groups").get_json()]
    # the messages themselves are not rewritten — other members still read them
    assert "before removal" in alice.get(f"/api/groups/{gid}/messages").get_json()["messages"][0]["content"]


def test_mute_sticks_until_it_is_lifted(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "moderated", members=[bob.id])
    alice.post(f"/api/groups/{gid}/members/{bob.id}/mute", json={"minutes": 30})
    muted = bob.post("/api/messages", json={"group_id": gid, "content": "hello?"})
    assert muted.status_code == 403
    assert muted.get_json()["error"]["code"] == "MUTED"
    alice.post(f"/api/groups/{gid}/members/{bob.id}/mute", json={"minutes": 0})
    assert bob.post("/api/messages", json={"group_id": gid, "content": "hello"}).status_code == 201


def test_admins_only_group_silences_members_not_admins(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    gid = _group(alice, "announcements", members=[bob.id])
    alice.patch(f"/api/groups/{gid}", json={"only_admins_post": True})
    assert bob.post("/api/messages", json={"group_id": gid, "content": "x"}).status_code == 403
    alice.post(f"/api/groups/{gid}/members/{carol.id}/role", json={"role": "admin"})
    assert alice.post("/api/messages", json={"group_id": gid, "content": "read this"}).status_code == 201


def test_owner_role_is_sticky_and_transfer_is_explicit(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "two heads", members=[bob.id])
    demote = alice.post(f"/api/groups/{gid}/members/{alice.id}/role", json={"role": "member"})
    assert demote.status_code == 409
    assert demote.get_json()["error"]["code"] == "OWNER_STICKY"
    assert alice.post(f"/api/groups/{gid}/members/{bob.id}/role",
                      json={"role": "admin"}).get_json()["role"] == "admin"
    # a plain member cannot promote anyone
    assert bob.post(f"/api/groups/{gid}/members/{bob.id}/role",
                    json={"role": "owner"}).status_code in (403, 409)


def test_ban_beats_invite_and_unban_restores_access(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "no bob", members=[bob.id])
    alice.post(f"/api/groups/{gid}/members/{bob.id}/ban", json={"reason": "spam"})
    assert bob.get(f"/api/groups/{gid}/messages").status_code == 403
    code = alice.post(f"/api/groups/{gid}/invite", json={}).get_json()["code"]
    joined = bob.post(f"/api/groups/join/{code}", json={})
    assert joined.status_code == 403
    assert joined.get_json()["error"]["code"] == "BANNED"
    assert bob.id in {r["user_id"] for r in alice.get(f"/api/groups/{gid}/bans").get_json()["bans"]}
    alice.post(f"/api/groups/{gid}/unban/{bob.id}", json={})
    assert bob.post(f"/api/groups/join/{code}", json={}).status_code == 200


def test_invite_code_expiry_and_use_limit(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    gid = _group(alice, "one seat")
    one = alice.post(f"/api/groups/{gid}/invite", json={"max_uses": 1, "hours": 1}).get_json()
    assert one["join_url"].endswith(one["code"])
    assert bob.post(f"/api/groups/join/{one['code']}", json={}).status_code == 200
    used = carol.post(f"/api/groups/join/{one['code']}", json={})
    assert used.status_code == 409
    assert used.get_json()["error"]["code"] == "INVITE_USED_UP"
    assert alice.post("/api/groups/join/nosuchcode", json={}).status_code == 404


def test_group_unread_uses_a_per_member_cursor(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "inbox", members=[bob.id])
    first = _say(alice, gid, "one")
    _say(alice, gid, "two")
    unread = bob.get("/api/messages/unread/groups").get_json()["unread"]
    assert unread[str(gid)] == 2, unread
    bob.post(f"/api/groups/{gid}/seen", json={"last_message_id": first["id"]})
    assert bob.get("/api/messages/unread/groups").get_json()["unread"][str(gid)] == 1


def test_group_search_matches_members_by_name(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "findable", members=[bob.id])
    _say(bob, gid, "the venue is booked for Friday")
    out = alice.get(f"/api/groups/{gid}/search?q=venue").get_json()
    assert out["messages"] and out["messages"][0]["content"].startswith("the venue")
    # legacy contract: /group_messages/<gid> is a bare array
    raw = alice.get(f"/api/groups/{gid}/messages").get_json()
    assert isinstance(raw, dict) and len(raw["messages"]) == 1
    assert isinstance(alice.get(f"/group_messages/{gid}").get_json(), list)


def test_group_delete_leaves_no_orphans(app, client, users):
    alice, bob = users["alice"], users["bob"]
    gid = _group(alice, "temporary", members=[bob.id])
    msg = _say(alice, gid, "bye")
    assert alice.delete(f"/api/groups/{gid}").status_code == 200
    assert alice.get(f"/api/groups/{gid}").status_code == 404
    # ex-members lose access because membership rows are dropped hard
    assert bob.get(f"/api/groups/{gid}/messages").status_code in (403, 404)
    assert gid not in [g["id"] for g in alice.get("/my_groups").get_json()]
    # …but the group row and its messages survive for audit
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        grp = db.query_one(conn, "SELECT deleted_at FROM groups WHERE id = ?", (gid,))
        assert grp is not None and grp["deleted_at"], "group delete must be soft"
        still = db.query_one(conn, "SELECT content FROM messages WHERE id = ?", (msg["id"],))
        assert still["content"] == "bye"
        assert db.scalar(conn, "SELECT COUNT(*) FROM group_members WHERE group_id = ?", (gid,)) == 0
    finally:
        db.close(conn)

