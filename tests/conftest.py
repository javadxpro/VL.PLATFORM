"""
Shared pytest fixtures.

Every test gets an isolated app: a fresh SQLite file in `tmp_path`, in-memory
storage, migrations applied, janitor off, rate limits wide open. `Config` is a
process-wide singleton, so the fixture installs its own copy and restores the
previous one afterwards — otherwise test 2 would inherit test 1's database.
"""

from __future__ import annotations

import base64
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import storage as storage_mod          # noqa: E402
from backend.config import Config, set_config        # noqa: E402
from backend.presence import reset as reset_presence  # noqa: E402
from backend.security import reset_limiters           # noqa: E402


def make_config(tmp_path: Path) -> Config:
    """Deterministic dev-free config: no dotenv, no inherited env vars."""
    return replace(
        Config(),
        env="test", dev_mode=False,
        secret_key="test-secret-key-0123456789abcdef-0123456789abcdef",
        auto_admin_password="",
        db_engine="sqlite", db_path=str(tmp_path / "test.sqlite"),
        upload_folder=str(tmp_path / "uploads"),
        log_level="ERROR", log_json=False,
        rate_capacity_default=100_000, rate_window_default=60, rate_max_keys=100_000,
        discovery_enabled=False, discovery_networks=(),
        janitor_interval_seconds=3_600,
        pbkdf2_iterations=1_000,        # real cost is tested in test_security
        min_password_length=8,
        default_page_size=20, max_page_size=100,
    )


@pytest.fixture()
def cfg(tmp_path):
    config = make_config(tmp_path)
    set_config(config)
    storage_mod.set_storage(None)                # fresh provider for this config
    try:
        yield config
    finally:
        set_config(Config())                      # back to a neutral snapshot
        storage_mod.set_storage(None)


@pytest.fixture()
def app(cfg):
    from backend.app import create_app
    application = create_app(config=cfg, start_janitor=False)
    reset_presence()
    reset_limiters()
    yield application
    storage_mod.set_storage(None)


@pytest.fixture()
def client(app):
    """
    Test client that behaves like the SPA's browser tab.

    Origin/Referer are set because the CSRF gate treats "unsafe method + session
    cookie + no Origin" as a cross-site form post and refuses it — which is
    correct for a browser (they always send Origin) but not something the test
    client does on its own. test_csrf_rejects_cookie_post_without_origin covers
    the refused case.
    """
    c = app.test_client()
    c.environ_base["HTTP_ORIGIN"] = "http://localhost"
    c.environ_base["HTTP_REFERER"] = "http://localhost/"
    return c


# --------------------------------------------------------------------------
# media fixtures
# --------------------------------------------------------------------------
#: a real 1x1 PNG — the upload pipeline sniffs magic bytes, so a fake file with
#: a `.png` name must be *rejected*, and these tests need bytes that pass.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "BQIAh6hvswQAAAAASUVORK5CYII=")

_PDF_HEAD = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


def png_bytes() -> bytes:
    return _PNG_1x1


def pdf_bytes() -> bytes:
    return _PDF_HEAD


# --------------------------------------------------------------------------
# users / sessions
# --------------------------------------------------------------------------
class Actor:
    """A logged-in test user with a client that keeps its session cookie."""

    def __init__(self, client, username: str, user_id: int, token: str, raw: dict):
        self.client = client
        self.username = username
        self.id = user_id
        self.token = token
        self.raw = raw

    # --- HTTP helpers -----------------------------------------------------
    def _hdr(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, url: str, **kw):
        return self.client.get(url, headers=kw.pop("headers", {}) or self._hdr(), **kw)

    def post(self, url: str, data=None, json=None, **kw):
        headers = kw.pop("headers", {}) or self._hdr()
        if json is not None:
            return self.client.post(url, json=json, headers=headers, **kw)
        return self.client.post(url, data=data if data is not None else {}, headers=headers, **kw)

    def put(self, url: str, json=None, **kw):
        return self.client.put(url, json=json or {}, headers=self._hdr(), **kw)

    def delete(self, url: str, **kw):
        return self.client.delete(url, headers=self._hdr(), **kw)

    def upload(self, url: str, files: dict[str, tuple[str, bytes]], data: dict | None = None,
                **extra):
        """multipart POST: `files` maps form-field -> (filename, bytes)."""
        from io import BytesIO
        body = {key: (BytesIO(blob), name) for key, (name, blob) in files.items()}
        body.update(data or {})
        body.update(extra)
        return self.client.post(url, data=body, content_type="multipart/form-data",
                                headers={"Authorization": f"Bearer {self.token}"})

    def post_image(self, url: str, field: str = "file", name: str = "shot.png", **extra):
        return self.upload(url, {field: (name, png_bytes())}, **extra)


def register(client, username: str, password: str = "Str0ng-Pass!phrase", **extra) -> Actor:
    """
    Create a user and hand back an authenticated actor.

    Two calls on purpose: `/register` has never returned a session (the SPA
    switches to the login form after it), so this mirrors what a real client does
    instead of leaning on a shortcut the endpoint does not offer.
    """
    resp = client.post("/register", json={"username": username, "password": password,
                                          "full_name": extra.pop("full_name", username.title()),
                                          **extra})
    assert resp.status_code in (200, 201), f"register {username}: {resp.status_code} {resp.get_json()}"
    assert resp.get_json().get("success"), resp.get_json()
    login = client.post("/login", json={"username": username, "password": password})
    body = login.get_json()
    assert login.status_code == 200 and body.get("success"), body
    return Actor(client, username, int(body["user"]["id"]), body["token"], body)


@pytest.fixture()
def users(client):
    """alice, bob and carol — the minimum to talk about graphs, blocks and roles."""
    return {u: register(client, u) for u in ("alice", "bob", "carol")}


@pytest.fixture()
def admin(app, client):
    """
    A real administrator, created through the same rules as production.

    Deliberately *not* the old `admin/admin123` seed: the fixture proves an admin
    can be created without it (docs/AUDIT.md §7).
    """
    from backend.db import Database
    from backend.security import hash_password
    cfg = app.extensions["volexturn_config"]
    db = Database(engine="sqlite", db_path=str(cfg.db_file))
    conn = db.connect()
    try:
        uid = db.insert(conn, """INSERT INTO users (username, password, full_name, bio, role, status)
                                 VALUES ('root', ?, 'Root Admin', 'administrator', 'admin', 'active')""",
                       (hash_password("Adm1n-Str0ng-Pass!"),))
        conn.commit()
    finally:
        db.close(conn)
    resp = client.post("/login", json={"username": "root", "password": "Adm1n-Str0ng-Pass!"})
    body = resp.get_json()
    assert body.get("success"), body
    return Actor(client, "root", int(uid), body["token"], body)
