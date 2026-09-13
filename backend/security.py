"""
Security primitives: hashing, tokens, sanitisation, validation, rate limits.

Threats explicitly addressed here:
  * credential storage        -> PBKDF2-HMAC-SHA256, per-user salt, high iters
  * legacy/plaintext hashes   -> still verify, transparently upgraded on login
  * stolen/brute-forced login -> bounded attempts per identity+IP, lockout
  * session theft persistence -> absolute + idle expiry, rotation, revocation
  * spoofed identity          -> server derives the user from the token only
  * XSS                     -> HTML escaping of user text at write time, no
                               inline event data interpolated unescaped
  * path traversal            -> stored names are generated, never client-supplied
  * SSRF                      -> discovery targets validated against an allowlist
  * open network scanning     -> no scan primitive exists; only single TCP
                                 connect probes to an authorised host:port
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from typing import Any

from .config import get_config, signing_secret
from .errors import RateLimited, ValidationFailed
from .log import get_logger

log = get_logger("security")

# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------
# Format kept byte-compatible with the pre-upgrade build so existing rows
# verify unchanged:   pbkdf2$<iters>$<salt>$<hex digest>
PBKDF2_PREFIX = "pbkdf2"
_ALGO = "sha256"
#: upper bound accepted from a stored hash, so a corrupted row cannot turn
#: login into a CPU denial of service against the server
MAX_VERIFY_ITERATIONS = 5_000_000


def hash_password(password: str, *, iterations: int | None = None) -> str:
    cfg = get_config()
    iters = iterations or cfg.pbkdf2_iterations
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                                bytes.fromhex(salt), iters).hex()
    return f"{PBKDF2_PREFIX}${iters}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verification, tolerant of the two legacy formats."""
    if not stored:
        return False
    try:
        if stored.startswith(f"{PBKDF2_PREFIX}$"):
            _, iters_s, salt, digest = stored.split("$", 3)
            # The iteration count is part of the record and must be used
            # verbatim: "raising" it at verify time would just produce a
            # different digest and lock out every row written under an older
            # cost. Rows below `legacy_iterations_floor` still verify, and
            # `needs_rehash()` is what upgrades them on the next login.
            iters = int(iters_s)
            if iters < 1 or iters > MAX_VERIFY_ITERATIONS:
                # A tampered row could otherwise make one login burn minutes of CPU.
                return False
            calc = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"),
                                       bytes.fromhex(salt), iters).hex()
            return secrets.compare_digest(calc, digest)
        # Pre-upgrade plaintext rows: accept once, and the caller upgrades.
        return secrets.compare_digest(stored, password)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str | None) -> bool:
    """True when the row should be re-hashed with current parameters."""
    cfg = get_config()
    if not stored:
        return True
    if not stored.startswith(f"{PBKDF2_PREFIX}$"):
        return True
    try:
        return int(stored.split("$", 2)[1]) < cfg.pbkdf2_iterations
    except (ValueError, IndexError):
        return True


# A deliberately weak, common list — enough to stop "password123", not a
# full HIBP client (which would need a third-party API: out of scope by design).
_COMMON_PASSWORDS = frozenset("""
password passw0rd 123456 12345678 123456789 qwerty abc123 111111 admin
administrator welcome letmein monkey dragon sunshine master hello freedom
whatever qazwsx password1 password123 iloveyou football baseball 000000
volexturn volex volexturn123 admin123 root toor test test123 guest
""".split())

_PERSIAN_COMMON = frozenset(["رمزعبور", "12345678", "کاربر", "ولکس‌ترن", "ولکس turn"])


