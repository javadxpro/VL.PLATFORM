"""
The CLI is the install path, so it is tested like one.

`doctor` used to crash on exactly the two states an operator meets first: a
database with no schema (its admin check queried a missing table) and a
`--json` run (the flag was on the parent parser, so `backend doctor --json`
was rejected, and when accepted it printed human lines around the document).
Neither was reachable from the HTTP tests, which is why they lived on.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest


def run(argv: list[str]) -> tuple[int, str]:
    from backend.cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return int(code or 0), buf.getvalue()


@pytest.fixture(autouse=True)
def _config_already_set(app):
    """`get_config()` is process-wide, so a CLI call must see the test config."""
    assert app is not None
    return app


def test_doctor_passes_on_a_migrated_database(app):
    code, out = run(["doctor"])
    assert code == 0, out
    assert "Runtime" in out and "Realtime" in out
    assert "❌" not in out, out


def test_doctor_json_is_a_solo_document(app):
    code, out = run(["doctor", "--json"])
    assert code == 0, out
    # exactly one document on stdout: nothing human-readable may share the stream
    doc = json.loads(out)
    assert doc["ok"] is True and doc["problems"] == []
    assert doc["tables"] >= 40 and "rows" in doc


def test_doctor_survives_a_database_with_no_schema(app, tmp_path):
    from backend.config import get_config, set_config
    from dataclasses import replace
    cfg = get_config()
    fresh = replace(cfg, db_path=str(tmp_path / "unmanaged.sqlite"))
    set_config(fresh)
    try:
        code, out = run(["doctor"])
    finally:
        set_config(cfg)
    # missing tables are a *report*, not a traceback — this is the state where
    # someone reaches for doctor in the first place
    assert code == 1
    assert "مهاجرت" in out or "migrate" in out, out


def test_migrate_is_idempotent_and_reports_the_version(app, tmp_path):
    from backend.config import get_config, set_config
    from dataclasses import replace
    cfg = get_config()
    fresh = replace(cfg, db_path=str(tmp_path / "migrate.sqlite"))
    set_config(fresh)
    try:
        code, out = run(["migrate"])
        assert code == 0
        assert "0001:baseline_legacy_schema" in out and "version=10" in out
        code2, out2 = run(["migrate"])
        assert code2 == 0
        assert "⏭" in out2 and "applied" not in out2, out2
    finally:
        set_config(cfg)


def test_create_admin_then_token_and_passwd(app, client):
    code, out = run(["create-admin", "-u", "rootie", "-p", "Long-Admin-Pass!2026",
                     "-n", "Rootie"])
    assert code == 0, out
    assert "rootie" in out
    # the password never appears in the tool's own output
    assert "Long-Admin-Pass!2026" not in out
    # Re-running the command is safe in both senses of the word: with an
    # explicit -p it *resets* the password (an operator who means to take a
    # box back over), and without one it is a no-op that reports success, so a
    # deploy entrypoint can call it on every start.
    code, out = run(["create-admin", "-u", "rootie", "-p", "Whatever-12345!x"])
    assert code == 0 and "promoted" in out, out
    code, out = run(["create-admin", "-u", "rootie"])
    assert code == 0 and "already" in out, out
    assert client.post("/api/auth/login", json={"username": "rootie", "password": "Whatever-12345!x"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "rootie", "password": "Long-Admin-Pass!2026"}).status_code == 401

    # a bare token: `TOKEN=$(python -m backend token alice)` must paste cleanly
    code, out = run(["token", "rootie"])
    assert code == 0 and len(out.strip()) >= 32, out
    assert out.strip() == out.strip().split()[0], "stdout must hold nothing but the token"
    doc = json.loads(run(["token", "rootie", "--json"])[1])
    assert doc["user_id"] and doc["expires_at"] and doc["token"]
    before = out.strip()
    # and the token it printed is real
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {before}"})
    assert me.status_code == 200 and me.get_json()["user"]["role"] == "admin"

    code, out = run(["passwd", "rootie", "-p", "Rotated-Admin!2026"])
    assert code == 0, out
    # rotating the password kills the sessions that came before it
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {before}"}).status_code == 401
    again = run(["token", "rootie"])[1].strip()
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {again}"}).status_code == 200


def test_token_is_refused_in_production(app):
    from backend.config import get_config, set_config
    from dataclasses import replace
    cfg = get_config()
    set_config(replace(cfg, env="production", secret_key="x" * 48))
    try:
        code, out = run(["token", "rootie"])
        assert code == 2
        assert "پروداکشن" in out or "production" in out.lower(), out
    finally:
        set_config(cfg)


def test_routes_lists_both_surfaces(app):
    code, out = run(["routes"])
    assert code == 0
    assert "/api/admin/dashboard" in out
    assert "legacy" in out.lower() or "/login" in out
    code, out = run(["routes", "--filter", "stories"])
    assert code == 0 and "/api/stories" in out and "/api/posts" not in out


def test_janitor_and_sweep_report_counts(app):
    code, out = run(["janitor"])
    assert code == 0 and "tick" in out
    code, out = run(["sweep"])
    assert code == 0
    assert "dry_run" in out or "یتیم" in out, out


def test_help_without_arguments(app):
    code, out = run([])
    assert code == 0
    assert "doctor" in out and "create-admin" in out
