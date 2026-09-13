"""
Game servers, voice rooms and the gaming catalog.

These are the endpoints where the old code trusted the client most: the host
reported whatever status it liked, anyone could join anything, and a "probe"
would open a socket to any address the browser named. The tests below hold the
new lines — ownership of status, capacity and invitation rules, and a probe that
cannot be pointed at the metadata service.
"""

from __future__ import annotations

import datetime as dt

LAN_IP = "192.168.50.20"


def _server(actor, **extra):
    body = {"name": "Friday night", "game_name": "Counter-Strike 1.6",
            "ip_address": LAN_IP, "port": 27015, "max_players": 4, **extra}
    resp = actor.post("/api/servers", json=body)
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()


def _room(actor, **extra):
    body = {"name": "four slots", "max_players": 3, **extra}
    resp = actor.post("/api/rooms", json=body)
    assert resp.status_code in (200, 201), resp.get_json()
    return resp.get_json()


def _set_last_heartbeat(app, sid, when: dt.datetime) -> None:
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        db.execute(conn, "UPDATE lan_hosts SET last_heartbeat = ? WHERE id = ?",
                   (when.isoformat(sep=" "), sid)).close()
        conn.commit()
    finally:
        db.close(conn)


# --------------------------------------------------------------------------
# servers
# --------------------------------------------------------------------------
def test_created_server_starts_unknown_and_is_never_probed_eagerly(client, users):
    alice = users["alice"]
    body = _server(alice, description="de_dust2 only")
    sid = body["id"]
    assert body["server"]["status"] == "unknown"
    assert body["status_note"], "the client must be told why the dot is grey"
    # nothing in the row implies reachability, and secrets never travel
    assert "password_hash" not in body["server"], body["server"].keys()
    assert sid in [h["id"] for h in alice.get("/lan_hosts").get_json()]      # bare array


def test_password_is_stored_hashed_and_unlock_is_the_gate(client, users):
    alice, bob = users["alice"], users["bob"]
    sid = _server(alice, password="dr4gon-lair")["id"]
    wrong = bob.post(f"/api/servers/{sid}/unlock", json={"password": "nope"})
    assert wrong.status_code == 403
    assert wrong.get_json()["error"]["code"] == "BAD_SERVER_PASSWORD"
    ok = bob.post(f"/api/servers/{sid}/unlock", json={"password": "dr4gon-lair"}).get_json()
    assert ok["connect_string"] == f"{LAN_IP}:27015"
    # the owner never needs the password
    assert alice.post(f"/api/servers/{sid}/unlock", json={"password": ""}).status_code == 200
    # a server without a password refuses the "unlock" dance instead of pretending
    plain = _server(alice, name="open table")["id"]
    assert bob.post(f"/api/servers/{plain}/unlock", json={"password": "x"}).status_code == 409


def test_heartbeat_only_moves_status_for_the_owner(client, users):
    alice, bob = users["alice"], users["bob"]
    sid = _server(alice)["id"]
    stolen = bob.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 1})
    assert stolen.status_code == 403
    assert stolen.get_json()["error"]["code"] == "NOT_OWNER"

    beat = alice.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 2}).get_json()
    assert beat["status"] == "online" and beat["expires_in"] > 0
    detail = bob.get(f"/api/servers/{sid}").get_json()["server"]
    assert detail["players"] == 2 and detail["status"] == "online" and detail["players_known"]

    packed = alice.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 4}).get_json()
    assert packed["status"] == "full", "max_players is respected, not just reported"
    # a heartbeat is a claim about *liveness*; the player count is not invented
    empty = alice.post(f"/api/servers/{sid}/heartbeat", json={}).get_json()
    assert empty["status"] == "online"
    # a heartbeat with no count must not reset what the last one reported
    assert bob.get(f"/api/servers/{sid}").get_json()["server"]["players"] == 4