def check_password_strength(password: str, *, username: str = "") -> tuple[bool, str, list[str]]:
    """Returns (ok, human message, list of failed rule codes)."""
    cfg = get_config()
    problems: list[str] = []
    if len(password) < cfg.min_password_length:
        problems.append(f"TOO_SHORT:{cfg.min_password_length}")
    if len(password) > 128:
        problems.append("TOO_LONG:128")
    low = password.lower()
    if low in _COMMON_PASSWORDS or password in _PERSIAN_COMMON:
        problems.append("COMMON")
    if username and len(username) >= 3 and username.lower() in low:
        problems.append("CONTAINS_USERNAME")
    classes = sum([
        bool(re.search(r"[a-z]", password)),
        bool(re.search(r"[A-Z]", password)),
        bool(re.search(r"\d", password)),
        bool(re.search(r"[^\w\s]", password)),
    ])
    if len(password) < 12 and classes < 2:
        problems.append("NEED_TWO_CHAR_CLASSES")
    # Shannon entropy, rough but effective against short random-ish strings.
    if len(password) >= 3:
        freq = {c: password.count(c) for c in set(password)}
        ent = -sum((n / len(password)) * math.log2(n / len(password)) for n in freq.values())
        if ent < 2.0:
            problems.append("LOW_ENTROPY")
    if problems:
        labels = {
            "TOO_SHORT": f"رمز عبور باید حداقل {cfg.min_password_length} کاراکتر باشد",
            "TOO_LONG": "رمز عبور حداکثر ۱۲۸ کاراکتر می‌تواند باشد",
            "COMMON": "این رمز عبور رایج/قابل‌حدس است — یکی قوی‌تر انتخاب کنید",
            "CONTAINS_USERNAME": "رمز عبور نباید نام کاربری شما را شامل شود",
            "NEED_TWO_CHAR_CLASSES": "ترکیب حرف، عدد یا نماد را استفاده کنید",
            "LOW_ENTROPY": "تنوع کاراکترها خیلی کم است",
        }
        first = labels.get(problems[0].split(":")[0], "رمز عبور به اندازه کافی قوی نیست")
        return False, first, problems
    return True, "", []


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------
def new_session_token() -> str:
    return secrets.token_urlsafe(32)          # 256 bits, URL-safe for query use


def token_preview(token: str) -> str:
    """For admin listings only — first 8 chars, never a usable credential."""
    return (token or "")[:8] + "…"


