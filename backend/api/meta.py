"""
Meta endpoints: capabilities, health, public app info, route introspection.
"""

from __future__ import annotations

from flask import jsonify, request

from . import Module, all_routes, current_db
from ..config import get_config
from ..log import get_logger
from ..security import classify_target

mod = Module("meta")
log = get_logger("api.meta")


@mod.route("/app_info", auth="none", rate=None, legacy="/api/app_info")
def app_info():
    """Public capability banner. Extended without changing any existing key."""
    cfg = get_config()
    return jsonify({
        "name": cfg.app_name,
        "version": cfg.app_version,
        "mode": "publish" if not cfg.dev_mode else "dev",
        # --- new, additive ---
        "api_version": 2,
        "capabilities": {
            "gaming": True, "game_rooms": True, "discovery": cfg.discovery_enabled,
            "friends": True, "search": True, "reports": True, "voice": True,
            "file_search": True, "infinite_feed": True, "websocket": True,
        },
        "limits": {
            "image_mb": cfg.max_image_mb, "video_mb": cfg.max_video_mb,
            "file_mb": cfg.max_file_mb, "avatar_mb": cfg.max_avatar_mb,
            "page_size": cfg.default_page_size, "max_page_size": cfg.max_page_size,
        },
        "session": {"ttl_days": cfg.session_ttl_days, "max_per_user": cfg.session_max_per_user},
        "db_engine": current_db().engine,
        "server_time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@mod.route("/health", auth="none", rate=None)
def health():
    """Cheap readiness probe: DB round-trip + janitor + socket counts."""
    from ..janitor import janitor_status
    from .. import presence
    db = current_db()
    db_ok = True
    detail = {}
    conn = db.connect()
    try:
        db.query_one(conn, "SELECT 1 AS ok")
        from ..migrations import pending_count
        detail["migration_pending"] = pending_count(db)
        detail["schema_version"] = db.scalar(
            conn, "SELECT COALESCE(MAX(version),0) AS v FROM vx_schema_version")
    except Exception as exc:
        db_ok = False
        detail["db_error"] = str(exc)[:200]
    finally:
        db.close(conn)
    body = {
        "success": db_ok,
        "status": "ok" if db_ok else "degraded",
        "engine": db.engine,
        "presence": presence.snapshot(),
        "janitor": janitor_status(),
        "version": get_config().app_version,
        "detail": detail,
    }
    return jsonify(body), (200 if db_ok else 503)


@mod.route("", methods=["GET"], auth="none", rate=None)   # url == "/api" exactly
def api_index():
    """
    Surface summary for humans at a browser.

    The legacy build served `api_terminal.html` here in DEV_MODE only and 404'd
    otherwise; that file never shipped in the archive, so dev mode now returns
    the same information as data. Publish keeps the 404 — a route index on an
    anonymous endpoint is free reconnaissance otherwise.
    """
    cfg = get_config()
    if not cfg.dev_mode:
        return jsonify({"success": False, "message": "در نسخه publish غیرفعال است"}), 404
    from flask import current_app
    items = all_routes(current_app)
    groups: dict[str, int] = {}
    for it in items:
        head = "/" + str(it["rule"]).strip("/").split("/")[0]
        groups[head] = groups.get(head, 0) + 1
    return jsonify({"success": True, "app": cfg.app_name, "version": cfg.app_version,
                     "routes": len(items), "by_prefix": dict(sorted(groups.items())),
                     "list": "/api/routes (admin)", "docs": "docs/api.md"})


@mod.route("/routes", auth="admin", rate=None)
def routes():
    """Introspection for admins/docs tooling."""
    from flask import current_app
    items = all_routes(current_app)
    return jsonify({"success": True, "count": len(items), "routes": items})


@mod.route("/config", auth="none", rate=None)
def public_config():
    """Only what the SPA legitimately needs. Never secrets, never the allowlist."""
    cfg = get_config()
    return jsonify({
        "success": True,
        "max_image_mb": cfg.max_image_mb, "max_video_mb": cfg.max_video_mb,
        "max_file_mb": cfg.max_file_mb, "max_avatar_mb": cfg.max_avatar_mb,
        "min_password_length": cfg.min_password_length,
        "page_size": cfg.default_page_size,
        "discovery_enabled": cfg.discovery_enabled,
        "dev_mode": cfg.dev_mode,
        "version": cfg.app_version,
    })


@mod.route("/whoami", auth="optional", rate=None)
def whoami():
    from . import me
    try:
        u = me()
    except Exception:
        return jsonify({"success": True, "authenticated": False})
    from ..auth import as_user_dict
    from .. import presence
    data = as_user_dict(u)
    data["presence"] = presence.status_for(int(u["id"]))
    return jsonify({"success": True, "authenticated": True, "user": data,
                    "session": {"device": u.get("_device")}})


@mod.route("/discovery/selftest", methods=["POST"], auth="admin",
           rate=(10, 60))
def discovery_selftest():
    """
    Lets an admin verify the *policy* before trusting statuses.

    Never probes: it reports whether a given target would be allowed, and why
    not. Actual probing stays inside the janitor tick so one admin click can
    not become a port scanner.
    """
    data = request.get_json(silent=True) or {}
    host = str(data.get("ip_address") or "").strip()
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    allowed, reason = classify_target(host, port)
    from ..discovery import health_check
    return jsonify({"success": True, "would_probe": allowed, "reason": reason,
                    "policy": health_check()})
