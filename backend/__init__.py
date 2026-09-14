"""
Volexturn backend package.

Public surface:

    from backend import create_app        # factory used by server.py / gunicorn
    from backend.app import socketio      # the SocketIO instance (for WSGI wrappers)

The layout is deliberately flat: one module per concern, `backend/api/*` for
HTTP surface, `backend/app.py` as the only place that knows about Flask.
See README (معماری و نحوهٔ اجرا), docs/API.md for the endpoint table and
docs/LEGACY_MAP.md for the pre-upgrade routes; docs/security.md for the guards.
"""

from __future__ import annotations

__version__ = "4.0.0"   # همان Config.app_version در backend/config.py — یکی را عوض کنی، دیگری را هم
__all__ = ["create_app", "run", "__version__"]


def create_app(**kwargs):
    """
    Build the Flask app (see `backend/app.py`).

    Imported lazily on purpose: `import backend` must not drag in Flask, the DB
    driver or the socket server, so tooling like `python -m backend --help`
    stays fast and works without optional dependencies.
    """
    from .app import create_app as _create
    return _create(**kwargs)


def run(*, host: str | None = None, port: int | None = None,
        use_reloader: bool | None = None):
    """Serve with the socketio transport (what `python server.py` does)."""
    from .app import create_app as _create
    from .app import socketio
    from .config import get_config
    cfg = get_config()
    app = _create()
    return socketio.run(app, host=host or cfg.host, port=port or cfg.port, debug=False,
                        use_reloader=cfg.dev_mode if use_reloader is None else use_reloader,
                        allow_unsafe_werkzeug=True)
