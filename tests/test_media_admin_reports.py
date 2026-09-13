"""
Uploads, moderation and the realtime contract.

Three areas the old build got wrong in three different ways: media was trusted
because the browser said so, admin endpoints answered to any logged-in user,
and the socket layer accepted a `user_id` from the client. The tests here keep
the fixes honest.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.conftest import make_config, png_bytes, register

PNG = png_bytes()


def _reason(client) -> str:
    """First report reason, whatever the catalogue is keyed by."""
    body = client.get("/api/reports/reasons").get_json()["reasons"]
    return body[0] if isinstance(body, list) else sorted(body)[0]


@pytest.fixture()
def tiny_app(tmp_path):
    """A second app whose image cap is 1 MB, so the size path is reachable."""
    from backend.app import create_app
    cfg = replace(make_config(tmp_path), max_image_mb=1)
    app = create_app(config=cfg)
    return app


@pytest.fixture()
def tiny_client(tiny_app):
    tiny_app.config.update(TESTING=True)
    c = tiny_app.test_client()
    c.environ_base = {"HTTP_ORIGIN": "http://localhost", "HTTP_REFERER": "http://localhost/"}
    return c


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------
def test_image_upload_is_stored_under_a_generated_name_and_served(app, client, users):
    alice = users["alice"]
    resp = alice.upload("/api/posts", {"file": ("my photo.png", PNG)}, content="look at this")
    assert resp.status_code in (200, 201), resp.get_json()
    post = resp.get_json()["post"]
    # the SPA builds the url itself (`/files/posts/${p.file_path}`), so what
    # matters is that the stored name is generated, never the client's filename
    name = post["file_path"]
    assert name and "photo" not in name and ".." not in name
    url = f"/files/posts/{name}"
    served = alice.get(url)
    assert served.status_code == 200
    assert served.mimetype == "image/png"
    root = app.extensions["volexturn_storage"].root
    assert (root / "posts" / name).exists()


def test_lying_about_the_file_type_is_rejected(client, users):
    alice = users["alice"]
    # an .png name around a php payload is exactly the upload that used to work
    bad = alice.upload("/api/posts", {"file": ("shell.png", b"<?php system($_GET['c']); ?>")},
                       content="hi")
    assert bad.status_code == 415, bad.get_json()
    assert bad.get_json()["error"]["code"].startswith("UPLOAD_")
    # an extension the policy never allows is refused before it is sniffed
    svg = alice.upload("/api/posts", {"file": ("icon.svg", b"<svg onload=alert(1)>")}, content="x")
    assert svg.status_code == 415, svg.get_json()
    assert svg.get_json()["error"]["code"] == "DENIED_EXTENSION"
    assert alice.get("/api/posts").get_json()["posts"] == []


def test_oversize_upload_is_refused_and_leaves_no_file(tiny_client, tmp_path):
    app = tiny_client.application
    with app.app_context():
        alice = register(tiny_client, "alice")
    big = PNG + b"\x00" * (1024 * 1024 + 800 * 1024)      # 1.8 MB against a 1 MB cap
    resp = alice.upload("/api/posts", {"file": ("big.png", big)}, content="heavy")
    assert resp.status_code == 413, resp.get_json()
    body = resp.get_json()
    assert body["error"]["code"] in {"FILE_TOO_LARGE", "UPLOAD_FILE_TOO_LARGE"}, body
    posts_root = tmp_path / "uploads" / "posts"
    assert not posts_root.exists() or list(posts_root.iterdir()) == [], \
        "a rejected upload must not stay on disk"


def test_file_serving_refuses_to_walk_outside_the_category(client, users):
    alice = users["alice"]
    for probe in ("/files/posts/..%2F..%2Ftest.sqlite", "/files/posts/../../etc/passwd",
                  "/files/avatars/nope.png"):
        resp = alice.get(probe)
        assert resp.status_code in (400, 404), (probe, resp.status_code)
    # only known categories are served at all
    assert alice.get("/files/secrets/boot.txt").status_code in (400, 404)


def test_orphaned_media_is_swept_after_the_row_is_gone(app, client, users):
    alice = users["alice"]
    created = alice.upload("/api/stories", {"file": ("gone.png", PNG)}, caption="brief")
    sid = created.get_json()["story"]["id"]
    name = created.get_json()["story"]["file_path"]
    root = app.extensions["volexturn_storage"].root
    path = root / "stories" / name
    assert path.exists()
    alice.delete(f"/api/stories/{sid}")
    from backend.janitor import run_once_for_tests
    run_once_for_tests(app, app.extensions["volexturn_db"], app.extensions["volexturn_storage"])
    # stories are deleted with their row, so this is immediate
    assert not path.exists(), "deleting a story must not leave the bytes behind"


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------
def test_admin_panel_is_admin_only(client, users):
    alice = users["alice"]
    for path in ("/api/admin/dashboard", "/api/admin/users", "/api/admin/posts",
                 "/api/admin/servers", "/api/admin/audit", "/api/admin/health"):
        resp = alice.get(path)
        assert resp.status_code == 403, (path, resp.get_json())
        assert resp.get_json()["error"]["code"] in {"ADMIN_REQUIRED", "FORBIDDEN"}
    assert alice.post("/api/admin/users/1/ban", json={}).status_code == 403


def test_admin_dashboard_and_user_search(client, users, admin):
    dash = admin.get("/api/admin/dashboard").get_json()
    assert dash["success"] and "stats" in dash
    assert dash["stats"]["users"] >= 3
    users["alice"].post("/api/posts", json={"content": "one"})
    listed = admin.get("/api/admin/users?q=ali").get_json()
    assert [u["username"] for u in listed["users"]] == ["alice"]
    # the admin view never carries a password hash, even a hashed one
    assert all("password" not in u for u in listed["users"]), listed["users"][0].keys()
    detail = admin.get(f"/api/admin/users/{users['alice'].id}").get_json()["user"]
    assert detail["username"] == "alice" and "role" in detail
    legacy = admin.get("/api/admin/stats").get_json()
    assert legacy["success"] and "stats" in legacy


def test_ban_revokes_sessions_and_unban_restores_access(app, client, users, admin):
    bob = users["bob"]
    banned = admin.post(f"/api/admin/users/{bob.id}/ban", json={"reason": "spam" * 40}).get_json()
    assert banned["banned"] is True
    # an existing token stops working immediately, not at its expiry
    assert bob.get("/api/auth/me").status_code in (401, 403)
    again = bob.post("/login", data={"username": "bob", "password": bob.password})
    assert again.status_code in (401, 403)
    body = again.get_json()
    assert "مسدود" in body["message"], body
    assert body["error"]["code"] == "ACCOUNT_BANNED"
    with client.application.app_context():
        from backend.db import Database
        db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
        conn = db.connect()
        try:
            row = db.query_one(conn, "SELECT is_banned, ban_reason FROM users WHERE id = ?", (bob.id,))
        finally:
            db.close(conn)
    assert row["is_banned"] == 1 and row["ban_reason"].startswith("spam")

    admin.post(f"/api/admin/users/{bob.id}/unban", json={})
    assert bob.post("/login", data={"username": "bob", "password": bob.password}).status_code == 200


def test_admin_password_reset_forces_a_change_and_kills_old_sessions(app, client, users, admin):
    alice = users["alice"]
    res = admin.post(f"/api/admin/users/{alice.id}/password", json={}).get_json()
    assert res["must_change"] is True and res["temporary_password"]
    assert alice.get("/api/auth/me").status_code in (401, 403), "old token must die with the password"
    logged = alice.client.post("/login", data={"username": "alice", "password": res["temporary_password"]},
                              headers={"Origin": "http://localhost"})
    assert logged.status_code == 200, logged.get_json()
    # the flag is on the login envelope, which is what the SPA branches on
    assert logged.get_json()["must_change_password"] is True
    # and the strength rule still applies to the change it must make
    token = logged.get_json()["token"]
    weak = alice.client.post("/api/auth/change_password", json={"old_password": res["temporary_password"],
                                                                "new_password": "abc"},
                            headers={"Authorization": f"Bearer {token}", "Origin": "http://localhost"})
    assert weak.status_code == 422, weak.get_json()
    assert weak.get_json()["error"]["code"] == "WEAK_PASSWORD"


def test_admins_cannot_lock_themselves_out(client, users, admin):
    # self-demotion and self-ban are both refused: with one admin left, either
    # one would end the ability to moderate the instance
    assert admin.post(f"/api/admin/users/{admin.id}/admin", json={"value": False}).status_code == 403
    assert admin.post(f"/api/admin/users/{admin.id}/ban", json={}).status_code == 403
    demote = admin.post(f"/api/admin/users/{users['bob'].id}/admin", json={"value": False}).get_json()
    assert demote["role"] == "user"
    promote = admin.post(f"/api/admin/users/{users['bob'].id}/admin", json={"value": True}).get_json()
    assert promote["is_admin"] is True
    # and the promotion is visible to the client through the normal session route
    whoami = users["bob"].get("/api/auth/me").get_json()
    assert whoami["is_admin"] is True and whoami["user"]["role"] == "admin"


def test_delete_user_cascades_only_when_told_to(app, client, users, admin):
    alice, bob = users["alice"], users["bob"]
    pid = alice.post("/api/posts", json={"content": "keep or kill"}).get_json()["post"]["id"]
    # a real account deletion needs an explicit confirmation, not just a DELETE
    refused = admin.delete(f"/api/admin/users/{alice.id}")
    assert refused.status_code == 400
    assert refused.get_json()["error"]["code"] == "CONFIRM_REQUIRED"
    assert alice.get("/api/auth/me").status_code == 200, "nothing happened yet"
    dry = admin.delete(f"/api/admin/users/{alice.id}", json={"confirm": "delete"}).get_json()
    assert dry["deleted"] is True
    assert isinstance(dry["cascaded"], list)
    from backend.db import Database
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        gone = db.query_one(conn, "SELECT id, username FROM users WHERE id = ?", (alice.id,))
        post = db.query_one(conn, "SELECT content FROM posts WHERE id = ?", (pid,))
    finally:
        db.close(conn)
    assert gone is None, "the account itself is always removed"
    # without `cascade_posts` the content is orphaned-but-kept, which is the
    # difference between moderation and data loss
    assert post is not None and post["content"] == "keep or kill"
    assert bob.get("/api/auth/me").status_code == 200


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------
def test_reasons_are_public_and_describe_the_flow(client):
    body = client.get("/api/reports/reasons").get_json()
    assert body["targets"] and body["reasons"]
    assert body["status_flow"] == ["open", "reviewing", "actioned", "dismissed"]
    assert not hasattr(client, "token")       # no auth needed for this one


def test_report_flow_open_claim_handle(client, users, admin):
    alice, bob = users["alice"], users["bob"]
    reason = _reason(client)
    made = alice.post("/api/reports", json={"target_type": "user", "target_id": bob.id,
                                           "reason": reason, "details": "harassment in chat"})
    assert made.status_code == 201, made.get_json()
    rid = made.get_json()["report_id"]
    assert made.get_json()["reports_on_target"] == 1

    dupes = alice.post("/api/reports", json={"target_type": "user", "target_id": bob.id,
                                            "reason": reason, "details": "again"})
    assert dupes.status_code == 409
    assert dupes.get_json()["error"]["code"] == "ALREADY_REPORTED"

    mine = alice.get("/api/reports/mine").get_json()["reports"]
    assert [r["id"] for r in mine] == [rid]
    assert mine[0]["status"] == "open"

    queue = admin.get("/api/reports/queue").get_json()
    assert queue["counts"]["open"] == 1
    assert queue["reports"][0]["reporter_username"] == "alice"

    assert admin.post(f"/api/reports/{rid}/claim", json={}).get_json()["status"] == "reviewing"
    bad = admin.post(f"/api/reports/{rid}/handle", json={"action": "shoot"})
    assert bad.status_code == 400 and bad.get_json()["error"]["code"] == "BAD_ACTION"
    handled = admin.post(f"/api/reports/{rid}/handle",
                         json={"action": "ban", "note": "harassment confirmed"}).get_json()
    assert handled["action"] == "ban" and handled["result"] == {"action": "ban"}
    assert admin.get(f"/api/reports/{rid}").get_json()["report"]["status"] == "actioned"
    # the action is not cosmetic: banning through a report bans the account and
    # kills its sessions, which is the behaviour the moderators expect
    assert bob.get("/api/auth/me").status_code in (401, 403)
    assert admin.post(f"/api/reports/{rid}/handle", json={"action": "unban"}).status_code in (200, 409)


def test_reports_need_an_existing_target_and_never_yourself(client, users, admin):
    alice, bob = users["alice"], users["bob"]
    reason = _reason(client)
    ghost = alice.post("/api/reports", json={"target_type": "user", "target_id": 9999,
                                            "reason": reason})
    assert ghost.status_code == 404 and ghost.get_json()["error"]["code"] == "TARGET_NOT_FOUND"
    self = alice.post("/api/reports", json={"target_type": "user", "target_id": alice.id,
                                           "reason": reason})
    assert self.status_code == 400 and self.get_json()["error"]["code"] == "SELF_REPORT"
    bogus = alice.post("/api/reports", json={"target_type": "spaceship", "target_id": 1,
                                            "reason": reason})
    assert bogus.status_code == 400
    assert bogus.get_json()["error"]["code"] == "BAD_TARGET_TYPE"
    ctx = admin.get(f"/api/reports/target?type=user&id={bob.id}").get_json()
    assert ctx["exists"] is True and ctx["can_report"] is True
    mine = admin.get(f"/api/reports/target?type=user&id={admin.id}").get_json()
    assert mine["can_report"] is False


def test_escalation_counts_other_peers_not_yourself(client, users):
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    reason = _reason(client)
    first = alice.post("/api/reports", json={"target_type": "user", "target_id": bob.id,
                                            "reason": reason}).get_json()
    second = carol.post("/api/reports", json={"target_type": "user", "target_id": bob.id,
                                             "reason": reason}).get_json()
    assert first["escalated"] is False and second["reports_on_target"] == 2
    assert second["escalated"] is False, "two is not yet an escalation"
    dave = register(client, "dave")
    third = dave.post("/api/reports", json={"target_type": "user", "target_id": bob.id,
                                           "reason": reason}).get_json()
    assert third["reports_on_target"] == 3
    assert third["escalated"] is True, "three independent reports raise the priority"


# --------------------------------------------------------------------------
# realtime
# --------------------------------------------------------------------------
def _registered(app) -> set[str]:
    from flask_socketio import SocketIO
    from backend.realtime import register_realtime
    si = SocketIO()
    si.init_app(app)
    register_realtime(si, app)
    return set(si.server.handlers.get("/", {}))


def test_every_socket_event_the_spa_emits_is_registered(app):
    """
    The frontend file is untouched, so every name it emits has to have a handler.
    Reading the SPA here is the point: a rename on either side silently kills a
    feature, and no python test would notice.
    """
    import re
    from pathlib import Path
    html = Path(__file__).resolve().parents[1] / "index.html"
    emitted = set(re.findall(r"socket\.emit\(\s*'([a-z_]+)'", html.read_text(encoding="utf-8")))
    assert emitted, "the SPA emits nothing? the scanner is wrong"
    missing = sorted(n for n in emitted if n not in _registered(app))
    assert not missing, f"no handler registered for: {missing}"


def test_events_the_spa_listens_for_still_exist_server_side(app):
    """The other half of the contract: names the browser waits for must be emitted."""
    import re
    from pathlib import Path
    html = Path(__file__).resolve().parents[1] / "index.html"
    listened = set(re.findall(r"socket\.on\(\s*'([a-z_]+)'", html.read_text(encoding="utf-8")))
    transport = {"connect", "disconnect", "connect_error", "reconnect", "error"}
    sources = ""
    root = Path(__file__).resolve().parents[1] / "backend"
    for py in root.rglob("*.py"):
        sources += py.read_text(encoding="utf-8")
    orphan = sorted(n for n in (listened - transport) if f'"{n}"' not in sources)
    assert not orphan, f"the SPA listens for events nothing emits: {orphan}"


def test_broadcasts_go_to_rooms_not_to_everyone(app):
    """
    The old code emitted to no room at all, so every browser saw every message.
    Room addressing is the fix, and it is only observable through the room names,
    so that is exactly what this asserts.
    """
    class Capture:
        def __init__(self):
            self.calls: list[tuple[str, str | None]] = []

        def emit(self, event, data=None, **kw):
            self.calls.append((event, kw.get("room")))

    from backend.notify import emit_group, emit_pair, emit_to_room_participants, user_room
    cap = Capture()
    emit_pair(cap, 3, 4, "new_message", {"id": 1})
    emit_group(cap, 9, "group_message", {"id": 2})
    emit_to_room_participants(cap, 11, "room_message", {"id": 3})
    assert cap.calls == [("new_message", user_room(3)), ("new_message", user_room(4)),
                         ("group_message", "group:9"), ("room_message", "game_room:11")]
    # a missing room is the broadcast bug this guards against
    assert all(room is not None for _, room in cap.calls)


def test_room_and_pair_broadcast_helpers_are_exported():
    """The API modules import these from one place; if they move, emits vanish."""
    from backend import notify
    for name in ("emit_pair", "emit_group", "emit_to_room_participants", "notify"):
        assert callable(getattr(notify, name, None)), name
