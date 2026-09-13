"""
Volexturn configuration.

Everything environment-specific lives here, read once from the process
environment. No secret has a usable default — see `require_secret()`.

Precedence: explicit env var > derived default. `.env` (if present and
`python-dotenv` is installed) is loaded before the environment is read so
local development needs no shell plumbing.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

# Project root = directory containing `server.py`; `backend/` is its child.
ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Best-effort .env loading. Absent dependency or file is not an error."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        # Never override a real environment variable (container/CI wins).
        load_dotenv(env_path, override=False)


_load_dotenv()


# --------------------------------------------------------------------------
# env parsing helpers
# --------------------------------------------------------------------------
def env_str(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    return default if val is None or val == "" else val


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def env_list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in env_str(name, default).split(",") if p.strip()]


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a number, got {raw!r}")


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of runtime configuration."""

    # ---- identity -------------------------------------------------------
    app_name: str = "Volexturn"
    app_version: str = "4.0.0"
    env: str = "development"            # development | production | test

    # ---- network --------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 5000
    url_scheme: str = "http"
    # Hostnames accepted in Origin/Referer for same-site mutation checks.
    allowed_hosts: tuple[str, ...] = ()

    # ---- secrets ----------------------------------------------------------
    # Empty means "not configured": dev derives an ephemeral key (logged loudly);
    # production refuses to start. Rotating it invalidates every session token.
    secret_key: str = ""
    auto_admin_password: str = ""       # if set, seeds admin on first init only

    # ---- database ---------------------------------------------------------
    # `sqlite` (default, zero-config) or `postgresql` for production.
    db_engine: str = "sqlite"
    db_path: str = "database.db"        # sqlite only
    pg_dsn: str = ""                    # postgresql only: postgres://user:pw@host/db
    db_statement_timeout_ms: int = 15_000

    # ---- sessions / auth --------------------------------------------------
    session_ttl_days: int = 30          # idle token expiry
    session_max_per_user: int = 5
    session_idle_rotate_days: int = 7   # refresh token after this much use
    min_password_length: int = 8
    pbkdf2_iterations: int = 240_000
    legacy_iterations_floor: int = 100_000   # still verifiable, rehash on login
    login_max_attempts: int = 8
    login_window_seconds: int = 300

    # ---- rate limiting -----------------------------------------------------
    rate_capacity_default: int = 120        # requests
    rate_window_default: int = 60           # seconds
    rate_max_keys: int = 20_000             # bound the in-process limiter map
    # In-process token buckets are per-worker and lose their meaning under a test
    # runner (every case gets a fresh process, or shares one for all of them), so
    # tests disable them wholesale with VOLEXTURN_RATE_LIMITS=0.
    rate_limit_enabled: bool = True

    # ---- uploads ------------------------------------------------------------
    max_image_mb: int = 5
    max_video_mb: int = 10
    max_file_mb: int = 50
    max_avatar_mb: int = 3
    upload_folder: str = "uploads"
    # `.svg` is deliberately excluded: it can carry script (stored XSS).
    image_extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
    video_extensions: tuple[str, ...] = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")
    audio_extensions: tuple[str, ...] = (".mp3", ".wav", ".ogg", ".m4a", ".flac")
    archive_extensions: tuple[str, ...] = (".zip", ".rar", ".7z")
    doc_extensions: tuple[str, ...] = (".txt", ".pdf", ".json", ".csv", ".log")

    # ---- feed scoring (modular; see backend/feed.py) ----------------------
    feed_weights: dict = field(default_factory=lambda: {
        "like": 3.0, "comment": 4.0, "view": 0.4,
        "follow_boost": 12.0, "friend_boost": 20.0,
        "recency_hours_half_life": 18.0, "media_boost": 6.0,
    })

    # ---- pagination ---------------------------------------------------------
    default_page_size: int = 20
    max_page_size: int = 100

    # ---- gaming / LAN discovery ---------------------------------------------
    # Discovery is OFF by default and, when on, restricted to allowlisted CIDRs.
    discovery_enabled: bool = False
    discovery_interval_seconds: int = 60
    discovery_timeout_seconds: float = 1.5
    discovery_max_targets_per_tick: int = 40
    # e.g. "192.168.0.0/16,10.0.0.0/8". Empty = probe nothing.
    discovery_networks: tuple[str, ...] = ()
    heartbeat_timeout_seconds: int = 180     # online -> offline
    server_offline_grace_seconds: int = 24 * 3600   # before pruning
    room_stale_minutes: int = 720            # empty rooms auto-close

    # ---- housekeeping ---------------------------------------------------------
    janitor_interval_seconds: int = 60

    # ---- misc ---------------------------------------------------------------
    dev_mode: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    trust_x_forwarded_for: bool = False
    socket_ping_interval: int = 25
    socket_ping_timeout: int = 20

    # ------------------------------------------------------------------ factory
    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    @property
    def uploads_root(self) -> Path:
        p = Path(self.upload_folder)
        return p if p.is_absolute() else ROOT_DIR / p

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT_DIR / p

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            env=env_str("VOLEXTURN_ENV", env_str("FLASK_ENV", "development")),
            host=env_str("VOLEXTURN_HOST", env_str("HOST", "0.0.0.0")),
            port=env_int("PORT", env_int("VOLEXTURN_PORT", 5000)),
            url_scheme=env_str("VOLEXTURN_URL_SCHEME", "https" if env_bool("VOLEXTURN_IS_SECURE", False) else "http"),
            allowed_hosts=tuple(env_list("VOLEXTURN_ALLOWED_HOSTS", "localhost,127.0.0.1")),
            secret_key=env_str("VOLEXTURN_SECRET_KEY", env_str("SECRET_KEY")),
            auto_admin_password=env_str("VOLEXTURN_ADMIN_PASSWORD"),
            db_engine=env_str("VOLEXTURN_DB_ENGINE", "postgres" if env_str("DATABASE_URL") else "sqlite").lower(),
            db_path=env_str("VOLEXTURN_DB_PATH", "database.db"),
            pg_dsn=env_str("DATABASE_URL"),
            session_ttl_days=env_int("VOLEXTURN_SESSION_TTL_DAYS", 30),
            session_max_per_user=env_int("VOLEXTURN_SESSION_MAX_PER_USER", 5),
            min_password_length=env_int("VOLEXTURN_MIN_PASSWORD_LENGTH", 8),
            pbkdf2_iterations=env_int("VOLEXTURN_PBKDF2_ITERATIONS", 240_000),
            login_max_attempts=env_int("VOLEXTURN_LOGIN_MAX_ATTEMPTS", 8),
            rate_capacity_default=env_int("VOLEXTURN_RATE_CAPACITY", 120),
            rate_window_default=env_int("VOLEXTURN_RATE_WINDOW", 60),
            rate_limit_enabled=env_bool("VOLEXTURN_RATE_LIMITS", True),
            max_image_mb=env_int("VOLEXTURN_MAX_IMAGE_MB", 5),
            max_video_mb=env_int("VOLEXTURN_MAX_VIDEO_MB", 10),
            max_file_mb=env_int("VOLEXTURN_MAX_FILE_MB", 50),
            max_avatar_mb=env_int("VOLEXTURN_MAX_AVATAR_MB", 3),
            upload_folder=env_str("VOLEXTURN_UPLOAD_FOLDER", "uploads"),
            default_page_size=env_int("VOLEXTURN_PAGE_SIZE", 20),
            max_page_size=env_int("VOLEXTURN_MAX_PAGE_SIZE", 100),
            discovery_enabled=env_bool("VOLEXTURN_DISCOVERY_ENABLED", False),
            discovery_interval_seconds=env_int("VOLEXTURN_DISCOVERY_INTERVAL", 60),
            discovery_timeout_seconds=env_float("VOLEXTURN_DISCOVERY_TIMEOUT", 1.5),
            discovery_networks=tuple(env_list("VOLEXTURN_DISCOVERY_NETWORKS")),
            heartbeat_timeout_seconds=env_int("VOLEXTURN_HEARTBEAT_TIMEOUT", 180),
            server_offline_grace_seconds=env_int("VOLEXTURN_OFFLINE_GRACE", 24 * 3600),
            janitor_interval_seconds=env_int("VOLEXTURN_JANITOR_INTERVAL", 60),
            room_stale_minutes=env_int("VOLEXTURN_ROOM_STALE_MINUTES", 720),
            dev_mode=env_bool("VOLEXTURN_DEV_MODE", False),
            log_level=env_str("VOLEXTURN_LOG_LEVEL", "INFO").upper(),
            log_json=env_bool("VOLEXTURN_LOG_JSON", True),
            trust_x_forwarded_for=env_bool("VOLEXTURN_TRUST_XFF", False),
        )
        return cfg


