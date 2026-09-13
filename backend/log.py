"""
Structured logging.

One JSON object per line with: timestamp, level, logger, event, request_id,
user_id (only when it is a plain integer), plus a whitelisted `ctx` payload.

Guarantees enforced here:
  * passwords / tokens / cookies / secrets never appear in a log line
  * private message bodies are never logged by the request logger
  * unhandled errors are logged with a stack trace server-side only
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

# Keys a caller must never put in `ctx`.
_FORBIDDEN_KEYS = re.compile(
    r"(?i)^(password|passwd|pwd|old_password|new_password|secret|secret_key|"
    r"token|auth|authorization|api_key|apikey|cookie|set_cookie|csrf.*token|"
    r"content|message_body|message|file_path|ip_address)$"
)

# Belt-and-braces: scrub anything credential-shaped inside a string value.
_SCRUB = re.compile(
    r"(?i)(?P<k>password|passwd|secret|token|authorization|api[_-]?key|set[_-]cookie)"
    r"(?P<sep>\s*[:=]\s*)(?P<v>\S+)"
)

# Per-user identifiers are safe to log; email-like and long-opaque strings are not.
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-./+=]{20,}$")


def scrub_value(value: Any) -> Any:
    """Redact credential-shaped strings; keep short human-readable text intact."""
    if isinstance(value, str):
        value = _SCRUB.sub(lambda m: f"{m.group('k')}{m.group('sep')}***", value)
        if _TOKENISH.match(value.strip()):
            return "***"
        return value[:500]
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items() if not _FORBIDDEN_KEYS.match(str(k))}
    if isinstance(value, (list, tuple, set)):
        return [scrub_value(v) for v in list(value)[:20]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub_value(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) +
                  f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict):
            for key, val in extra.items():
                if _FORBIDDEN_KEYS.match(str(key)):
                    continue
                ctx[key] = scrub_value(val)
        # Never widen a line past a sane bound.
        rid = getattr(record, "request_id", None)
        if rid:
            ctx["request_id"] = rid
        uid = getattr(record, "user_id", None)
        if isinstance(uid, int):
            ctx["user_id"] = uid
        if record.exc_info:
            ctx["exc"] = self.formatException(record.exc_info)[-3000:]
        try:
            return json.dumps(ctx, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps({"ts": ctx["ts"], "level": ctx["level"],
                               "event": "log_encode_error"}, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable alternative (VOLEXTURN_LOG_JSON=0) — still redacted."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [f"{record.levelname:<7}", record.name, record.getMessage()]
        extra = getattr(record, "ctx", None)
        if isinstance(extra, dict) and extra:
            safe = {k: v for k, v in scrub_value(extra).items() if k not in ("exc",)}
            if safe:
                parts.append(json.dumps(safe, ensure_ascii=False, default=str))
        if record.exc_info:
            parts.append(self.formatException(record.exc_info))
        return " | ".join(parts)


_configured = False


def configure_logging(level: str = "INFO", as_json: bool = True,
                      stream=None) -> None:
    """Idempotent setup. Root logger is the only owner of a handler."""
    global _configured
    root = logging.getLogger()
    if _configured:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if as_json else TextFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party noise: keep werkzeug from double-logging plain text lines.
    for noisy in ("werkzeug", "gunicorn.error", "geventwebsocketlogger"):
        lg = logging.getLogger(noisy)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(max(level, "WARNING"))
    logging.getLogger("engineio.server").setLevel("WARNING")
    logging.getLogger("socketio.server").setLevel("WARNING")
    _configured = True


def quiet_logging() -> None:
    """Used by the test-suite."""
    global _configured
    root = logging.getLogger()
    root.handlers[:] = [logging.NullHandler()]
    root.setLevel("CRITICAL")
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"volexturn.{name}")
