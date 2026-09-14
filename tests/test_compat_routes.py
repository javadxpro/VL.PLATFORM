"""
Backward compatibility: the pre-upgrade HTTP surface must still exist.

The list below was extracted from the legacy `server.py` (`git show
9cdceb3:server.py`, 49 `@app.route` rules). If a rule disappears from the app,
every installed client that still calls it breaks — so this test fails loudly
instead of the SPA breaking silently.

The *shape* contract matters as much as the path: the old SPA does
`Array.isArray(res)` / `res.success` on several of these, so those endpoints
must keep returning bare arrays/maps rather than the new `{success, …}` envelope.
"""

from __future__ import annotations

import re

import pytest

#: (rule, methods) exactly as the legacy app declared them
LEGACY_ROUTES: list[tuple[str, list[str]]] = [
    ("/", ["GET"]),
    ("/api", ["GET"]),
    ("/api/admin/clear_all_messages", ["DELETE"]),
    ("/api/admin/delete_post/<int:post_id>", ["DELETE"]),
    ("/api/admin/delete_user/<int:user_id>", ["DELETE"]),
    ("/api/admin/demote_user/<int:user_id>", ["POST"]),
    ("/api/admin/promote_user/<int:user_id>", ["POST"]),
    ("/api/admin/stats", ["GET"]),
    ("/api/admin/users", ["GET"]),
    ("/api/app_info", ["GET"]),
    ("/change_password", ["POST"]),
    ("/comment_post/<int:post_id>", ["POST"]),
    ("/create_group", ["POST"]),
    ("/create_lan_host", ["POST"]),
    ("/create_post", ["POST"]),
    ("/create_story", ["POST"]),
    ("/delete_lan_host/<int:host_id>", ["DELETE"]),
    ("/delete_message/<int:mid>", ["DELETE"]),
    ("/edit_message", ["POST"]),
    ("/files/<category>/<filename>", ["GET"]),
    ("/follow/<int:target_id>", ["POST"]),
    ("/forward_message", ["POST"]),
    ("/group_add_member/<int:gid>", ["POST"]),
    ("/group_delete/<int:gid>", ["DELETE"]),
    ("/group_info/<int:gid>", ["GET"]),
    ("/group_messages/<int:gid>", ["GET"]),
    ("/group_remove_member/<int:gid>", ["POST"]),
    ("/history/<int:me>", ["GET"]),
    ("/lan_hosts", ["GET"]),
    ("/like_post/<int:post_id>", ["POST"]),
    ("/login", ["POST"]),
    ("/messages/<int:u1>/<int:u2>", ["GET"]),
    ("/my_groups", ["GET"]),
    ("/notifications/<int:me>", ["GET"]),
    ("/notifications_read/<int:me>", ["POST"]),
    ("/pin_message/<int:mid>", ["POST"]),
    ("/post_comments/<int:post_id>", ["GET"]),
    ("/posts", ["GET"]),
    ("/register", ["POST"]),
    ("/seen_messages/<int:partner>", ["POST"]),
    ("/send_message", ["POST"]),
    ("/stories", ["GET"]),
    ("/story_views/<int:story_id>", ["GET"]),
    ("/unread_counts/<int:me>", ["GET"]),
    ("/update_profile", ["POST"]),
    ("/user_profile/<int:me>/<int:target>", ["GET"]),
    ("/users", ["GET"]),
    ("/view_post/<int:post_id>", ["POST"]),
    ("/view_story/<int:story_id>", ["POST"]),
]

#: legacy socket events the SPA emits or listens for (server.py @socketio.on)
LEGACY_SOCKET_EVENTS = {"connect", "join", "disconnect", "typing",
                        "join_voice", "leave_voice", "voice_signal"}


def _normalize(rule: str) -> str:
    """`/users/<int:uid>` and `/users/<int:user_id>` are the same path shape."""
    return re.sub(r"<[^>]+>", "<*>", rule)


@pytest.fixture()
def url_map(app):
    return {(_normalize(str(r)), m) for r in app.url_map.iter_rules()
            for m in (r.methods or set()) if m not in {"HEAD", "OPTIONS"}}


@pytest.mark.parametrize("rule,methods", LEGACY_ROUTES)
def test_legacy_route_still_registered(url_map, rule, methods):
    for method in methods:
        assert (_normalize(rule), method) in url_map, f"legacy {method} {rule} disappeared"


def test_legacy_route_count_matches_the_audit(app):
    """A guard against someone 'fixing' this test by deleting entries."""
    assert len(LEGACY_ROUTES) == 49


def test_socket_events_are_all_registered(app):
    sio = app.extensions["volexturn_socketio"]
    handled = set((getattr(sio.server, "handlers", None) or {}).get("/") or {})
    missing = sorted(LEGACY_SOCKET_EVENTS - handled)
    assert not missing, f"socket events no longer handled: {missing}"
    # the additions the API relies on for scoped fan-out
    assert {"room_join", "room_leave", "room_message", "mute", "set_presence"} <= handled


def test_new_api_surface_is_mounted(app):
    rules = {_normalize(str(r)) for r in app.url_map.iter_rules()}
    for prefix in ("/api/auth", "/api/users", "/api/posts", "/api/stories", "/api/messages",
                   "/api/groups", "/api/games", "/api/servers", "/api/rooms",
                   "/api/notifications", "/api/search", "/api/reports", "/api/admin"):
        assert any(r.startswith(prefix) for r in rules), f"{prefix}/* is not mounted"


def test_unknown_route_is_json_not_html(client):
    """The SPA fetches JSON everywhere; a 404 page of HTML breaks its parsers."""
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert resp.is_json
    body = resp.get_json()
    assert body["success"] is False
    assert body.get("message")

def test_page_version_literal_matches_the_server(app, client):
    """
    `boot()` in index.html compares its own `APP_VERSION` literal against
    `/api/app_info` and toasts "clear your cache" when they differ. The literal
    lives in a file that ships from the server, so a stale *cache* is not what a
    mismatch means — a bumped `Config.app_version` with an un-bumped literal means
    every user gets a red "clear the cache" message that clearing the cache cannot
    fix. That is exactly what happened (page 3.0.0 vs server 4.0.0).
    """
    import re
    from backend.config import get_config

    html = client.get("/").get_data(as_text=True)
    page = re.search(r"const APP_VERSION = '([^']+)'", html)
    assert page, "the version literal the boot check reads is gone from index.html"
    assert page.group(1) == get_config().app_version, (
        f"index.html says {page.group(1)}, the server says {get_config().app_version}")

