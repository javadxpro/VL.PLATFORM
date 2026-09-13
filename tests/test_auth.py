"""
Auth: registration, login, sessions, password policy, the admin bootstrap.

These are the checks the legacy build either skipped or got wrong
(docs/AUDIT.md §7): a 4-char password, an `admin/admin123` seed, tokens that
never expired and no way to invalidate a stolen session.
"""

from __future__ import annotations


from backend.auth import issue_session
from backend.db import Database
from backend.security import check_password_strength, hash_password, verify_password


def test_register_login_me_roundtrip(client):
    from tests.conftest import register
    alice = register(client, "alice")
    assert alice.id > 0
    me = alice.get("/api/auth/me").get_json()
    assert me["success"] and me["user"]["username"] == "alice"
    assert "password" not in me["user"] and "token" not in me["user"]


def test_login_returns_the_same_legacy_shape(client):
    from tests.conftest import register
    register(client, "alice", password="Another-Str0ng-Pass!")
    resp = client.post("/login", json={"username": "alice", "password": "Another-Str0ng-Pass!"})
    body = resp.get_json()
    assert resp.status_code == 200
    # exactly the legacy keys the SPA reads (`res.user`, `res.token`), plus the
    # additive ones; no `message` on success because the old build had none.
    assert set(body) >= {"success", "user", "token", "version"}
    assert body["user"]["id"] and "password" not in body["user"]


def test_login_failure_message_is_top_level(client):
    """/login errors must carry `message`: the SPA shows it verbatim."""
    resp = client.post("/login", json={"username": "ghost", "password": "whatever-123"})
    assert resp.status_code in (401, 400)
    body = resp.get_json()
    assert body["success"] is False
    assert isinstance(body.get("message"), str) and body["message"]


def test_weak_password_is_rejected(client):
    for pw in ("abc", "password", "12345678", "aaaaaaaa"):
        resp = client.post("/register", json={"username": "u_" + pw[:4] + "_x", "password": pw})
        assert resp.status_code in (400, 422), f"{pw!r} was accepted"
        body = resp.get_json()
        assert body["success"] is False and body["error"]["code"] == "WEAK_PASSWORD"


def test_username_rules(client):
    """.  - and _ stay legal because the legacy app allowed them (existing accounts
    must remain registerable/recoverable), while spaces and markup do not."""
    short = client.post("/register", json={"username": "a", "password": "Str0ng-Pass!word"})
    assert short.status_code == 400 and short.get_json()["error"]["code"] == "FIELD_TOO_SHORT"
    bad = client.post("/register", json={"username": "bad name!", "password": "Str0ng-Pass!word"})
    assert bad.status_code in (400, 422) and bad.get_json()["error"]["code"] == "INVALID_USERNAME"
    for name in ("good_name-2", "a.b_c", "کاربر_۱"):
        resp = client.post("/register", json={"username": name, "password": "Str0ng-Pass!word"})
        assert resp.status_code in (200, 201), f"{name}: {resp.get_json()}"


def test_reserved_admin_names_are_taken(client):
    for name in ("admin", "root", "system"):
        resp = client.post("/register", json={"username": name, "password": "Str0ng-Pass!word"})
        assert resp.status_code == 409, f"{name} registered: {resp.get_json()}"


def test_duplicate_username_conflicts(client):
    from tests.conftest import register
    register(client, "alice")
    resp = client.post("/register", json={"username": "alice", "password": "Str0ng-Pass!word"})
    assert resp.status_code == 409


def test_password_hash_format_and_iterations(app):
    cfg = app.extensions["volexturn_config"]
    stored = hash_password("S3cret-Passphrase")
    assert "S3cret" not in stored
    prefix, iters, salt, digest = stored.split("$", 3)
    assert prefix == "pbkdf2" and int(iters) == cfg.pbkdf2_iterations
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert verify_password("S3cret-Passphrase", stored)
    assert not verify_password("wrong", stored)
    # same password twice must not produce the same row
    assert hash_password("S3cret-Passphrase") != stored


def test_weak_legacy_hashes_are_flagged_for_upgrade(app):
    """
    Migration path: rows below the current cost still verify, but login rehashes
    them. A plaintext row (the worst legacy case) must also verify exactly once
    and be flagged.
    """
    from backend.security import needs_rehash
    cheap = hash_password("Old-Password-1", iterations=10)
    assert verify_password("Old-Password-1", cheap)
    assert needs_rehash(cheap)
    assert not needs_rehash(hash_password("New-Password-1"))
    plaintext = "totally-plaintext"
    assert verify_password(plaintext, plaintext)
    assert needs_rehash(plaintext)
    assert not verify_password("nope", plaintext)


def test_strength_checker_flags_common_patterns():
    ok, why, tips = check_password_strength("Tr0trot3000!!")
    assert ok, why
    ok, why, _ = check_password_strength("password1")
    assert not ok and why


def test_unauthenticated_api_is_401_not_redirect(client):
    for url in ("/api/users", "/api/posts", "/api/auth/me", "/api/servers"):
        resp = client.get(url)
        assert resp.status_code == 401, url
        assert resp.is_json, url