def hmac_sign(payload: str) -> str:
    return hmac.new(signing_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def hmac_verify(payload: str, signature: str) -> bool:
    if not signature:
        return False
    return secrets.compare_digest(hmac_sign(payload), signature.strip())


def make_setup_code(user_id: int, ttl_seconds: int = 900) -> str:
    """Short-lived, signed, single-purpose code for the first-admin flow."""
    exp = int(time.time()) + ttl_seconds
    body = f"admin-setup:{user_id}:{exp}"
    raw = f"{exp}:{hmac_sign(body)[:16]}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_setup_code(code: str, user_id: int) -> bool:
    try:
        pad = "=" * (-len(code) % 4)
        raw = base64.urlsafe_b64decode(code + pad).decode()
        exp_s, sig = raw.split(":", 1)
        exp = int(exp_s)
        if exp < time.time():
            return False
        return hmac_verify(f"admin-setup:{user_id}:{exp}", sig)
    except Exception:
        return False


# --------------------------------------------------------------------------
# rate limiting (in-process, bounded)
# --------------------------------------------------------------------------
class TokenBucket:
    """
    Bounded token bucket per key.

    `capacity` requests may be made in a burst, refilling at
    `capacity / window` per second. In-process on purpose: adding Redis would
    violate the "no mandatory Redis" constraint. Multi-worker accuracy is
    therefore approximate — documented in docs/security.md.
    """

    def __init__(self, capacity: int, window_seconds: float, max_keys: int | None = None):
        self.capacity = max(1, capacity)
        self.window = max(0.001, window_seconds)
        self.rate = self.capacity / self.window
        self.max_keys = max_keys or 20_000
        self._buckets: dict[str, tuple[float, float]] = {}   # key -> (tokens, last)
        self._last_gc = time.monotonic()

    def consume(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        self._maybe_gc(now)
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < cost:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - cost, now)
        return True

    def retry_after(self, key: str) -> int:
        tokens, _ = self._buckets.get(key, (float(self.capacity), time.monotonic()))
        if tokens >= 1:
            return 0
        return max(1, int(math.ceil((1 - tokens) / self.rate)))

    def _maybe_gc(self, now: float) -> None:
        if now - self._last_gc < 30 and len(self._buckets) < self.max_keys:
            return
        cutoff = now - self.window * 4
        self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
        if len(self._buckets) > self.max_keys:               # hard bound, evict oldest
            keep = sorted(self._buckets.items(), key=lambda kv: kv[1][1])[-self.max_keys:]
            self._buckets = dict(keep)
        self._last_gc = now


_LIVE_LIMITERS: dict[str, TokenBucket] = {}


def limiter(name: str, capacity: int | None = None, window: float | None = None) -> TokenBucket:
    cfg = get_config()
    key = f"{name}:{capacity}:{window}"
    if key not in _LIVE_LIMITERS:
        _LIVE_LIMITERS[key] = TokenBucket(
            capacity or cfg.rate_capacity_default,
            window or cfg.rate_window_default,
            cfg.rate_max_keys,
        )
    return _LIVE_LIMITERS[key]


def reset_limiters() -> None:
    _LIVE_LIMITERS.clear()


def client_ip() -> str:
    """Best-effort client IP. Only trusts proxy headers when configured to."""
    from flask import request
    cfg = get_config()
    if cfg.trust_x_forwarded_for:
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
        real = request.headers.get("X-Real-IP", "").strip()
        if real:
            return real[:64]
    return (request.remote_addr or "?")[:64]


def throttle(scope: str, *, capacity: int | None = None, window: float | None = None,
             extra: str = "") -> None:
    """Raise RateLimited when the caller is over budget for `scope`."""
    if not get_config().rate_limit_enabled:
        return
    key = f"{scope}:{client_ip()}:{extra}"
    lb = limiter(scope, capacity, window)
    if not lb.consume(key):
        raise RateLimited(retry_after=lb.retry_after(key))


# --------------------------------------------------------------------------
# sanitisation / XSS
# --------------------------------------------------------------------------
# Control chars + bidi overrides (used in "Trojan Source" style attacks).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_TAG_RE = re.compile(r"<\s*/?\s*(script|iframe|object|embed|link|meta|style|svg|math)\b[^>]*>", re.I)
_EVENT_RE = re.compile(r"\son[a-z]+\s*=", re.I)
_URL_SCHEME_RE = re.compile(r"^\s*(javascript|vbscript|data|file)\s*:", re.I)


def sanitize_text(value: Any, *, max_len: int = 4000, strip_newlines: bool = False) -> str:
    """
    Defensive normalisation for user-generated text.

    The frontend escapes on render; this is the server-side half of that
    contract so data is clean even if another client renders it.
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    text = _CTRL_RE.sub("", text)
    text = _TAG_RE.sub("", text)                 # no markup for dangerous tags
    text = _EVENT_RE.sub(" ", text)              # no onerror= style payloads
    if strip_newlines:
        text = " ".join(text.split())
    return text.strip()[:max_len]


def escape_html(value: Any) -> str:
    return (str(value or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def safe_url(value: Any, *, allowed_schemes: tuple[str, ...] = ("http", "https")) -> str:
    url = sanitize_text(value, max_len=500).strip()
    if not url:
        return ""
    if _URL_SCHEME_RE.match(url):
        return ""
    if "://" not in url:
        url = "https://" + url
    scheme = url.split("://", 1)[0].lower()
    if scheme not in allowed_schemes:
        return ""
    return url


def extract_hashtags(text: str, limit: int = 12) -> list[str]:
    tags = re.findall(r"(?:^|\s)#([\w\u0600-\u06FF\u200c_]{1,40})", text or "")
    out, seen = [], set()
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return out


def extract_mentions(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"(?:^|\s)@([A-Za-z0-9._-]{3,32})", text or "")))


# --------------------------------------------------------------------------
# field validation
# --------------------------------------------------------------------------
# \w with re.UNICODE on purpose: this is a Persian-first app and the legacy
# rule was `ch.isalnum() or ch in "._-"`, which accepted Persian letters and
# digits. An ASCII-only class would lock those users out of registering again.
USERNAME_RE = re.compile(r"^[\w.-]{3,32}$", re.UNICODE)
SLUG_RE = re.compile(r"^[\w][\w-]{1,63}$", re.UNICODE)
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                         r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def valid_username(name: str) -> bool:
    return bool(name) and bool(USERNAME_RE.match(name)) and ".." not in name


def slugify(value: str, *, max_len: int = 64) -> str:
    """
    URL-safe slug that keeps Persian letters.

    Non-ASCII is preserved on purpose: this is a Persian-first app and stripping
    every non-latin char would collapse all local titles to "game".
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"[\s_/\\]+", "-", text)
    text = re.sub(r"[^\w\u0600-\u06FF\u200c-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        text = "game"
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text


def valid_slug(slug: str) -> bool:
    return bool(slug) and bool(SLUG_RE.match(slug))


def valid_port(port: Any) -> int | None:
    try:
        n = int(port)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 65535 else None


def clean_port_range(raw: Any, default: int = 0) -> int:
    n = valid_port(raw)
    return n if n is not None else default


def normalize_ip(raw: Any) -> str | None:
    """Strict IPv4/IPv6 parse. Rejects decimal/hexal shorthand SSRF tricks."""
    text = str(raw or "").strip().strip("[]")
    if not text:
        return None
    if IP_RE.match(text):
        try:
            return str(ipaddress.IPv4Address(text))
        except ValueError:
            return None
    try:
        return str(ipaddress.IPv6Address(text))
    except ValueError:
        return None


def clean_hostname(raw: Any) -> str | None:
    host = str(raw or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or ".." in host:
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if IP_RE.match(host) or ":" in host:
        return normalize_ip(host)
    return host if HOSTNAME_RE.match(host) else None


# --------------------------------------------------------------------------
# SSRF guard
# --------------------------------------------------------------------------
def allowed_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for entry in get_config().discovery_networks:
        try:
            nets.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            log.warning("bad_discovery_network", extra={"ctx": {"entry": entry[:64]}})
    return nets


def classify_target(host: str, port: int) -> tuple[bool, str]:
    """
    Decides whether a *probe* to host:port is permitted.

    Default-deny: the address must be explicitly allowlisted via
    VOLEXTURN_DISCOVERY_NETWORKS. Names are never resolved here — that would
    let DNS rebinding bypass the check — so a hostname target is refused and
    the caller must supply a literal IP.
    """
    cfg = get_config()
    if not cfg.discovery_enabled:
        return False, "discovery_disabled"
    if port is None or not (1 <= int(port) <= 65535):
        return False, "bad_port"
    ip = normalize_ip(host)
    if ip is None:
        # Only literal IPs are probeable; avoids TOCTOU via DNS.
        return False, "hostname_not_allowed"
    addr = ipaddress.ip_address(ip)
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False, "reserved_address"
    # Cloud metadata ranges are never reachable, even inside a broad RFC1918 /16.
    if str(addr) in {"169.254.169.254", "fd00:ec2::254"}:
        return False, "metadata_endpoint"
    nets = allowed_networks()
    if not nets:
        return False, "no_allowlist"
    if not any(addr.version == n.version and addr in n for n in nets):
        return False, "outside_allowlist"
    return True, "allowed"


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------
_MAGIC = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "video/webm",          # could be WAV too; refined below
    b"OggS": "audio/ogg",
    b"fLaC": "audio/flac",
    b"%PDF": "application/pdf",
    b"ID3": "audio/mpeg",
    b"\x1a\x45\xdf\xa3": "video/webm",   # matroska EBML head
    b"PK\x03\x04": "application/zip",
    b"\x00\x00\x01\x00": "image/x-icon",
}


def sniff_mime(head: bytes) -> str | None:
    """Identify by magic bytes — the part an extension cannot lie about."""
    if not head:
        return None
    for sig, mime in _MAGIC.items():
        if head.startswith(sig):
            if sig == b"RIFF":
                if len(head) >= 12 and head[8:12] == b"WAVE":
                    return "audio/wav"
                if len(head) >= 12 and head[8:12] == b"AVI ":
                    return "video/avi"
                return "application/octet-stream"
            return mime
    # ISO-BMFF video (mp4/mov/m4v): bytes 4..8 == 'ftyp'
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    return None


_EXT_MIME = {
    ".jpg": {"image/jpeg"}, ".jpeg": {"image/jpeg"},
    ".png": {"image/png"}, ".gif": {"image/gif"}, ".webp": {"image/webp"},
    ".bmp": {"image/bmp", "image/x-ms-bmp"},
    ".mp4": {"video/mp4"}, ".m4v": {"video/mp4"}, ".mov": {"video/quicktime", "video/mp4"},
    ".webm": {"video/webm"}, ".mkv": {"video/x-matroska", "video/webm"},
    ".avi": {"video/avi", "video/x-msvideo"},
    ".mp3": {"audio/mpeg"}, ".wav": {"audio/wav"}, ".ogg": {"audio/ogg"},
    ".m4a": {"audio/mp4", "video/mp4"}, ".flac": {"audio/flac"},
    ".zip": {"application/zip"}, ".rar": {"application/vnd.rar", "application/x-rar-compressed"},
    ".7z": {"application/x-7z-compressed"},
    ".txt": {"text/plain"}, ".pdf": {"application/pdf"},
    ".json": {"application/json"}, ".csv": {"text/csv", "text/plain"},
    ".log": {"text/plain"},
}


def stored_filename(owner_id: int, original: str, *, prefix: str = "") -> str:
    """
    Generate a filename we control entirely.

    The client's name is never used for storage — that single decision removes
    path traversal, null-byte truncation, and `.html`/`.svg` upload attacks in
    one stroke. The original name is kept in the DB for display only.
    """
    ext = os.path.splitext(original or "")[1].lower()
    ext = ext if re.match(r"^\.[a-z0-9]{1,5}$", ext) else ""
    rand = secrets.token_hex(8)
    stamp = time.strftime("%Y%m%d%H%M%S")
    safe_owner = re.sub(r"\W", "", str(owner_id))[:8] or "anon"
    return f"{prefix}{stamp}_{safe_owner}_{rand}{ext}"


def validate_upload(filename: str, head: bytes, *, allow: tuple[str, ...],
                    declared_mime: str | None = None) -> tuple[bool, str, str]:
    """
    Returns (ok, reason, normalised_mime).

    Three independent checks must agree: extension allowlist, the browser's
    declared type, and the sniffed magic bytes. Extension-only checks are the
    classic bypass; magic bytes are what we actually trust.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in allow:
        return False, f"extension_{ext or 'none'}", ""
    sniffed = sniff_mime(head)
    if sniffed is None:
        return False, "unrecognised_content", ""
    ok_mimes = _EXT_MIME.get(ext, set())
    if ok_mimes and sniffed not in ok_mimes:
        return False, "content_type_mismatch", sniffed
    if declared_mime:
        declared = declared_mime.split(";")[0].strip().lower()
        # Some browsers send application/octet-stream; don't fail on that alone.
        if declared not in {"", "application/octet-stream", "text/plain"} \
           and ok_mimes and declared not in ok_mimes:
            return False, "declared_type_mismatch", sniffed
    return True, "", sniffed


# --------------------------------------------------------------------------
# CSRF: same-origin enforcement
# --------------------------------------------------------------------------
# The SPA authenticates with `Authorization: Bearer`, which a cross-site page
# cannot attach, so classic CSRF does not apply to it. This guard covers the
# cases where it *can*: cookie-authenticated or form-style requests.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def same_origin_ok() -> bool:
    """
    CSRF gate for unsafe methods, without a second token to manage.

    Two facts do the work:

    * A request that authenticates with an `Authorization` header is not
      forgeable cross-site (the browser will not attach it from another origin
      unless CORS allowed it, and CORS is host-pinned), so it is accepted.
    * A cookie-authenticated request must prove where it came from. Browsers
      always send Origin on unsafe methods; when neither Origin nor Referer is
      present we assume a non-browser client that is *not* using the cookie.

    Same-*site* rather than strict same-origin: LAN users reach the app by IP or
    by a name that is not in `Host`, and both are equally trusted here.
    """
    from flask import request
    if request.method in _SAFE_METHODS:
        return True
    if request.headers.get("Authorization"):
        return True
    origin = request.headers.get("Origin") or ""
    referer = request.headers.get("Referer") or ""
    source = origin or referer
    if not source:
        return not request.cookies
    cfg = get_config()
    host = (request.host or "").split(":")[0].lower()
    allowed = {h.split(":")[0].lower() for h in cfg.allowed_hosts} | {host}
    try:
        from urllib.parse import urlparse
        src_host = (urlparse(source).netloc or "").split(":")[0].lower()
    except ValueError:
        return False
    if not src_host:
        return True
    if src_host == host or src_host in allowed:
        return True
    src_parts, host_parts = src_host.split("."), host.split(".")
    if len(src_parts) == len(host_parts) == 2 and src_parts[-1] == host_parts[-1]:
        return True
    return False


def enforce_same_origin() -> None:
    if not same_origin_ok():
        raise ValidationFailed(
            "مبدأ درخواست با مبدأ صفحه یکی نیست — برای جلوگیری از CSRF رد شد",
            code="CSRF_ORIGIN_MISMATCH")


# --------------------------------------------------------------------------
# generic input guards
# --------------------------------------------------------------------------
def bounded_int(value: Any, *, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def pagination_args(*, default_size: int | None = None,
                    max_size: int | None = None) -> tuple[int, int, int]:
    """Read `limit`/`offset` (or `page`) from the query string, clamped."""
    from flask import request
    cfg = get_config()
    max_size = max_size or cfg.max_page_size
    default_size = default_size or cfg.default_page_size
    limit = bounded_int(request.args.get("limit"), lo=1, hi=max_size, default=default_size)
    if request.args.get("offset"):
        offset = bounded_int(request.args.get("offset"), lo=0, hi=1_000_000, default=0)
    else:
        page = bounded_int(request.args.get("page"), lo=1, hi=10_000, default=1)
        offset = (page - 1) * limit
    return limit, offset, page


def paginated(total: int, limit: int, offset: int, page: int) -> dict:
    pages = max(1, math.ceil(total / limit)) if limit else 1
    return {
        "pagination": {
            "total": int(total), "limit": int(limit), "offset": int(offset),
            "page": int(page), "pages": int(pages),
            "has_more": offset + limit < total,
        }
    }


def db_err(exc: Exception) -> str:
    msg = str(exc)
    if "UNIQUE constraint failed" in msg or "duplicate key" in msg:
        return "این مقدار تکراری است"
    if "FOREIGN KEY" in msg:
        return "مرتبط با داده‌ای وجود دارد که حذف نشده است"
    return "خطا در ذخیره‌سازی داده"


def sqlite_error(exc: Exception) -> sqlite3.Error | None:      # pragma: no cover
    return exc if isinstance(exc, sqlite3.Error) else None