_config: Config | None = None
_config_lock_secret: str | None = None


def get_config() -> Config:
    """Process-wide config singleton (frozen snapshot)."""
    global _config
    if _config is None:
        _config = Config.from_env()
        _validate(_config)
    return _config


def set_config(cfg: Config) -> None:
    """Override config — used by the test-suite."""
    global _config
    _config = cfg


def _validate(cfg: Config) -> None:
    if cfg.secret_key and len(cfg.secret_key) < 32:
        raise RuntimeError(
            "VOLEXTURN_SECRET_KEY is too short — use at least 32 characters "
            "(generate with: python -c \"import secrets;print(secrets.token_hex(32))\")"
        )
    if not cfg.secret_key and cfg.is_production:
        raise RuntimeError(
            "Refusing to start in production without VOLEXTURN_SECRET_KEY. "
            "Generate one: python -c \"import secrets;print(secrets.token_hex(32))\""
        )
    if not cfg.secret_key:
        # Ephemeral per-process key: dev convenience only. Sessions do not
        # survive a restart, which is exactly what you want in development.
        global _config_lock_secret
        if _config_lock_secret is None:
            _config_lock_secret = secrets.token_hex(32)
            import logging
            logging.getLogger("volexturn.config").warning(
                "VOLEXTURN_SECRET_KEY not set — using an ephemeral development key. "
                "Sessions will be invalidated on restart. Never do this in production."
            )


def signing_secret() -> str:
    """Secret used to derive stateless signatures (setup codes, invites)."""
    cfg = get_config()
    return cfg.secret_key or _config_lock_secret or "volexturn-insecure-dev-secret"


def new_app_config() -> dict:
    """Flat dict for `Flask.config` — keeps `MAX_CONTENT_LENGTH` semantics."""
    cfg = get_config()
    return {
        "SECRET_KEY": cfg.secret_key or _config_lock_secret or "volexturn-insecure-dev-secret",
        "MAX_CONTENT_LENGTH": (cfg.max_file_mb + 50) * 1024 * 1024,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": cfg.is_production,
        "PREFERRED_URL_SCHEME": cfg.url_scheme,
        "JSON_SORT_KEYS": False,
    }