def test_stale_heartbeat_is_swept_to_offline_by_the_janitor(app, client, users):
    alice = users["alice"]
    sid = _server(alice)["id"]
    alice.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 1})
    _set_last_heartbeat(app, sid, dt.datetime.utcnow() - dt.timedelta(hours=6))
    from backend.janitor import run_once_for_tests
    run_once_for_tests(app, app.extensions["volexturn_db"], app.extensions["volexturn_storage"])
    out = alice.get(f"/api/servers/{sid}").get_json()["server"]
    assert out["status"] == "offline", out
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        # the transition is written down, so "when did it die" is answerable
        rows = db.query(conn, "SELECT status FROM server_status_log WHERE server_id = ? "
                              "ORDER BY id", (sid,))
    finally:
        db.close(conn)
    assert [r["status"] for r in rows] == ["unknown", "online", "offline"]


def test_probe_refuses_targets_outside_the_allowed_network(client, users):
    alice = users["alice"]
    meta = _server(alice, name="cloud", ip_address="169.254.169.254", port=80)["id"]
    res = alice.post(f"/api/servers/{meta}/probe", json={}).get_json()
    assert res["probed"] is False
    assert res["reason"] in {"discovery_disabled", "metadata_endpoint", "reserved_address"}
    lan = _server(alice, name="lan only")["id"]
    res2 = alice.post(f"/api/servers/{lan}/probe", json={}).get_json()
    assert res2["probed"] is False and res2["policy"]["enabled"] is False, \
        "with discovery off, no socket is opened at all"


def test_bad_targets_are_rejected_before_they_reach_the_database(client, users):
    alice = users["alice"]
    for bad in ({"ip_address": "not-an-ip"}, {"ip_address": "10.0.0.5", "port": 70000},
                {"ip_address": "10.0.0.5", "port": 0}, {"game_name": "x", "ip_address": "300.1.1.1"}):
        resp = alice.post("/api/servers", json={"name": "bad", **bad})
        assert resp.status_code == 400, (bad, resp.get_json())
        assert resp.get_json()["error"]["code"] in {"BAD_HOST", "BAD_IP", "BAD_TARGET",
                                                   "BAD_PORT", "HOST_NOT_ALLOWED"}
    # a server row is only ever created for a well-formed target
    assert alice.get("/api/servers").get_json()["pagination"]["total"] == 0


def test_join_and_leave_track_the_player_list(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    sid = _server(alice, max_players=2)["id"]
    alice.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 0})
    joined = bob.post(f"/api/servers/{sid}/join", json={}).get_json()
    assert joined["joined"] is True and joined["players"] == 1
    again = bob.post(f"/api/servers/{sid}/join", json={})
    assert again.status_code == 409 and again.get_json()["error"]["code"] == "ALREADY_JOINED"
    carol.post(f"/api/servers/{sid}/join", json={})
    third = alice.post(f"/api/servers/{sid}/join", json={})
    assert third.status_code == 409, "capacity is enforced"
    assert bob.delete is not None
    left = bob.post(f"/api/servers/{sid}/leave", json={}).get_json()
    assert left["removed"] == 1
    offline = _server(alice, name="quiet box")["id"]
    alice.post(f"/api/servers/{offline}/offline", json={})
    late = carol.post(f"/api/servers/{offline}/join", json={})
    assert late.status_code == 409 and late.get_json()["error"]["code"] == "SERVER_OFFLINE"


def test_offline_claim_needs_ownership(client, users):
    alice, bob = users["alice"], users["bob"]
    sid = _server(alice)["id"]
    assert bob.post(f"/api/servers/{sid}/offline", json={}).status_code == 403
    assert alice.post(f"/api/servers/{sid}/offline", json={}).get_json()["status"] == "offline"
    assert alice.get("/api/servers/stats").get_json()["totals"]["offline"] >= 1


