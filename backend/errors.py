"""
Consistent API error envelope + the exception types that produce it.

Every failure the client can see has this shape:

    {"success": false, "error": {"code": "...", "message": "...", "details": {...}},
     "request_id": "..."}

`message` is always safe to show a human; `details` is machine-readable.
Tracebacks never leave the process — see `register_error_handlers`.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

log = logging.getLogger("volexturn.errors")


class ApiError(Exception):
    """Base class for every deliberate, client-visible failure."""

    status = 400
    code = "INVALID_REQUEST"
    message = "درخواست نامعتبر است"

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 status: int | None = None, details: dict[str, Any] | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message
        self.code = code or self.code
        self.status = status or self.status
        self.details = details or {}

    def to_response(self):
        body: dict[str, Any] = {
            "success": False,
            "error": {"code": self.code, "message": self.message},
            # Compat bridge: the pre-upgrade SPA does `toast(res.message)` on
            # failure, so the flat key must keep existing alongside `error`.
            "message": self.message,
        }
        if self.details:
            body["error"]["details"] = self.details
        rid = getattr(g, "request_id", None)
        if rid:
            body["request_id"] = rid
        return jsonify(body), self.status


# ---------------------------------------------------------------- subtypes
class BadRequest(ApiError):
    status, code, message = 400, "INVALID_REQUEST", "درخواست نامعتبر است"


class ValidationFailed(ApiError):
    status, code, message = 422, "VALIDATION_FAILED", "داده‌های ارسالی معتبر نیست"


class Unauthorized(ApiError):
    status, code, message = 401, "UNAUTHORIZED", "نیاز به ورود — دوباره وارد شوید"


class Forbidden(ApiError):
    status, code, message = 403, "FORBIDDEN", "دسترسی غیرمجاز"


class NotFound(ApiError):
    status, code, message = 404, "NOT_FOUND", "موردی یافت نشد"


class Conflict(ApiError):
    status, code, message = 409, "CONFLICT", "تعارض با وضعیت فعلی وجود دارد"


class RateLimited(ApiError):
    status, code, message = 429, "RATE_LIMITED", "تلاش بیش از حد — کمی بعداً دوباره امتحان کنید"

    def __init__(self, message: str | None = None, *, retry_after: int = 60, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after

    def to_response(self):
        resp = super().to_response()
        resp[0].headers["Retry-After"] = str(self.retry_after)
        return resp


class PayloadTooLarge(ApiError):
    status, code, message = 413, "FILE_TOO_LARGE", "حجم فایل بیش از حد مجاز است"


class UnsupportedMediaType(ApiError):
    status, code, message = 415, "UNSUPPORTED_FILE_TYPE", "نوع فایل پشتیبانی نمی‌شود"


class Locked(ApiError):
    status, code, message = 423, "LOCKED", "این حساب مسدود است"


class ServerError(ApiError):
    status, code, message = 500, "INTERNAL_ERROR", "خطای داخلی سرور — لطفاً بعداً تلاش کنید"


class NotImplementedFeature(ApiError):
    status, code, message = 501, "NOT_IMPLEMENTED", "این قابلیت هنوز پیاده‌سازی نشده است"


# ------------------------------------------------------------ registration
_HTTP_MAP = {
    400: ("INVALID_REQUEST", "درخواست نامعتبر است"),
    401: ("UNAUTHORIZED", "نیاز به ورود است"),
    403: ("FORBIDDEN", "دسترسی غیرمجاز"),
    404: ("NOT_FOUND", "مسیری یافت نشد"),
    405: ("METHOD_NOT_ALLOWED", "این روش مجاز نیست"),
    408: ("TIMEOUT", "زمان درخواست پایان یافت"),
    413: ("FILE_TOO_LARGE", "حجم درخواست بیش از حد مجاز است"),
    415: ("UNSUPPORTED_FILE_TYPE", "نوع فایل پشتیبانی نمی‌شود"),
    429: ("RATE_LIMITED", "تلاش بیش از حد"),
    500: ("INTERNAL_ERROR", "خطای داخلی سرور"),
    502: ("UPSTREAM_ERROR", "خطای سمت سرویس"),
    503: ("UNAVAILABLE", "سرویس موقتاً در دسترس نیست"),
}

# Anything that looks like a credential must never reach a log or a response.
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|authorization|api[_-]?key|set[_-]cookie)\b"
)


def redact(text: str) -> str:
    """Scrub credential-looking `key=value` pairs from a free-text string."""
    if not text:
        return text
    return re.sub(
        r"(?i)(?P<k>password|passwd|secret|token|authorization|api[_-]?key|set[_-]cookie)"
        r"(?P<sep>\s*[:=]\s*)(?P<v>[^\s,;}&]+)",
        lambda m: f"{m.group('k')}{m.group('sep')}***REDACTED***",
        text,
    )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _api_error(err: ApiError):
        # Client errors are expected noise: log at INFO/DEBUG, never with a trace.
        log.info("api_error", extra={"ctx": {
            "code": err.code, "status": err.status, "path": request.path,
        }})
        return err.to_response()

    @app.errorhandler(HTTPException)
    def _http_error(err: HTTPException):
        code, msg = _HTTP_MAP.get(err.code or 500, ("ERROR", "خطای ناشناخته"))
        message = msg if err.code != 404 else "مسیری یافت نشد"
        body: dict[str, Any] = {
            "success": False,
            "error": {"code": code, "message": message},
            "message": message,
        }
        rid = getattr(g, "request_id", None)
        if rid:
            body["request_id"] = rid
        resp = jsonify(body)
        resp.status_code = err.code or 500
        if err.code == 405 and getattr(err, "valid_methods", None):
            resp.headers["Allow"] = ", ".join(sorted(err.valid_methods))
        return resp

    @app.errorhandler(Exception)
    def _unhandled(err: Exception):
        """Last line of defence: log loudly server-side, answer generically."""
        is_http = isinstance(err, HTTPException)
        if is_http and err.code and err.code < 500:      # type: ignore[attr-defined]
            return _http_error(err)
        rid = getattr(g, "request_id", "-")
        log.error(
            "unhandled_exception",
            extra={"ctx": {
                "path": request.path, "method": request.method,
                "exc_type": type(err).__name__,
                # never surface this to the client
                "exc": redact(f"{err}"),
                "stack": redact("".join(traceback.format_exception(err)))[-4000:],
            }},
            exc_info=err,
        )
        internal_msg = "خطای داخلی سرور. شناسه: " + str(rid)
        body = {
            "success": False,
            "error": {"code": "INTERNAL_ERROR", "message": internal_msg},
            "message": internal_msg,
        }
        if rid:
            body["request_id"] = rid
        # In development the trace is genuinely useful; gate it hard.
        from .config import get_config
        if get_config().dev_mode:
            body["error"]["debug"] = redact(f"{type(err).__name__}: {err}")
        return jsonify(body), 500
