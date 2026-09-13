"""
API plumbing shared by every module.

`Module.route(...)` declares an endpoint once and gets, for free:
  * auth enforcement (`none` / `optional` / `user` / `admin`)
  * same-origin (CSRF) enforcement on unsafe methods
  * a per-endpoint rate limit, keyed by user when one is known
  * a **legacy alias**: every pre-upgrade root path keeps working against the
    same view function, so old clients are unaffected (only the URL differs).

`register_api(app)` mounts every blueprint and wires the aliases. The full
mapping lives in docs/api.md.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Sequence

from flask import Blueprint, Flask, g, jsonify, request

from ..auth import authenticate
from ..config import get_config
from ..db import Database, Row
from ..errors import ApiError, BadRequest, Forbidden, Unauthorized
from ..log import get_logger
from ..security import client_ip, enforce_same_origin, sanitize_text, throttle

log = get_logger("api")

_SAFE = {"GET", "HEAD", "OPTIONS"}

#: (rule, methods, endpoint) collected by Module.route, drained by register_api.
LEGACY_ALIASES: list[tuple[str, tuple[str, ...], str]] = []

#: Blueprint for pre-upgrade paths whose response *shape* differs from the new
#: canonical one (the old SPA reads bare arrays from `/users`, `/posts`, ...).
#: Those need their own view function, not an alias, so they live here.
LEGACY_BP = Blueprint("legacy", "backend.api.legacy")
_LEGACY_SEEN: set[str] = set()
_MODULES: dict[str, "Module"] = {}

#: prefix each module is mounted at — the canonical surface.
PREFIXES: dict[str, str] = {
    "meta": "/api", "auth": "/api/auth", "users": "/api/users",
    "posts": "/api/posts", "stories": "/api/stories", "messages": "/api/messages",
    "groups": "/api/groups", "gaming": "/api/games", "servers": "/api/servers",
    "rooms": "/api/rooms", "notifications": "/api/notifications",
    "search": "/api/search", "reports": "/api/reports", "admin": "/api/admin",
}

MODULE_ORDER: tuple[str, ...] = (
    "meta", "auth", "users", "posts", "stories", "messages", "groups",
    "gaming", "servers", "rooms", "notifications", "search", "reports", "admin",
)


# --------------------------------------------------------------------------
# Module
# --------------------------------------------------------------------------
class Module:
    """Owns one blueprint and the routes declared on it."""

    def __init__(self, name: str, *, prefix: str | None = None):
        self.name = name
        self.prefix = prefix if prefix is not None else PREFIXES.get(name, f"/api/{name}")
        self.bp = Blueprint(name, f"backend.api.{name}", url_prefix=self.prefix)
        _MODULES[name] = self

    def route(self, rule: str, *, methods: Sequence[str] | None = None,
              auth: str = "user", rate: tuple[int, float] | str | None = "default",
              csrf: bool = True, legacy: str | None = None,
              legacy_methods: Sequence[str] | None = None,
              endpoint: str | None = None) -> Callable:
        """
        `auth`: none | optional | user | admin
        `rate`: "default" | None | (capacity, window_seconds)
        """
        methods = tuple(dict.fromkeys(methods or ("GET",)))

        def decorator(fn: Callable) -> Callable:
            ep = endpoint or fn.__name__
            wrapper = _guarded(fn, auth=auth, rate=rate, csrf=csrf, ep=ep, methods=methods)
            wrapper.__name__ = fn.__name__
            # Blueprint endpoints must not contain a dot (Flask prefixes the
            # blueprint name itself); `full` is the *app-level* name aliases use.
            full = f"{self.name}.{ep}"
            self.bp.add_url_rule(rule, ep, wrapper, methods=list(methods),
                                 strict_slashes=False, merge_slashes=True)
            if legacy:
                LEGACY_ALIASES.append(
                    (legacy, tuple(legacy_methods or methods), full))
            return wrapper

        return decorator

    def legacy(self, rule: str, *, methods: Sequence[str] = ("GET",),
               auth: str = "user", rate: tuple[int, float] | str | None = "default"):
        """
        Register a view at a root-level pre-upgrade path.

        Use this instead of `legacy=` whenever the old response shape is not the
        new one — e.g. `/users` returned a bare array, `/login` did not.
        """
        methods = tuple(dict.fromkeys(methods or ("GET",)))

        def decorator(fn: Callable) -> Callable:
            ep = f"legacy_{self.name}_{fn.__name__}"
            wrapper = _guarded(fn, auth=auth, rate=rate, csrf=True, ep=ep, methods=methods)
            wrapper.__name__ = fn.__name__
            if ep in _LEGACY_SEEN:                             # pragma: no cover
                return wrapper
            _LEGACY_SEEN.add(ep)
            LEGACY_BP.add_url_rule(rule, ep, wrapper, methods=list(methods),
                                   strict_slashes=False, merge_slashes=True)
            return wrapper

        return decorator


def _guarded(fn: Callable, *, auth: str, rate: tuple[int, float] | str | None,
             csrf: bool, ep: str, methods: tuple[str, ...] = ("GET",)) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        cfg = get_config()

        # 1. identity — the only trusted source of "who"
        if auth in ("user", "admin"):
            user = authenticate()
            if user is None:
                raise Unauthorized()
            if auth == "admin" and (user.get("role") or "user") != "admin":
                raise Forbidden("دسترسی مدیریتی لازم است")
        elif auth == "optional":
            authenticate()

        # 2. cross-site request forgery guard on state-changing verbs
        if csrf and not set(methods).issubset(_SAFE):
            enforce_same_origin()

        # 3. rate limiting, keyed by user when authenticated else by IP
        who = f"u{int(g.user['id'])}" if getattr(g, "user", None) else client_tag()
        if rate == "default":
            throttle(f"rl:{ep}", capacity=cfg.rate_capacity_default,
                     window=cfg.rate_window_default, extra=who)
        elif rate is not None:
            capacity, window = rate
            throttle(f"rl:{ep}", capacity=capacity, window=window, extra=who)

        g._vx_endpoint = ep
        return fn(*args, **kwargs)

    wrapper._vx_auth = auth                                  # type: ignore[attr-defined]
    wrapper._vx_methods = methods                            # type: ignore[attr-defined]
    wrapper.__wrapped_view = fn                              # type: ignore[attr-defined]
    return wrapper


def client_tag() -> str:
    return "ip:" + (client_ip() or "?")


# --------------------------------------------------------------------------
# accessors used by every module
# --------------------------------------------------------------------------
def current_db() -> Database:
    from flask import current_app
    return current_app.extensions["volexturn_db"]


def conn():
    """Request-scoped connection, opened lazily and closed at teardown."""
    from flask import current_app
    existing = getattr(g, "_vx_conn", None)
    if existing is not None:
        return existing
    c = current_app.extensions["volexturn_db"].connect()
    g._vx_conn = c
    return c


def tx():
    """Context manager for a request-scoped transaction."""
    return current_db().tx(conn())


def me() -> Row:
    """The authenticated user. Always this, never a client-supplied id."""
    user = getattr(g, "user", None)
    if user is None:
        user = authenticate()
    if user is None:
        raise Unauthorized()
    return user


def my_id() -> int:
    return int(me()["id"])


def actor_name() -> str:
    user = getattr(g, "user", None)
    return str(user.get("full_name") or user.get("username") or "?") if user else "?"


def is_admin_user() -> bool:
    user = getattr(g, "user", None)
    return bool(user) and (user.get("role") or "user") == "admin"


def payload(*, form: bool = False) -> dict[str, Any]:
    """
    Read scalar inputs from whichever body the client actually sent.

    `form=True` is for endpoints that also accept multipart uploads (the SPA
    posts FormData there). Those must keep working for JSON clients too, so the
    JSON body is the base and form fields *override* it — a multipart request
    that sends `full_name` and an `avatar` file wins on `full_name`, while a
    pure-JSON request to the same endpoint is not silently dropped. Getting this
    wrong loses user data without an error, which is the worst kind of bug.
    """
    data: dict[str, Any] = {}
    js = request.get_json(silent=True, force=False)
    if isinstance(js, dict):
        data.update(js)
    if form:
        data.update(request.form.to_dict(flat=True) or {})
        return data
    if not isinstance(js, dict):
        form_data = request.form.to_dict(flat=True) or {}
        data.update(form_data if form_data else (request.args.to_dict(flat=True) or {}))
    return data


def arg(name: str, default: Any = None, *, src: dict | None = None) -> Any:
    if src is None:
        src = payload()
    val = src.get(name)
    if val is None or (isinstance(val, str) and not val.strip()):
        return default
    return val


def int_arg(name: str, default: int | None = None, *, src: dict | None = None,
            lo: int | None = None, hi: int | None = None) -> int | None:
    from ..security import bounded_int
    val = arg(name, None, src=src)
    if val is None:
        return default
    try:
        n = int(val)
    except (TypeError, ValueError):
        if default is None:
            raise BadRequest(f"«{name}» باید عدد باشد", code="INVALID_INTEGER")
        return default
    return bounded_int(n, lo=lo if lo is not None else -2**63,
                       hi=hi if hi is not None else 2**63, default=default or 0)


def float_arg(name: str, default: float | None = None, *, src: dict | None = None) -> float | None:
    val = arg(name, None, src=src)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def bool_arg(name: str, default: bool = False, *, src: dict | None = None) -> bool:
    val = arg(name, None, src=src)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def text_field(name: str, src: dict, *, max_len: int = 500, min_len: int = 1,
               required: bool = True, newlines: bool = False) -> str:
    """Sanitised string field. `required` raises a client-safe 400."""
    text = sanitize_text(src.get(name), max_len=max_len, strip_newlines=not newlines)
    if not text:
        if required:
            raise BadRequest(f"«{name}» الزامی است", code="FIELD_REQUIRED",
                             details={"field": name})
        return text
    if len(text) < min_len:
        # Distinct from "missing": the client did send something, it is just too
        # short — answering "this field is required" there is confusing.
        raise BadRequest(f"«{name}» باید حداقل {min_len} کاراکتر باشد", code="FIELD_TOO_SHORT",
                         details={"field": name, "min_len": min_len})
    return text


def ok(**fields: Any):
    body: dict[str, Any] = {"success": True}
    body.update(fields)
    return jsonify(body)


def fail(message: str, *, code: str = "INVALID_REQUEST", status: int = 400,
         details: dict | None = None):
    return ApiError(message, code=code, status=status, details=details).to_response()


# --------------------------------------------------------------------------
# mounting
# --------------------------------------------------------------------------
def module_for(name: str) -> Module:
    return _MODULES[name]


def register_api(app: Flask) -> int:
    """Import every module (each self-registers on import), then mount."""
    import importlib

    for name in MODULE_ORDER:
        if name not in _MODULES:                              # pragma: no cover
            importlib.import_module(f"backend.api.{name}")
        mod = _MODULES.get(name)
        if mod is None:                                       # pragma: no cover
            log.error("module_missing", extra={"ctx": {"module": name}})
            continue
        app.register_blueprint(mod.bp)

    if LEGACY_BP.deferred_functions and "legacy" not in app.blueprints:
        app.register_blueprint(LEGACY_BP)

    added = 0
    for rule, methods, endpoint in LEGACY_ALIASES:
        view = app.view_functions.get(endpoint)
        if view is None:                                      # pragma: no cover
            log.error("legacy_alias_missing_view", extra={"ctx": {"endpoint": endpoint}})
            continue
        tag = f"legacy:{rule}:{','.join(sorted(methods))}"
        if tag in app.view_functions:
            continue
        app.add_url_rule(rule, tag, view, methods=list(methods), strict_slashes=False)
        added += 1
    log.info("api_mounted", extra={"ctx": {"modules": len(_MODULES), "legacy_aliases": added}})
    return added


def all_routes(app: Flask) -> list[dict]:
    """Introspection, used by `/api/routes` and the docs generator."""
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        if rule.endpoint == "static":
            continue
        methods = sorted(m for m in (rule.methods or set()) if m not in {"HEAD", "OPTIONS"})
        out.append({"rule": str(rule), "endpoint": rule.endpoint, "methods": methods})
    return out


def get_db() -> Database:
    return current_db()