def test_follow_notify_and_delete_archive(client, users, admin):
    alice, bob = users["alice"], users["bob"]
    sid = _server(alice)["id"]
    fav = bob.post(f"/api/servers/{sid}/follow", json={}).get_json()
    assert fav["following"] is True and fav["followers"] == 2     # owner is auto-followed
    seen = bob.get("/api/servers?scope=following").get_json()["servers"]
    assert [s["id"] for s in seen] == [sid]
    alice.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 3})
    notes = bob.get("/api/notifications").get_json()["items"]
    # the follower is told which way the dot moved, not just that it moved
    assert any(n["type"] == "server_online" for n in notes), notes
    archived = alice.delete(f"/api/servers/{sid}").get_json()
    assert archived["mode"] == "archived"
    # an archive is not a licence to delete history: the owner still reads it,
    # a stranger gets the same answer as if the row were gone
    assert alice.get(f"/api/servers/{sid}").status_code == 200
    assert bob.get(f"/api/servers/{sid}").status_code == 404
    assert sid not in [x["id"] for x in bob.get("/api/servers").get_json()["servers"]]
    # and purging is admin-only, since it destroys the audit trail
    assert bob.delete(f"/api/servers/{sid}?purge=1").status_code == 403


def test_manual_offline_switch_is_respected_until_a_heartbeat_arrives(client, users):
    alice = users["alice"]
    sid = _server(alice)["id"]
    listed = alice.get("/api/servers").get_json()["servers"][0]
    assert listed["status"] == "unknown"
    alice.post(f"/api/servers/{sid}/offline", json={})
    assert alice.get(f"/api/servers/{sid}").get_json()["server"]["manual_status"] == 1
    alice.post(f"/api/servers/{sid}/heartbeat", json={"players_online": 1})
    row = alice.get(f"/api/servers/{sid}").get_json()["server"]
    assert row["manual_status"] == 0 and row["status"] == "online"


# --------------------------------------------------------------------------
# rooms
# --------------------------------------------------------------------------
def test_room_create_and_list(client, users):
    alice = users["alice"]
    body = _room(alice, description="mic check at 9", ttl_minutes=120)
    rid = body["id"]
    assert body["room"]["voice_enabled"] == 1
    assert body["room"]["player_count"] == 1 and body["room"]["is_host"] is True
    assert rid in [r["id"] for r in alice.get("/api/rooms").get_json()["rooms"]]
    assert rid in [r["id"] for r in alice.get("/api/rooms/mine").get_json()["rooms"]]
    short = alice.post("/api/rooms", json={"name": "ab"})
    assert short.status_code == 400 and short.get_json()["error"]["code"] == "NAME_TOO_SHORT"


