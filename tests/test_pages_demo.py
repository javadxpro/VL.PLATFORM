"""
The GitHub Pages demo (docs/PAGES.md): a build of the real UI plus `pages/vl-demo.js`.

Nothing here tests the demo's business logic — `pages/demo-selftest.js` does that,
driving the patched `fetch` for real. What these tests guard is the thing that
rots quietly in a setup like this: the *agreement* between three files that must
move together (the UI's call list, the server's routes, the demo's routes), the
build's assumptions, and the promise that a public demo contains no credentials.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
DEMO = (ROOT / "pages" / "vl-demo.js").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT))
from tools import build_pages  # noqa: E402


def _ui_paths() -> set[str]:
    """Every path the UI asks for, as a literal with template holes filled in."""
    found = set()
    patterns = (
        r"apiFetch\(\s*[`'\"]([^`'\"]+)",
        r"fetch\(\s*[`'\"](/api[^`'\"]*|/[a-z_][^`'\"]*)",
        r"`(/(?:messages|group_messages|history|user_profile|post_comments|notifications|"
        r"unread_counts|seen_messages|story_views|group_info|view_story|view_post|like_post|"
        r"pin_message|delete_message|group_add_member|group_remove_member|group_delete|"
        r"delete_lan_host|comment_post|follow)/[^`]*)`",
    )
    for m in re.finditer(r"\?\s*'(/[a-z_/]+)'\s*:\s*'(/[a-z_/]+)'", HTML):
        # the auth form picks its URL from a ternary (`? '/login' : '/register'`), so
        # no fetch( call site contains either literal; without this the guard below
        # would silently stop covering login
        found.update(m.groups())
    for pat in patterns:
        for m in re.finditer(pat, HTML):
            p = m.group(1)
            if p.startswith("//") or "." in p.split("/")[1 if p.startswith("/api") else 1]:
                continue
            found.add(p)
    out = set()
    for p in found:
        p = re.sub(r"\?.*$", "", p)                       # query string is not a route
        p = re.sub(r"\$\{[^}]*\}", "1", p)                 # template holes → a sample id
        # '…/' + id is how the UI builds ids; a path with no trailing slash is complete
        # already, so filling one in here would invent routes the app never calls.
        while p.endswith("/"):
            p += "1"
        out.add(re.sub(r"//+", "/", p))
    return {p for p in out if p.startswith("/")}


def _demo_routes() -> set[tuple[str, re.Pattern]]:
    out = set()
    for m in re.finditer(r"on\('(GET|POST|PUT|PATCH|DELETE)',\s*'([^']+)'", DEMO):
        method, pattern = m.group(1), m.group(2)
        out.add((method, re.compile("^" + re.sub(r":\w+", r"[^/]+", pattern) + "$")))
    return out


# ----------------------------------------------------------------- UI ↔ server
def test_the_ui_only_calls_routes_the_server_actually_has():
    """
    The UI was written against the old single-file server. The compat layer is what
    keeps it working, and this test is what keeps *that* honest: if a legacy alias is
    dropped or renamed, the frontend breaks in ways no backend test would notice.
    """
    from backend.app import create_app

    app = create_app()
    matcher = app.url_map.bind("/")
    unresolved = []
    for path in sorted(_ui_paths()):
        hit = False
        for method in ("GET", "POST", "PUT", "DELETE"):
            try:
                matcher.match(path, method=method)
                hit = True
                break
            except Exception:
                continue
        if not hit:
            unresolved.append(path)
    assert not unresolved, f"the UI calls paths the server does not have: {unresolved}"
    assert len(_ui_paths()) > 25, "the extractor stopped matching the UI — fix this test, not the UI"


def test_the_ui_paths_are_a_stable_surface():
    """A guard on the guard: someone rewriting index.html wholesale should notice here."""
    paths = _ui_paths()
    for must in ("/posts", "/users", "/login", "/create_post", "/api/app_info", "/send_message"):
        assert any(p == must or p.startswith(must) for p in paths), must


# ------------------------------------------------------------------ UI ↔ demo
def test_the_demo_covers_every_ui_path_or_declares_itself_unavailable():
    """
    A path the UI calls and the demo neither serves nor lists as unavailable means one
    of two bad things on Pages: a spinner that never ends, or a JSON body the UI
    misreads. `emptyArrayFor` is the second half of the contract, so it is parsed too.
    """
    handled = _demo_routes()
    declared = set(re.findall(r"'(/[a-z_/]+)'", DEMO[DEMO.index("emptyArrayFor"):DEMO.index("function fallback")]))
    unresolved, unavailable = [], []
    for path in sorted(_ui_paths()):
        if any(rx.match(path) and True for _m, rx in handled if rx.match(path)):
            continue
        method_agnostic = any(rx.match(path) for _m, rx in handled)
        if method_agnostic:
            continue
        if any(path.startswith(pref) for pref in declared):
            unavailable.append(path)
            continue
        unresolved.append(path)
    assert not unresolved, f"paths the demo neither serves nor declares: {unresolved}"
    assert len(unavailable) <= 12, (
        f"the demo now ignores {len(unavailable)} UI paths ({unavailable}); either serve them "
        "in pages/vl-demo.js or accept the shorter list in docs/PAGES.md")


# ------------------------------------------------------------- version triangle
def test_app_version_agrees_in_the_ui_the_demo_and_the_server():
    """
    The UI compares its own constant with `app_info.version` and warns when they differ.
    The demo answers that endpoint, so a stale constant makes the demo open with a
    scary 'cache mismatch' toast — the exact bug this repo already fixed once
    (the banner). All three sources are compared, because the third is the truth.
    """
    from backend.config import Config

    html = re.search(r"const APP_VERSION = '([^']+)'", HTML)
    demo = re.search(r"var API_VERSION = '([^']+)'", DEMO)
    assert html and demo, "the constants moved — this test must move with them"
    assert html.group(1) == demo.group(1) == Config.app_version, (
        f"index.html={html.group(1)} vl-demo.js={demo.group(1)} config={Config.app_version}")


# ------------------------------------------------------------------------ build
def test_the_build_output_is_self_contained(tmp_path):
    stats = build_pages.build(tmp_path / "site", fonts=True, quiet=True)
    site = tmp_path / "site"
    built = (site / "index.html").read_text(encoding="utf-8")

    assert 'src="/static/' not in built and "url('/static/" not in built, "absolute asset path survived"
    assert "socket.io" not in built, "the demo must not ship a websocket client with no server"
    assert 'src="vl-demo.js"' in built
    assert (site / ".nojekyll").exists(), "Jekyll would eat files starting with an underscore"
    assert (site / "404.html").read_text(encoding="utf-8") == built
    assert stats["fonts"], "the Persian font must be bundled — a CDN would leak the visit"
    assert all((site / "static" / "fonts" / f).exists() for f in stats["fonts"])

    # the UI itself is not rewritten: only the head, so behaviour cannot drift
    body_start = HTML.index("<body>")
    assert built[built.index("<body>"):body_start + len(HTML) - body_start][:200] == HTML[body_start:body_start + 200]
    assert len(built) < len(HTML) + 200, "the build injected more than the script tag"


def test_the_build_fails_loudly_when_the_ui_changes_shape(tmp_path, monkeypatch):
    """The build matches two literal strings; if they move, it must say so, not
    publish a page that silently loads the websocket client and talks to nowhere."""
    monkeypatch.setattr(build_pages, "SOCKETIO_TAG", "<script src='/nope.js'></script>")
    with pytest.raises(build_pages.BuildError):
        build_pages.build(tmp_path / "site", fonts=False, quiet=True)


# ---------------------------------------------------------------------- safety
@pytest.mark.parametrize("needle", ["admin123", "AJXP", "SECRET_KEY", "BEGIN PRIVATE KEY",
                                   "password: \"", "password':'"])
def test_no_credentials_or_secrets_in_the_public_bundle(needle):
    """Pages is a public mirror. The demo must not carry a credential, a default
    password, or anything that reads like a leaked key — not even in a comment."""
    for blob in (DEMO, HTML, (ROOT / "pages" / "demo-selftest.js").read_text(encoding="utf-8")):
        assert needle not in blob, f"{needle!r} found in the shipped JavaScript"


def test_the_demo_stores_no_password_at_all():
    """Login in the demo accepts any 8-char string and never writes one: check that
    the seed object has no password field to begin with."""
    seed = DEMO[DEMO.index("function seed()"):DEMO.index("/* ───────── store")]
    # `is_password_protected` is a server field for a protected game server; a
    # password *value* is what must never appear.
    assert not re.search(r"password[sS]?\s*[:=]", seed), "the demo must not hold passwords, even dummy ones"


def test_seeded_vl_ids_are_valid_and_unique():
    """The P1 invariant, visible in the demo: format, no ambiguous glyphs, unique."""
    from backend import vlid

    ids = re.findall(r"'(VL-[0-9A-Z]{4}-[0-9A-Z]{4})'", DEMO)
    assert len(ids) >= 3, ids
    for value in ids:
        assert vlid.FORMAT_RE.match(value), value
        assert vlid.normalize(value) == value
        for ambiguous in "ILOU":
            assert ambiguous not in value.split("-", 1)[1], value
    assert len(set(ids)) == len(ids)


# ----------------------------------------------------------------- node harness
def test_the_demo_self_test_passes():
    """`pages/demo-selftest.js` drives the patched fetch end to end. Node ships with
    the CI runner; locally this skips when it is absent, so nothing is required."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    proc = subprocess.run([node, str(ROOT / "pages" / "demo-selftest.js")],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-4000:] + "\n" + proc.stderr[-2000:]
    assert "0 FAILED" not in proc.stdout
    tail = proc.stdout.strip().splitlines()[-1]
    assert re.search(r"\d+/\d+ demo checks passed", tail), tail


def test_this_feature_added_no_route_to_the_api_surface():
    """
    The Pages demo is a build artifact, not a backend feature: `docs/ROADMAP.md` §0
    rule 2 says a new endpoint only arrives with a capability flag and a docs
    refresh. So the check is that nothing appeared: API.md regenerates identical,
    with the generator's own default environment (pytest's VOLEXTURN_* fixtures
    would otherwise describe a different install).
    """
    gen = ROOT / "tools" / "gen_api_docs.py"
    proc = subprocess.run([sys.executable, str(gen), "--check"], capture_output=True, text=True,
                          timeout=300, cwd=str(ROOT),
                          env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
                               "VOLEXTURN_LOG_LEVEL": "CRITICAL"})
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    assert "stale" not in proc.stdout