def test_logout_revokes_the_token(client):
    from tests.conftest import register
    alice = register(client, "alice")
    assert alice.get("/api/auth/me").status_code == 200
    alice.post("/api/auth/logout", json={})
    again = alice.get("/api/auth/me")
    assert again.status_code == 401
    # the same token, replayed by an attacker, is also dead
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {alice.token}"}).status_code == 401


def test_change_password_kills_other_sessions(app, client):
    from tests.conftest import register
    alice = register(client, "alice")
    db = app.extensions["volexturn_db"]
    conn = db.connect()
    try:
        other = issue_session(db, conn, alice.id, device="stolen-laptop").token
    finally:
        db.close(conn)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {other}"}).status_code == 200
    resp = alice.post("/change_password", json={"old_password": "Str0ng-Pass!phrase",
                                                "new_password": "Ev3n-Str0nger-Pass!"})
    assert resp.status_code == 200, resp.get_json()
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {other}"}).status_code == 401
    assert client.post("/login", json={"username": "alice", "password": "Str0ng-Pass!phrase"}).status_code in (400, 401)
    assert client.post("/login", json={"username": "alice",
                                       "password": "Ev3n-Str0nger-Pass!"}).status_code == 200


def test_login_throttles_brute_force(app, client):
    from tests.conftest import register
    cfg = app.extensions["volexturn_config"]
    object.__setattr__(cfg, "rate_limit_enabled", True)   # this one test wants the limiter
    register(client, "victim")
    cfg = app.extensions["volexturn_config"]
    for i in range(cfg.login_max_attempts + 2):
        resp = client.post("/login", json={"username": "victim", "password": f"nope-{i}-guess"})
        if resp.status_code == 429:
            break
    else:
        raise AssertionError("login never rate-limited")
    body = resp.get_json()
    assert body["success"] is False
    assert isinstance(body.get("message"), str)
    object.__setattr__(cfg, "rate_limit_enabled", False)


def test_setup_refuses_once_an_admin_exists(app, client):
    """The bootstrap endpoint must not be a second front door (AUDIT §7)."""
    from tests.conftest import register
    register(client, "alice")
    db = Database(engine="sqlite", db_path=str(app.extensions["volexturn_config"].db_file))
    conn = db.connect()
    try:
        db.execute(conn, "UPDATE users SET role = 'admin' WHERE username = 'alice'").close()
        conn.commit()
    finally:
        db.close(conn)
    resp = client.post("/api/auth/setup", json={"username": "evil", "password": "Str0ng-Pass!word"})
    assert resp.status_code in (403, 409), resp.get_json()


def test_session_token_format_is_unguessable(app):
    from backend.security import new_session_token
    tokens = {new_session_token() for _ in range(50)}
    assert len(tokens) == 50
    lengths = {len(t) for t in tokens}
    assert max(lengths) >= 32


def test_cookie_and_header_both_authenticate(client):
    from tests.conftest import register
    alice = register(client, "alice")
    # the login response also set a cookie; a bare cookie request must work
    client2 = alice.client
    resp = client2.get("/api/auth/me")
    assert resp.status_code == 200


def test_expired_session_is_rejected(app, client):
    import datetime as dt
    from tests.conftest import register
    from backend.auth import authenticate
    alice = register(client, "alice")
    db = app.extensions["volexturn_db"]
    conn = db.connect()
    try:
        uid = db.scalar(conn, "SELECT user_id FROM sessions WHERE token = ?", (alice.token,))
        db.execute(conn, "UPDATE sessions SET expires_at = ? WHERE token = ?",
                   ((dt.datetime.utcnow() - dt.timedelta(days=1)).isoformat(sep=" "), alice.token)).close()
        conn.commit()
        assert authenticate(db, conn=conn, token=alice.token) is None
        # dead, and *marked* dead (revoked) instead of left looking alive
        row = db.query_one(conn, "SELECT revoked_at, revoked_reason FROM sessions WHERE token = ?",
                           (alice.token,))
        assert row is not None and row["revoked_at"]
        assert row["revoked_reason"] == "expired"
        assert int(uid) == alice.id
    finally:
        db.close(conn)


def test_csrf_rejects_cookie_post_without_origin(client, app):
    """
    A cross-site form post cannot set `Origin` in old browsers and cannot set an
    Authorization header — so a cookie-authenticated mutation with no Origin is
    refused, while the same request with a bearer token is fine.
    """
    from tests.conftest import register
    alice = register(client, "alice")
    bare = app.test_client()                      # no Origin/Referer at all
    # 1. cookie only (the dangerous shape): rejected before the view runs
    bare.set_cookie("vx_session", alice.token, domain="localhost")
    resp = bare.post("/api/users/me/profile", data={"bio": "pwned"},
                      content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 422, resp.get_json()
    assert resp.get_json()["error"]["code"] == "CSRF_ORIGIN_MISMATCH"
    # 2. same mutation with an Authorization header: allowed (unforgeable cross-site)
    resp = bare.post("/api/users/me/profile", json={"bio": "mine"},
                     headers={"Authorization": f"Bearer {alice.token}"})
    assert resp.status_code == 200, resp.get_json()
    assert client.get("/api/auth/me").get_json()["user"]["bio"] == "mine"