def test_room_capacity_invites_and_password(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    tiny = _room(alice, name="duo", max_players=2)["id"]
    assert bob.post(f"/api/rooms/{tiny}/join", json={}).status_code == 200
    assert carol.post(f"/api/rooms/{tiny}/join", json={}).status_code == 409
    assert carol.get(f"/api/rooms/{tiny}").get_json()["room"]["full"] is True
    assert carol.get(f"/api/rooms/{tiny}").get_json()["room"]["joinable"] is False

    invite_only = _room(alice, name="inner", visibility="invite")["id"]
    blocked = bob.post(f"/api/rooms/{invite_only}/join", json={})
    assert blocked.status_code == 403 and blocked.get_json()["error"]["code"] == "INVITE_ONLY"
    alice.post(f"/api/rooms/{invite_only}/invite", json={"user_ids": [bob.id]})
    assert bob.post(f"/api/rooms/{invite_only}/join", json={}).status_code == 200
    assert bob.get("/api/rooms/invites").get_json()["invites"] == []      # consumed

    locked = _room(alice, name="with code", password="4242")["id"]
    nope = bob.post(f"/api/rooms/{locked}/join", json={"password": "0000"})
    assert nope.status_code == 403 and nope.get_json()["error"]["code"] == "BAD_ROOM_PASSWORD"
    missing = bob.post(f"/api/rooms/{locked}/join", json={})
    assert missing.status_code == 403 and missing.get_json()["error"]["code"] == "PASSWORD_REQUIRED"
    assert bob.post(f"/api/rooms/{locked}/join", json={"password": "4242"}).status_code == 200


def test_room_flow_ready_start_stop_kick(client, users):
    alice, bob = users["alice"], users["bob"]
    rid = _room(alice, name="competitive")["id"]
    bob.post(f"/api/rooms/{rid}/join", json={})
    assert bob.post(f"/api/rooms/{rid}/ready", json={"ready": True}).get_json()["ready"] is True
    detail = alice.get(f"/api/rooms/{rid}").get_json()["room"]
    # the host is listed first, and `ready` is what the start button waits for
    assert {p["user_id"]: int(p["ready"]) for p in detail["participants"]} == {alice.id: 0, bob.id: 1}
    assert detail["participants"][0]["role"] == "host"

    guest = bob.post(f"/api/rooms/{rid}/start", json={})
    assert guest.status_code == 403 and guest.get_json()["error"]["code"] == "NOT_HOST"
    started = alice.post(f"/api/rooms/{rid}/start", json={}).get_json()
    assert started["status"] == "ingame"
    assert alice.post(f"/api/rooms/{rid}/stop", json={}).get_json()["status"] == "open"
    kicked = alice.post(f"/api/rooms/{rid}/kick", json={"user_id": bob.id}).get_json()
    assert kicked["removed"] == 1 and kicked["players"] == 1, "the host is all that is left"
    assert bob.post(f"/api/rooms/{rid}/kick", json={"user_id": alice.id}).status_code == 403


def test_room_messages_are_member_only_and_paged(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    rid = _room(alice, name="talk")["id"]
    posted = alice.post(f"/api/rooms/{rid}/messages", json={"content": "gg"}).get_json()
    assert posted["message"]["sender_id"] == alice.id
    assert bob.post(f"/api/rooms/{rid}/messages", json={"content": "nope"}).status_code == 403
    bob.post(f"/api/rooms/{rid}/join", json={})
    bob.post(f"/api/rooms/{rid}/messages", json={"content": "rematch"})
    # a page is requested newest-first and handed back oldest-first, so a
    # client can prepend `?limit=N` while still rendering top-to-bottom
    thread = alice.get(f"/api/rooms/{rid}/messages?limit=1").get_json()
    assert len(thread["messages"]) == 1 and thread["pagination"]["total"] == 2
    assert thread["messages"][0]["content"] == "rematch"
    assert thread["messages"][0]["user_id"] == bob.id == thread["messages"][0]["sender_id"]
    full = alice.get(f"/api/rooms/{rid}/messages").get_json()["messages"]
    assert [m["content"] for m in full] == ["gg", "rematch"]
    assert carol.get(f"/api/rooms/{rid}/messages").status_code == 403
    assert carol.post(f"/api/rooms/{rid}/messages", json={"content": "hi"}).status_code == 403


def test_leaving_closes_the_room_only_for_the_host(client, users):
    alice, bob = users["alice"], users["bob"]
    rid = _room(alice, name="solo-ish")["id"]
    bob.post(f"/api/rooms/{rid}/join", json={})
    left = bob.post(f"/api/rooms/{rid}/leave", json={}).get_json()
    assert left["closed_room"] is False
    assert alice.get(f"/api/rooms/{rid}").get_json()["room"]["player_count"] == 1
    gone = alice.post(f"/api/rooms/{rid}/leave", json={}).get_json()
    assert gone["closed_room"] is True
    assert bob.post(f"/api/rooms/{rid}/join", json={}).status_code == 409


def test_stale_rooms_are_closed_by_the_sweep(app, client, users):
    alice, bob = users["alice"], users["bob"]
    rid = _room(alice, name="forgotten")["id"]
    bob.post(f"/api/rooms/{rid}/join", json={})
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        # the sweep reads last_activity_at, which every room action refreshes
        db.execute(conn, "UPDATE game_rooms SET last_activity_at = ? WHERE id = ?",
                   ((dt.datetime.utcnow() - dt.timedelta(minutes=app.extensions["volexturn_config"]
                     .room_stale_minutes + 30)).isoformat(sep=" "), rid)).close()
        conn.commit()
    finally:
        db.close(conn)
    from backend.janitor import run_once_for_tests
    run_once_for_tests(app, app.extensions["volexturn_db"], app.extensions["volexturn_storage"])
    assert alice.get(f"/api/rooms/{rid}").get_json()["room"]["status"] == "closed"


# --------------------------------------------------------------------------
# gaming catalog
# --------------------------------------------------------------------------
def test_catalog_reads_and_admin_writes(client, users, admin):
    alice = users["alice"]
    games = alice.get("/api/games").get_json()["games"]
    assert games and {"id", "slug", "name"} <= set(games[0])
    first = games[0]["id"]
    assert alice.get(f"/api/games/{first}").get_json()["game"]["id"] == first
    slug = games[0]["slug"]
    assert alice.get(f"/api/games/slug/{slug}").get_json()["game"]["slug"] == slug
    meta = alice.get("/api/games/meta").get_json()
    assert meta["genres"] and meta["platforms"] and meta["providers"]
    assert "playing" in meta["library_statuses"]
    assert all(g["id"] in {x["id"] for x in meta["games"]} for g in games)

    denied = alice.post("/api/games", json={"name": "New Game", "slug": "new-game"})
    assert denied.status_code == 403, "the catalog is admin-curated"
    made = admin.post("/api/games", json={"name": "Marathon", "slug": "marathon",
                                         "default_port": 7777})
    assert made.status_code == 201, made.get_json()
    gid = made.get_json()["id"]
    assert admin.patch(f"/api/games/{gid}", json={"description": "extraction"}).status_code == 200
    assert alice.get("/api/games/slug/marathon").get_json()["game"]["default_port"] == 7777
    assert admin.delete(f"/api/games/{gid}").status_code == 200
    assert alice.get("/api/games/slug/marathon").status_code == 404


def test_library_hours_and_profile(client, users):
    alice, bob = users["alice"], users["bob"]
    gid = alice.get("/api/games").get_json()["games"][0]["id"]
    added = alice.post(f"/api/games/library/{gid}", json={"status": "playing"}).get_json()
    assert added["status"] == "playing" and added["game"]
    bogus = alice.post(f"/api/games/library/{gid}", json={"status": "installed"})
    assert bogus.status_code == 400 and bogus.get_json()["error"]["code"] == "BAD_STATUS"
    lib = alice.get("/api/games/library").get_json()
    assert [g["id"] for g in lib["games"]] == [gid]
    assert lib["counts"]["playing"] == 1
    logged = alice.post(f"/api/games/library/{gid}/hours", json={"minutes": 90}).get_json()
    assert logged["added_minutes"] == 90 and float(logged["total_hours"]) == 1.5
    # only the owner's flags are private, the profile itself is readable
    prof = bob.get(f"/api/users/{alice.id}/gaming").get_json()
    assert prof["success"]
    assert alice.post("/api/games/profile",
                      json={"gamertag": "viper", "tagline": "AWPer main"}).status_code == 200
    mine = alice.get("/api/games/profile").get_json()["profile"]
    assert mine["gamertag"] == "viper" and mine["tagline"] == "AWPer main"
    assert mine["stats"]["games_playing"] == 1 and mine["stats"]["rooms_hosted"] >= 0
    # gamertags are unique, and the collision is reported instead of overwritten
    clash = bob.post("/api/games/profile", json={"gamertag": "viper"})
    assert clash.status_code == 409 and clash.get_json()["error"]["code"] == "GAMERTAG_TAKEN"
    assert alice.get("/api/games/recent").get_json()["games"], "a library entry counts as played"
    assert alice.post(f"/api/games/library/{gid}", json={"remove": True}).status_code == 200
    assert alice.get("/api/games/library").get_json()["games"] == []
