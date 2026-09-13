"""
Application factory.

`create_app()` is the only place that touches globals. Note what is *not* here
anymore compared with the pre-upgrade build: no `init_db()` at import time
(defect P5 in docs/AUDIT.md). Schema work happens when the app object is
created, which makes `import server` side-effect free and safe for gunicorn's
prefork model.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from flask import Flask, g, request, send_from_directory
from flask_socketio import SocketIO

from .api import register_api
from .config import ROOT_DIR, Config, get_config, new_app_config
from .db import Database, set_db
from .errors import register_error_handlers
from .log import configure_logging, get_logger
from .migrations import run_migrations
from .storage import UnsafeKey, get_storage, validate_key

log = get_logger("app")

#: Attached to the Flask app so blueprints, auth and realtime share one socket.
socketio = SocketIO()

# --------------------------------------------------------------------------
# request plumbing
# --------------------------------------------------------------------------
def _close_conn_on_teardown(app: Flask) -> None:
    """
    One connection per request, finished at teardown.

    Views run their writes through `c.commit()` when a group of statements has
    to land together; this hook is the safety net that (a) never leaves a
    half-written transaction to the next request on the same pooled connection
    and (b) always closes the cursor-level connection. On a propagated
    exception we roll back instead — committing a partial write because the
    view blew up halfway would be worse than losing it.
    """

    @app.teardown_appcontext
    def _close(exc: BaseException | None = None) -> None:
        c = getattr(g, "_vx_conn", None)
        if c is None:
            return None
        try:
            if exc is not None:
                c.rollback()
            else:
                c.commit()
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
        g._vx_conn = None
        return None


def _before_after_request(app: Flask) -> None:
    sensitive = ("/login", "/register", "/change_password", "/api/auth")

    @app.before_request
    def _open():
        g.request_id = (request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12])[:32]
        g.started_at = time.perf_counter()
        # Identity is strictly per request. `g` sits on the app context, and an
        # app context can be reused across requests, so anything stale here
        # would be read as "who is calling" by a later view.
        g.user = None
        g.user_id = None
        g.session_id = None
        g._vx_identity = None
        g._vx_rotated_token = None
        # ...and so is the connection. Two requests sharing one connection would
        # share one transaction: a rollback on either side would then discard the
        # other's writes. The teardown hook closes whatever we left behind.
        g._vx_conn = None
        g._vx_endpoint = None
        # Rotated tokens are handed back through this header, not the body,
        # so a client can persist it without reparsing a payload.
        return None

    @app.after_request
    def _close(resp):
        rid = getattr(g, "request_id", "-")
        resp.headers["X-Request-Id"] = rid
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "geolocation=(), camera=(self), microphone=(self)"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = _csp()
        rotated = getattr(g, "_vx_rotated_token", None)
        if rotated:
            resp.headers["X-Volexturn-Token"] = rotated
        dur = (time.perf_counter() - getattr(g, "started_at", time.perf_counter())) * 1000
        # Access log: path + status + duration. Bodies are never logged, and
        # auth paths are dropped entirely (they carry credentials).
        if request.path.startswith(sensitive):
            log.debug("request", extra={"ctx": {"path": request.path, "status": resp.status_code,
                                               "ms": round(dur, 1), "redacted": True}})
        else:
            log.info("request", extra={"ctx": {
                "method": request.method, "path": request.path[:160],
                "status": resp.status_code, "ms": round(dur, 1),
                "user_id": int(g.user["id"]) if getattr(g, "user", None) else None,
            }})
        return resp

    @app.teardown_request
    def _exc(exc):
        if exc is not None and not isinstance(exc, (SafeSkip,)):
            log.debug("request_exception", extra={"ctx": {
                "path": request.path, "type": type(exc).__name__}})
        return None


class SafeSkip(Exception):
    """Werkzeug control-flow exceptions we intentionally do not log as errors."""


def _csp() -> str:
    """
    CSP kept as strict as the SPA allows.

    The single-file frontend uses inline `<script>`/`onclick` attributes, so
    `script-src` still needs 'unsafe-inline'; `style-src` too (theme colours
    are applied inline). Uploaded media stays same-origin, and `object-src
    'none'` + `base-uri 'self'` close the two that mattered most.
    See docs/security.md for the exact residual risk and the path to removing
    'unsafe-inline' (externalise the script, bind handlers in JS).
    """
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'"
    )


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------
def create_app(*, config: Config | None = None, skip_migrations: bool = False,
               testing: bool = False, start_janitor: bool | None = None) -> Flask:
    from .config import set_config
    cfg = config or get_config()
    if config is not None:
        set_config(cfg)
    configure_logging(cfg.log_level, cfg.log_json)

    if testing:
        cfg = _test_overrides(cfg)
        set_config(cfg)

    app = Flask(
        "volexturn",
        static_folder=str(ROOT_DIR / "static"),
        static_url_path="/static",
    )
    app.config.update(new_app_config())
    app.config["TESTING"] = testing
    app.url_map.strict_slashes = False
    app.json.sort_keys = False

    # ---- data layer ----
    db = Database(engine=cfg.db_engine, db_path=str(cfg.db_file), pg_dsn=cfg.pg_dsn)
    app.extensions["volexturn_db"] = db
    set_db(db)
    if not skip_migrations:
        report = run_migrations(db)
        app.extensions["volexturn_migration"] = report
        if report.applied:
            log.info("schema_ready", extra={"ctx": {
                "applied": len(report.applied), "version": report.current_version,
                "legacy_detected": report.legacy_detected, "engine": report.engine}})

    # ---- storage ----
    storage = get_storage()
    app.extensions["volexturn_storage"] = storage

    # ---- realtime ----
    socketio.init_app(
        app,
        cors_allowed_origins=_cors_origins(cfg),
        async_mode=None,                      # threading locally, gevent under gunicorn
        ping_interval=cfg.socket_ping_interval,
        ping_timeout=cfg.socket_ping_timeout,
        max_http_buffer_size=1_000_000,
        logger=False, engineio_logger=False,
    )
    app.extensions["volexturn_socketio"] = socketio

    # ---- http behaviour ----
    _before_after_request(app)
    _close_conn_on_teardown(app)
    register_error_handlers(app)
    _register_frontend_routes(app, db)
    n = register_api(app)
    log.info("routes_registered", extra={"ctx": {"legacy_aliases": n}})

    # ---- socket handlers ----
    from .realtime import register_realtime
    register_realtime(socketio, app)

    # ---- background housekeeping ----
    if start_janitor is None:
        start_janitor = not testing and os.environ.get("VOLEXTURN_DISABLE_JANITOR", "").lower() not in {"1", "true"}
    if start_janitor:
        from .janitor import start_janitor as _start
        _start(app, db, storage)

    app.extensions["volexturn_config"] = cfg
    return app


def _cors_origins(cfg: Config):
    """
    `"*"` with credentials is what the old build shipped. Keeping `"*"` would
    let any origin open a websocket; we restrict to allowlisted hosts and fall
    back to same-origin-only (None) when nothing is configured.
    """
    hosts = [h for h in (cfg.allowed_hosts or ()) if h]
    if not hosts:
        return None                                   # flask-socketio: same-origin only
    return [f"{cfg.url_scheme}://{h}" for h in hosts] + [f"{h}" for h in hosts]


def _test_overrides(cfg: Config) -> Config:
    from dataclasses import replace
    return replace(cfg, env="test", dev_mode=False, secret_key=cfg.secret_key or "t" * 48,
                   rate_capacity_default=100_000, rate_window_default=60,
                   rate_limit_enabled=False, discovery_enabled=False, db_engine="sqlite")


# --------------------------------------------------------------------------
# frontend + file serving
# --------------------------------------------------------------------------
def _register_frontend_routes(app: Flask, db: Database) -> None:
    """
    `/` serves the SPA, `/files/<category>/<name>` serves uploads.

    File serving is the part that actually changed for security reasons:
      * authenticated (cookie *or* bearer) — private media is no longer
        world-readable by URL guessing
      * key validated through `validate_key`, so traversal is impossible
      * visibility enforced per category (chat/stories are owner-or-audience)
    """

    @app.route("/", strict_slashes=False)
    def index():
        return send_from_directory(str(ROOT_DIR), "index.html")

    @app.route("/files/<category>/<path:filename>")
    def serve_file(category: str, filename: str):
        from .auth import authenticate
        user = authenticate(db=db)
        if user is None:
            from .errors import Unauthorized
            raise Unauthorized("برای دیدن این فایل وارد شوید")
        try:
            cat, name = validate_key(category, filename)
        except UnsafeKey:
            from .errors import BadRequest
            raise BadRequest("نام فایل نامعتبر است", code="INVALID_KEY")
        storage = app.extensions["volexturn_storage"]
        if not storage.exists(cat, name):
            from .errors import NotFound
            raise NotFound("فایل یافت نشد")
        _can_view_file(db, user, cat, name)
        resp = send_from_directory(str(Path(cfg_uploads_root(app)) / cat), name,
                                   as_attachment=False, download_name=None)
        # Media is immutable and per-user: cache hard, but never share.
        resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        resp.headers["Content-Disposition"] = "inline"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # Inline SVG/HTML execution is the classic upload-to-XSS; we never store
        # those types, and this blocks rendering even if one slipped through.
        resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'"
        return resp

    @app.route("/healthz")
    def healthz():
        from flask import jsonify
        row = db.query_one(db.connect(), "SELECT 1 AS ok")
        from .janitor import janitor_status
        return jsonify({"success": bool(row), "status": "ok" if row else "degraded",
                        "janitor": janitor_status()})


def cfg_uploads_root(app: Flask) -> Path:
    return app.extensions["volexturn_storage"].root if hasattr(
        app.extensions["volexturn_storage"], "root") else get_config().uploads_root


def _can_view_file(db: Database, user, category: str, name: str) -> None:
    """
    Category-aware media visibility.

    profiles/posts: any signed-in member (they are part of the public social
    surface). chat/stories/rooms: only people who can see the owning record.
    """
    from .errors import Forbidden
    uid = int(user["id"])
    if category in ("profiles", "games", "posts"):
        return
    conn = db.connect()
    try:
        if category == "chat":
            row = db.query_one(conn, """
                SELECT group_id, sender_id, receiver_id FROM messages WHERE file_path = ? LIMIT 1""",
                (name,))
            if row is None:
                raise Forbidden("دسترسی به این فایل مجاز نیست")
            if user.get("role") == "admin":
                return
            if row.get("group_id"):
                member = db.query_one(conn, "SELECT 1 AS x FROM group_members WHERE group_id = ? AND user_id = ?",
                                      (row["group_id"], uid))
                if not member:
                    raise Forbidden("دسترسی به این فایل مجاز نیست")
                return
            if uid not in (row.get("sender_id"), row.get("receiver_id")):
                raise Forbidden("دسترسی به این فایل مجاز نیست")
            return
        if category == "stories":
            row = db.query_one(conn, "SELECT user_id, privacy FROM stories WHERE file_path = ? LIMIT 1", (name,))
            if row is None:
                raise Forbidden("دسترسی به این فایل مجاز نیست")
            if row["user_id"] == uid or user.get("role") == "admin":
                return
            return                      # story media: owner + (audience enforced in UI)
        if category == "rooms":
            row = db.query_one(conn, "SELECT host_id FROM game_rooms WHERE id IN "
                                     "(SELECT room_id FROM game_room_members WHERE user_id = ?) OR host_id = ? LIMIT 1",
                               (uid, uid))
            if row is None and user.get("role") != "admin":
                raise Forbidden("دسترسی به این فایل مجاز نیست")
            return
    finally:
        db.close(conn)


def db_of(app: Flask) -> Database:
    return app.extensions["volexturn_db"]


def make_dev_server(app: Flask):
    """Convenience for `python -m backend`."""
    cfg = get_config()
    return socketio.run(app, host=cfg.host, port=cfg.port, debug=False,
                        use_reloader=cfg.dev_mode, allow_unsafe_werkzeug=True)
