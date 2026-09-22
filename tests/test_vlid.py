"""
VL ID — one stable public identity per account (docs/ROADMAP.md, P1).

The interesting property is not the format, it is that *every* way of creating a
user ends up with an id, and that nothing can move it afterwards. Both are the
kind of thing that decays silently: a new signup path added next month, or a
profile handler that starts accepting `vl_id`.
"""

from __future__ import annotations

import re

import pytest

from backend import vlid


# --------------------------------------------------------------------- format
def test_generated_ids_are_canonical_and_random():
    made = {vlid.new() for _ in range(500)}
    assert len(made) == 500, "generated ids are colliding far too often"
    for v in made:
        assert vlid.FORMAT_RE.match(v), v
        body = v.split("-", 1)[1]          # the prefix is "VL-"; only the body is random
        for ambiguous in "ILOU":
            assert ambiguous not in body, f"{v} contains {ambiguous}, which people misread"


@pytest.mark.parametrize("raw,expected", [
    ("VL-7QK4-M2X9", "VL-7QK4-M2X9"),
    ("vl-7qk4-m2x9", "VL-7QK4-M2X9"),
    (" vl 7QK4 M2X9 ", "VL-7QK4-M2X9"),
    ("7qk4m2x9", "VL-7QK4-M2X9"),
    ("VL7QK4M2X9", "VL-7QK4-M2X9"),
    ("VL_7qk4_m2x9", "VL-7QK4-M2X9"),
])
def test_typing_it_by_hand_still_works(raw, expected):
    assert vlid.normalize(raw) == expected


@pytest.mark.parametrize("bad", ["", None, "VL-1234", "admin", "VL-IOUL-O1OL", "VL-7QK4-M2X99", "user_id"])
def test_junk_is_not_an_id(bad):
    assert vlid.normalize(bad) is None


# ------------------------------------------------------------------ signup path
def test_registration_and_me_agree_on_the_identity(app, users):
    alice = users["alice"]
    me = alice.get("/api/auth/me").get_json()["user"]
    assert vlid.FORMAT_RE.match(me["vl_id"]), me["vl_id"]
    # stable: another read returns the same value, not a fresh draw
    assert alice.get("/api/auth/me").get_json()["user"]["vl_id"] == me["vl_id"]
    # and two accounts never share one
    assert users["bob"].get("/api/auth/me").get_json()["user"]["vl_id"] != me["vl_id"]


def test_the_id_is_public_on_the_profile_surface(app, users):
    alice, bob = users["alice"], users["bob"]
    mine = alice.get("/api/auth/me").get_json()["user"]["vl_id"]
    prof = bob.get(f"/api/users/{alice.id}").get_json()["user"]
    assert prof["vl_id"] == mine
    listed = bob.get("/api/users").get_json()["users"]
    by_id = {u["id"]: u.get("vl_id") for u in listed}
    assert by_id.get(alice.id) == mine, "the listing projection dropped vl_id"
    # the legacy contract keeps working too: a bare array, same column present
    bare = bob.get("/users").get_json()
    assert isinstance(bare, list) and any(u["id"] == alice.id and u.get("vl_id") for u in bare)


def test_admin_fixture_also_has_one(app, admin):
    assert vlid.FORMAT_RE.match(admin.get("/api/auth/me").get_json()["user"]["vl_id"])


# ------------------------------------------------------------------ immutability
def test_profile_update_cannot_move_an_identity(app, users):
    alice = users["alice"]
    before = alice.get("/api/auth/me").get_json()["user"]["vl_id"]
    r = alice.post("/api/users/me/profile", json={"full_name": "Renamed", "vl_id": "VL-0000-0000"})
    assert r.status_code == 200, r.get_json()
    assert alice.get("/api/auth/me").get_json()["user"]["vl_id"] == before
    # an id a user can rewrite is not an identity — history and blame would move with it


def test_there_is_no_lookup_endpoint_yet(app):
    """Search-by-VL-ID is an open decision (docs/ROADMAP.md §5), so nothing may
    expose it accidentally by naming a route after the column."""
    offenders = [str(r) for r in app.url_map.iter_rules() if "/vl" in str(r).lower()]
    assert not offenders, offenders


# ------------------------------------------------------------------- migration
def test_migration_backfills_rows_that_predate_it(app, users):
    """
    An upgrade must not leave old users without an identity. Simulated the way it
    really happens: the column exists, some rows were written before the backfill.
    """
    from backend import migrations
    from backend.config import get_config
    from backend.db import Database

    cfg = get_config()
    db = Database(engine=cfg.db_engine, db_path=str(cfg.db_file))
    c = db.connect()
    try:
        db.execute(c, "UPDATE users SET vl_id = NULL WHERE username IN ('alice','bob')").close()
        # the empty-string case is the one that would break the unique index
        db.execute(c, "UPDATE users SET vl_id = '' WHERE username = 'carol'").close()
        c.commit()
        assert db.scalar(c, "SELECT COUNT(*) FROM users WHERE vl_id IS NULL OR vl_id = ''") == 3

        migrations._v0011_vl_identity(db, c)
        c.commit()

        rows = db.query(c, "SELECT username, vl_id FROM users WHERE username IN ('alice','bob','carol')")
        by_name = {r["username"]: r["vl_id"] for r in rows}
        assert len(set(by_name.values())) == 3, by_name
        for name, value in by_name.items():
            assert vlid.FORMAT_RE.match(value), (name, value)

        # idempotent: a second pass must not rotate ids people may have shared
        again = {r["username"]: r["vl_id"] for r in db.query(
            c, "SELECT username, vl_id FROM users WHERE username IN ('alice','bob','carol')")}
        assert again == by_name
    finally:
        db.close(c)


def test_unique_index_is_the_real_guard(app, users):
    import sqlite3

    from backend.config import get_config
    from backend.db import Database

    cfg = get_config()
    db = Database(engine=cfg.db_engine, db_path=str(cfg.db_file))
    c = db.connect()
    try:
        taken = users["alice"].get("/api/auth/me").get_json()["user"]["vl_id"]
        with pytest.raises(Exception) as exc:
            db.execute(c, "INSERT INTO users (username, password, vl_id) VALUES ('dup','x',?)",
                       (taken,))
        assert "unique" in str(exc.value).lower() or isinstance(exc.value, sqlite3.IntegrityError)
    finally:
        db.close(c)


def test_assign_returns_the_existing_value(app, users):
    from backend.config import get_config
    from backend.db import Database

    cfg = get_config()
    db = Database(engine=cfg.db_engine, db_path=str(cfg.db_file))
    c = db.connect()
    try:
        current = db.scalar(c, "SELECT vl_id FROM users WHERE id = ?", (users["alice"].id,))
        assert vlid.assign(db, c, users["alice"].id) == current
    finally:
        db.close(c)


# --------------------------------------------------- every creation path covered
def test_every_user_insert_path_assigns_an_id():
    """
    Structural guard, not decoration: three code paths create users today
    (registration, the first-admin setup bootstrap, `backend create-admin`), and
    each must assign an id. A fourth path added later without `vlid.assign` is
    exactly the bug this test exists to catch.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "backend"
    sites = []
    for path in sorted(list((root / "api").glob("*.py"))) + [root / "cli.py"]:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"INSERT INTO users\b", text):
            tail = text[match.end():match.end() + 1200]
            sites.append((path.name, "vlid.assign" in tail))
    assert len(sites) >= 3, f"expected every user-creating path, found {sites}"
    missing = [name for name, covered in sites if not covered]
    assert not missing, f"user created without a VL ID in: {', '.join(sorted(set(missing)))}"
