"""
Forward-only, idempotent schema migrations with a version table.

Rules this module enforces (spec §23 / §37):

  * Never destructive. No column is dropped, no table truncated. Destructive
    cleanups live in `retire/` steps that are explicitly opted into.
  * An **existing legacy database** (created by the old `init_db()`, which had
    no version table) is detected and stamped as baseline instead of being
    recreated — so current rows survive the upgrade untouched.
  * Every migration is re-runnable: `IF NOT EXISTS`, `INSERT OR IGNORE`,
    and column-existence checks before `ALTER TABLE ... ADD COLUMN`.
  * DDL is authored once in a SQLite-compatible subset and translated for
    PostgreSQL by `translate_ddl()`, so both engines get the same schema.

`migrations/` on disk is reserved for hand-written, dialect-specific scripts
that this runner will pick up and apply in version order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .config import ROOT_DIR
from .db import POSTGRES, Database, Row, split_sql
from .log import get_logger

log = get_logger("migrations")

VERSION_TABLE = "vx_schema_version"
DISK_MIGRATIONS_DIR = ROOT_DIR / "migrations"


# --------------------------------------------------------------------------
# DDL translation: the single source of schema truth
# --------------------------------------------------------------------------
def translate_ddl(sql: str, engine: str) -> str:
    """Rewrite the SQLite DDL subset used below for PostgreSQL."""
    if engine != POSTGRES:
        return sql
    out = sql
    out = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                 "BIGSERIAL PRIMARY KEY", out, flags=re.I)
    out = re.sub(r"\bAUTOINCREMENT\b", "", out, flags=re.I)
    out = re.sub(r"\bINTEGER\s+UNIQUE\b", "BIGINT UNIQUE", out, flags=re.I)
    out = re.sub(r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+\(\s*id\s+BIGSERIAL",
                 r"CREATE TABLE IF NOT EXISTS \1 (id BIGSERIAL", out, flags=re.I)
    out = re.sub(r"\bDATETIME\b", "TIMESTAMP", out, flags=re.I)
    out = re.sub(r"DEFAULT\s*\(\s*datetime\('now'(?:\s*,\s*'([^']+)')?\s*\)\s*\)",
                 lambda m: "DEFAULT " + (_pg_interval(m.group(1)) if m.group(1) else "CURRENT_TIMESTAMP"),
                 out, flags=re.I)
    out = re.sub(r"DEFAULT\s+CURRENT_TIMESTAMP", "DEFAULT CURRENT_TIMESTAMP", out, flags=re.I)
    out = re.sub(r"\bTEXT\s+UNIQUE\b", "TEXT UNIQUE", out, flags=re.I)
    # SQLite tolerates an untyped column; Postgres needs one.
    out = re.sub(r"(\b\w+)\s*,\s*$", r"\1 TEXT,", out, flags=re.M)
    return out


def _pg_interval(affix: str) -> str:
    """`'+24 hours'` -> `(CURRENT_TIMESTAMP + interval '24 hours')`."""
    m = re.match(r"([+-])\s*(\d+)\s+(\w+)", affix.strip())
    if not m:
        return "CURRENT_TIMESTAMP"
    sign, num, unit = m.groups()
    unit = unit.rstrip("s") if unit not in {"hours", "minutes", "seconds"} else unit
    return f"(CURRENT_TIMESTAMP + interval '{'-'+num if sign == '-' else num+' '} {unit}')"


# --------------------------------------------------------------------------
# migration definition
# --------------------------------------------------------------------------
@dataclass
class Migration:
    version: int
    name: str
    apply: Callable[[Database, object], None]
    #: human-readable summary for `python -m backend.migrations status`
    note: str = ""
    #: True when the step cannot be safely replayed on a partially-migrated db
    destructive: bool = False


def _table_exists(db: Database, conn, name: str) -> bool:
    return db.has_table(conn, name)


def _has_col(db: Database, conn, table: str, col: str) -> bool:
    return col in db.columns(conn, table)


def _create(db: Database, conn, ddl: str) -> None:
    for stmt in split_sql(ddl):
        db.execute(conn, translate_ddl(stmt, db.engine)).close()


def _add_cols(db: Database, conn, table: str, cols: list[tuple[str, str]]) -> None:
    """Idempotent ADD COLUMN. Skips silently if the table is not there yet."""
    if not _table_exists(db, conn, table):
        return
    existing = db.columns(conn, table)
    for name, spec in cols:
        if name in existing:
            continue
        sql = f"ALTER TABLE {table} ADD COLUMN {name} {spec}"
        try:
            db.execute(conn, translate_ddl(sql, db.engine)).close()
        except Exception as exc:                                  # pragma: no cover
            log.warning("add_column_failed", extra={"ctx": {"table": table, "col": name,
                                                            "err": str(exc)[:160]}})


def _index(db: Database, conn, name: str, table: str, cols: str, *, unique: bool = False) -> None:
    if not _table_exists(db, conn, table):
        return
    kind = "UNIQUE INDEX" if unique else "INDEX"
    _create(db, conn, f"CREATE {kind} IF NOT EXISTS {name} ON {table} ({cols})")


def _backfill(db: Database, conn, sql: str, params=None) -> None:
    try:
        db.execute(conn, sql, params).close()
    except Exception as exc:                                     # pragma: no cover
        log.warning("backfill_failed", extra={"ctx": {"err": str(exc)[:160],
                                                      "sql": sql[:120]}})


# ==========================================================================
#  Migrations
# ==========================================================================

def _v0001_baseline(db: Database, conn) -> None:
    """
    Legacy shape, verbatim from the pre-upgrade `init_db()`.

    Re-creating these with IF NOT EXISTS is how a *fresh* install ends up with
    the exact same tables the old build produced; an *existing* database simply
    matches what is already there and nothing changes.
    """
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS vx_schema_version (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        note TEXT,
        applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        full_name TEXT,
        bio TEXT,
        avatar TEXT,
        role TEXT DEFAULT 'user',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER, receiver_id INTEGER, group_id INTEGER,
        content TEXT, file_path TEXT, file_type TEXT, file_name TEXT,
        reply_to_id INTEGER, edited_at DATETIME,
        seen INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, forwarded INTEGER DEFAULT 0,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, file_path TEXT, file_type TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME DEFAULT (datetime('now', '+24 hours'))
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, content TEXT, file_path TEXT, file_type TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS lan_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, game_name TEXT, ip_address TEXT, port INTEGER,
        description TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS post_likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, user_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(post_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS post_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, user_id INTEGER, content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_id INTEGER, following_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(follower_id, following_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS story_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_id INTEGER, user_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(story_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS post_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, user_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(post_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, actor_id INTEGER, type TEXT, target_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, is_read INTEGER DEFAULT 0
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, creator_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER, user_id INTEGER, role TEXT DEFAULT 'member',
        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(group_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS group_seen (
        group_id INTEGER, user_id INTEGER, last_seen_id INTEGER DEFAULT 0,
        UNIQUE(group_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE, user_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    # columns the old build added with try/except ALTERs
    _add_cols(db, conn, "messages", [
        ("reply_to_id", "INTEGER"), ("edited_at", "DATETIME"), ("seen", "INTEGER DEFAULT 0"),
        ("pinned", "INTEGER DEFAULT 0"), ("group_id", "INTEGER"), ("forwarded", "INTEGER DEFAULT 0"),
    ])


def _v0002_auth_hardening(db: Database, conn) -> None:
    """Expiring/revocable sessions, ban flags, presence, profile extras."""
    _add_cols(db, conn, "users", [
        ("status", "TEXT DEFAULT 'active'"),          # active | banned | suspended | deleted
        ("is_banned", "INTEGER DEFAULT 0"),
        ("ban_reason", "TEXT"),
        ("banned_at", "DATETIME"),
        ("must_change_password", "INTEGER DEFAULT 0"),
        ("last_seen_at", "DATETIME"),
        ("presence", "TEXT DEFAULT 'offline'"),        # online | away | offline | dnd
        ("accent", "TEXT"),                             # profile colour, keeps identity
        ("gender", "TEXT"),
        ("location", "TEXT"),
        ("website", "TEXT"),
        ("online_visible", "INTEGER DEFAULT 1"),
        ("updated_at", "DATETIME"),
        ("password_changed_at", "DATETIME"),
    ])
    _add_cols(db, conn, "sessions", [
        ("expires_at", "DATETIME"),
        ("revoked_at", "DATETIME"),
        ("revoked_reason", "TEXT"),
        ("last_used_at", "DATETIME"),
        ("device", "TEXT"),
        ("user_agent", "TEXT"),
        ("ip", "TEXT"),
        ("is_current", "INTEGER DEFAULT 0"),
    ])
    # Existing rows must not be orphaned: give every legacy session a real
    # deadline so behaviour is uniform after the upgrade.
    _backfill(db, conn, f"UPDATE sessions SET expires_at = {db.now_sql()} "
                        f"WHERE expires_at IS NULL")
    _backfill(db, conn, f"UPDATE sessions SET last_used_at = COALESCE(created_at, {db.now_sql()}) "
                        f"WHERE last_used_at IS NULL")
    _backfill(db, conn, "UPDATE users SET presence = 'offline' WHERE presence IS NULL")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        notify_like INTEGER DEFAULT 1,
        notify_comment INTEGER DEFAULT 1,
        notify_follow INTEGER DEFAULT 1,
        notify_message INTEGER DEFAULT 1,
        notify_friend_request INTEGER DEFAULT 1,
        notify_game_invite INTEGER DEFAULT 1,
        notify_server_status INTEGER DEFAULT 1,
        notify_room_activity INTEGER DEFAULT 1,
        notify_mention INTEGER DEFAULT 1,
        notify_group_invite INTEGER DEFAULT 1,
        nsfw_filter INTEGER DEFAULT 0,
        activity_public INTEGER DEFAULT 1,
        dm_from INTEGER DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _index(db, conn, "ix_sessions_token", "sessions", "token", unique=True)
    _index(db, conn, "ix_sessions_user_expiry", "sessions", "user_id, expires_at")
    _index(db, conn, "ix_users_username", "users", "username", unique=True)


def _v0003_social_graph(db: Database, conn) -> None:
    """Friendships layered on top of follows (follow is left untouched)."""
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS friendships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_a INTEGER NOT NULL, user_b INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'none',
        requested_by INTEGER,
        blocked_by INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME,
        UNIQUE(user_a, user_b)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS user_blocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, blocked_user_id INTEGER NOT NULL,
        reason TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, blocked_user_id)
    )""")
    _add_cols(db, conn, "posts", [
        ("visibility", "TEXT DEFAULT 'public'"),      # public | followers | friends | private
        ("hashtags", "TEXT"),
        ("edit_count", "INTEGER DEFAULT 0"),
        ("edited_at", "DATETIME"),
        ("is_pinned", "INTEGER DEFAULT 0"),
        ("like_count", "INTEGER DEFAULT 0"),
        ("comment_count", "INTEGER DEFAULT 0"),
        ("view_count", "INTEGER DEFAULT 0"),
        ("deleted_at", "DATETIME"),
    ])
    _add_cols(db, conn, "post_comments", [("parent_id", "INTEGER"), ("edited_at", "DATETIME")])
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS post_reactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(post_id, user_id, emoji)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS saved_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, post_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS mentions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, comment_id INTEGER, mentioned_user_id INTEGER NOT NULL,
        mentioned_by INTEGER NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _index(db, conn, "ix_posts_user_created", "posts", "user_id, id")
    _index(db, conn, "ix_posts_timestamp", "posts", "id")
    _index(db, conn, "ix_follows_following", "follows", "following_id, follower_id")
    _index(db, conn, "ix_follows_follower", "follows", "follower_id")
    _index(db, conn, "ix_likes_post", "post_likes", "post_id")
    _index(db, conn, "ix_comments_post", "post_comments", "post_id, id")
    _index(db, conn, "ix_mentions_user", "mentions", "mentioned_user_id, id")


def _v0004_stories_messaging(db: Database, conn) -> None:
    """Story lifecycle/privacy; message reactions, search, delivery state."""
    _add_cols(db, conn, "stories", [
        ("caption", "TEXT"),
        ("privacy", "TEXT DEFAULT 'everyone'"),        # everyone | followers | friends | private
        ("allowed_user_ids", "TEXT"),                  # csv, only for privacy='private'
        ("view_count", "INTEGER DEFAULT 0"),
        ("is_archived", "INTEGER DEFAULT 0"),
        ("deleted_at", "DATETIME"),
        ("bg", "TEXT"),
    ])
    _index(db, conn, "ix_stories_user_expires", "stories", "user_id, expires_at")
    _index(db, conn, "ix_stories_expires", "stories", "expires_at")

    _add_cols(db, conn, "messages", [
        ("deleted_for_everyone", "INTEGER DEFAULT 0"),
        ("deleted_by", "INTEGER"),
        ("reply_count", "INTEGER DEFAULT 0"),
        ("thread_root_id", "INTEGER"),
        ("search_text", "TEXT"),
        ("msg_type", "TEXT DEFAULT 'text'"),            # text|image|video|audio|file|system|voice
        ("duration_ms", "INTEGER"),
        ("waveform", "TEXT"),
        ("delivered_at", "DATETIME"),
        ("reply_to_name", "TEXT"),
        ("pinned_by", "INTEGER"),
        ("pinned_at", "DATETIME"),
        ("edited_by", "INTEGER"),
    ])
    # Keep denormalised search text populated for existing rows.
    _backfill(db, conn, "UPDATE messages SET search_text = content WHERE search_text IS NULL")
    _backfill(db, conn, "UPDATE messages SET msg_type = COALESCE(file_type, 'text') "
                        "WHERE file_path IS NOT NULL AND msg_type = 'text'")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS message_reactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, emoji TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(message_id, user_id, emoji)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS chat_threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_a INTEGER, user_b INTEGER, group_id INTEGER,
        last_message_id INTEGER, last_at DATETIME,
        unread_a INTEGER DEFAULT 0, unread_b INTEGER DEFAULT 0,
        UNIQUE(user_a, user_b)
    )""")
    _index(db, conn, "ix_messages_pair", "messages", "sender_id, receiver_id, id")
    _index(db, conn, "ix_messages_group", "messages", "group_id, id")
    _index(db, conn, "ix_messages_receiver_unseen", "messages", "receiver_id, seen, id")
    _index(db, conn, "ix_reactions_msg", "message_reactions", "message_id")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS message_deletes (
        message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        deleted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deleted_by INTEGER, PRIMARY KEY (message_id, user_id)
    )""")
    _index(db, conn, "ix_msgdel_user", "message_deletes", "user_id, message_id")


def _v0005_group_roles(db: Database, conn) -> None:
    """Explicit roles/permissions, bans, mutes, invites."""
    _add_cols(db, conn, "groups", [
        ("description", "TEXT"),
        ("avatar", "TEXT"),
        ("invite_code", "TEXT"),
        ("is_private", "INTEGER DEFAULT 0"),
        ("max_members", "INTEGER DEFAULT 200"),
        ("only_admins_post", "INTEGER DEFAULT 0"),
        ("slow_mode_seconds", "INTEGER DEFAULT 0"),
        ("updated_at", "DATETIME"),
        ("deleted_at", "DATETIME"),
    ])
    _add_cols(db, conn, "group_members", [
        ("muted_until", "DATETIME"),
        ("title", "TEXT"),
        ("invited_by", "INTEGER"),
        ("last_read_at", "DATETIME"),
    ])
    # Legacy rows only ever had owner/member; normalise to the new ladder.
    _backfill(db, conn, "UPDATE group_members SET role = 'owner' WHERE role IS NULL")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS group_bans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        banned_by INTEGER, reason TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS group_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL, code TEXT UNIQUE, created_by INTEGER,
        uses INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 0, expires_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _index(db, conn, "ix_gm_user", "group_members", "user_id, group_id")
    _index(db, conn, "ix_gm_group_role", "group_members", "group_id, role")
    _index(db, conn, "ix_groups_invite", "groups", "invite_code")


def _v0006_gaming(db: Database, conn) -> None:
    """
    The Gaming Hub.

    `lan_hosts` is *extended in place* rather than replaced, so every existing
    row survives and the legacy `/lan_hosts` endpoint keeps returning the same
    fields (plus new ones).
    """
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
        description TEXT, icon TEXT, cover TEXT,
        platforms TEXT, genre TEXT,
        is_featured INTEGER DEFAULT 0,
        discover_provider TEXT DEFAULT 'generic_tcp',
        default_port INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS user_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, game_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'owned',   -- owned | playing | favorite | wishlist
        hours_real INTEGER DEFAULT 0,
        achievements TEXT,
        last_played_at DATETIME,
        added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME,
        UNIQUE(user_id, game_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS gaming_profiles (
        user_id INTEGER PRIMARY KEY,
        gamertag TEXT UNIQUE,
        tagline TEXT,
        banner TEXT,
        favorite_game_id INTEGER,
        games_played INTEGER DEFAULT 0,
        servers_hosted INTEGER DEFAULT 0,
        servers_joined INTEGER DEFAULT 0,
        gaming_hours INTEGER DEFAULT 0,
        rooms_created INTEGER DEFAULT 0,
        invites_sent INTEGER DEFAULT 0,
        achievements TEXT,
        level INTEGER DEFAULT 1,
        xp INTEGER DEFAULT 0,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS gaming_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, event TEXT NOT NULL,
        game_id INTEGER, server_id INTEGER, room_id INTEGER,
        meta TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    # ---- lan_hosts -> real server records (additive only) -----------------
    _add_cols(db, conn, "lan_hosts", [
        ("game_id", "INTEGER"),
        ("name", "TEXT"),
        ("max_players", "INTEGER DEFAULT 16"),
        ("player_count", "INTEGER DEFAULT 0"),
        ("password_hash", "TEXT"),
        ("is_password_protected", "INTEGER DEFAULT 0"),
        ("status", "TEXT DEFAULT 'unknown'"),     # online|offline|full|starting|unknown
        ("region", "TEXT"),
        ("last_heartbeat", "DATETIME"),
        ("heartbeat_fails", "INTEGER DEFAULT 0"),
        ("visibility", "TEXT DEFAULT 'public'"),  # public|friends|private
        ("is_enabled", "INTEGER DEFAULT 1"),
        ("version", "TEXT"),
        ("map_name", "TEXT"),
        ("tags", "TEXT"),
        ("updated_at", "DATETIME"),
        ("last_status_change", "DATETIME"),
        ("manual_status", "INTEGER DEFAULT 0"),
        ("archived_at", "DATETIME"),
        ("latency_ms", "INTEGER"),
        ("players_max", "INTEGER"),
        ("motd", "TEXT"),
        ("server_version", "TEXT"),
        ("probe_detail", "TEXT"),
        ("probe_edited", "INTEGER DEFAULT 0"),
    ])
    _backfill(db, conn, "UPDATE lan_hosts SET status = 'unknown' WHERE status IS NULL")
    _backfill(db, conn, "UPDATE lan_hosts SET name = game_name WHERE name IS NULL OR name = ''")
    # port may have been stored as text by the old insert path
    _backfill(db, conn, "UPDATE lan_hosts SET port = NULL WHERE port IS NOT NULL AND port = 0")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS server_players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER NOT NULL, user_id INTEGER,
        player_name TEXT NOT NULL, joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        left_at DATETIME, UNIQUE(server_id, player_name)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS server_follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(server_id, user_id)
    )""")

    # ---- social lobbies, deliberately distinct from a game server ---------
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS game_rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, game_id INTEGER, host_id INTEGER NOT NULL,
        server_id INTEGER,
        description TEXT,
        max_players INTEGER DEFAULT 8,
        status TEXT DEFAULT 'open',          -- open | starting | ingame | full | closed
        visibility TEXT DEFAULT 'public',    -- public | friends | private | invite
        password_hash TEXT,
        region TEXT,
        voice_enabled INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME,
        started_at DATETIME, closed_at DATETIME, expires_at DATETIME,
        last_activity_at DATETIME
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS game_room_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        role TEXT DEFAULT 'member',           -- host | cohost | member
        ready INTEGER DEFAULT 0,
        mic_muted INTEGER DEFAULT 0,
        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        left_at DATETIME,
        UNIQUE(room_id, user_id)
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS game_room_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL, from_user_id INTEGER NOT NULL,
        to_user_id INTEGER NOT NULL,
        state TEXT DEFAULT 'pending',         -- pending | accepted | declined | expired
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, responded_at DATETIME,
        UNIQUE(room_id, to_user_id)
    )""")
    _index(db, conn, "ix_games_slug", "games", "slug", unique=True)
    _index(db, conn, "ix_ug_user", "user_games", "user_id, status")
    _index(db, conn, "ix_ug_game", "user_games", "game_id")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS server_status_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER NOT NULL, status TEXT NOT NULL,
        changed_by INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _index(db, conn, "ix_ssl_server", "server_status_log", "server_id, id")
    _index(db, conn, "ix_lan_status_hb", "lan_hosts", "status, last_heartbeat")
    _index(db, conn, "ix_lan_user", "lan_hosts", "user_id, id")
    _index(db, conn, "ix_lan_game", "lan_hosts", "game_id")
    _index(db, conn, "ix_rooms_status_act", "game_rooms", "status, last_activity_at")
    _index(db, conn, "ix_rooms_host", "game_rooms", "host_id, id")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS room_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        content TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _index(db, conn, "ix_rm_room", "room_messages", "room_id, id")
    _index(db, conn, "ix_grm_user", "game_room_members", "user_id, room_id")
    _index(db, conn, "ix_grm_room", "game_room_members", "room_id, left_at")
    _index(db, conn, "ix_activity_user", "gaming_activity", "user_id, id")


def _v0007_notifications_reports(db: Database, conn) -> None:
    """Richer notifications + reporting/moderation queue."""
    _add_cols(db, conn, "notifications", [
        ("body", "TEXT"),
        ("icon", "TEXT"),
        ("target_type", "TEXT"),
        ("room_id", "INTEGER"),
        ("server_id", "INTEGER"),
        ("priority", "TEXT DEFAULT 'normal'"),
        ("read_at", "DATETIME"),
        ("delivered_at", "DATETIME"),
        ("data", "TEXT"),
    ])
    _index(db, conn, "ix_notif_user_unread", "notifications", "user_id, is_read, id")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS notifications_seen (
        user_id INTEGER PRIMARY KEY, last_read_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER NOT NULL,
        target_type TEXT NOT NULL,     -- user|post|comment|message|group|room|server|story
        target_id INTEGER,
        reason TEXT NOT NULL,
        details TEXT,
        status TEXT DEFAULT 'open',    -- open|reviewing|actioned|dismissed
        handled_by INTEGER, handled_at DATETIME, handle_action TEXT, handle_note TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS moderation_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL, action TEXT NOT NULL,
        target_type TEXT, target_id INTEGER, user_id INTEGER,
        note TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    _index(db, conn, "ix_reports_status", "reports", "status, id")
    _index(db, conn, "ix_reports_target", "reports", "target_type, target_id")
    _index(db, conn, "ix_mod_actions_user", "moderation_actions", "user_id, id")

    # search_index: one place for the unified search to hit cheaply
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS search_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, ref_id INTEGER NOT NULL,
        title TEXT, subtitle TEXT, body TEXT,
        owner_id INTEGER, is_public INTEGER DEFAULT 1,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(kind, ref_id)
    )""")
    _index(db, conn, "ix_search_kind", "search_index", "kind, ref_id")


def _v0008_seed_games(db: Database, conn) -> None:
    """
    Seed a small offline catalog so the Gaming Hub is usable on a fresh DB.
    These are *declarative* rows only: ports are the well-known defaults used
    by the generic probe, never a claim about a specific server.
    """
    if not _table_exists(db, conn, "games"):
        return
    seed = [
        ("minecraft", "Minecraft", "سندباکس ساخت‌وساز با پشتیبانی از سرور LAN", "cube",
         "sandbox", "pc,android,ios,console", 25565, "minecraft"),
        ("cs2", "Counter-Strike 2", "تیراندازی رقابتی ۵ به ۵", "scope",
         "fps", "pc", 27015, "generic_tcp"),
        ("valorant", "Valorant", "تیراندازی قهرمان‌محور", "cross",
         "fps", "pc", 7777, "generic_tcp"),
        ("minecraft-bedrock", "Minecraft Bedrock", "نسخه موبایل/کنسول ماینکرفت", "cube",
         "sandbox", "android,ios,console,pc", 19132, "minecraft_bedrock"),
        ("raft", "Raft", "بقا روی دریا با دوستان", "wave",
         "survival", "pc", 27015, "generic_tcp"),
        ("terraria", "Terraria", "ماجراجویی دولایه‌ای", "pick",
         "sandbox", "pc,mobile", 27015, "generic_tcp"),
        ("project-zomboid", "Project Zomboid", "بقای زامبی‌محور با عمق شبیه‌سازی", "skull",
         "survival", "pc", 16261, "generic_tcp"),
        (" Among-Us", "Among Us", "فریب در فضا", "crew",
         "party", "pc,android,ios", 22000, "generic_tcp"),
        ("gta-five-m", "GTA V (FiveM)", "مالتی‌پلیر سفارشی جی‌تی‌ای", "car",
         "roleplay", "pc", 30120, "generic_tcp"),
        ("rust", "Rust", "بقای آنلاین بی‌رحم", "can",
         "survival", "pc", 28015, "generic_tcp"),
        ("minecraft-pi", "Minecraft Pi", "ماینکرفت روی رزبری پای", "pi",
         "sandbox", "linux", 4711, "minecraft"),
        ("factorio", "Factorio", "کارخانه‌سازی خودکار", "gear",
         "strategy", "pc", 34197, "generic_tcp"),
    ]
    for slug, name, desc, icon, genre, platforms, port, provider in seed:
        slug = slug.strip()
        if db.query_one(conn, "SELECT id FROM games WHERE slug = ?", (slug,)):
            continue
        db.execute(conn,
                   """INSERT INTO games (slug, name, description, icon, platforms, genre,
                                         default_port, discover_provider)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                   (slug, name, desc, icon, platforms, genre, port, provider)).close()


def _v0010_comment_replies(db: Database, conn) -> None:
    """
    Reply counters on comments.

    The thread view needs "3 replies" per top-level comment; without a stored
    counter every feed render would run one COUNT per comment (the N+1 pattern
    docs/AUDIT.md §6 calls out for the legacy build).
    """
    _add_cols(db, conn, "post_comments", [("reply_count", "INTEGER DEFAULT 0")])
    _backfill(db, conn, """UPDATE post_comments
                           SET reply_count = (SELECT COUNT(*) FROM post_comments r
                                              WHERE r.parent_id = post_comments.id)
                           WHERE parent_id IS NULL""")
    _index(db, conn, "ix_pc_parent", "post_comments", "parent_id, id")


def _v0011_vl_identity(db: Database, conn) -> None:
    """
    One public identity per account: ``users.vl_id``.

    Explore and Advanced are two clients of the same account (docs/ROADMAP.md),
    and games/clips/groups/reputation all need to point at something that is not a
    serial row id. Random and immutable — see `backend/vlid.py` for why an alias
    built from `users.id` would have been a scraping cursor.

    Backfills existing rows so an upgraded install keeps its users, then adds the
    unique index that makes the value actually one-per-account.
    """
    from . import vlid

    _add_cols(db, conn, "users", [("vl_id", "TEXT")])
    if not _table_exists(db, conn, "users"):
        return
    # an empty string would collide with every other empty string under a unique
    # index, and old code paths could have written one
    _backfill(db, conn, "UPDATE users SET vl_id = NULL WHERE vl_id = ''")
    pending = db.query(conn, "SELECT id FROM users WHERE vl_id IS NULL ORDER BY id")
    for row in pending:
        try:
            vlid.assign(db, conn, int(row["id"]))
        except Exception as exc:                                        # pragma: no cover
            log.warning("vl_id_backfill_failed",
                        extra={"ctx": {"user_id": row["id"], "err": str(exc)[:120]}})
    # A single duplicated value would make the unique index fail, and since the
    # runner is transactional that means the install never boots again. Only
    # reachable if something wrote the column by hand (it did not exist before this
    # step) — but the repair costs two queries and the failure mode is total, so the
    # oldest holder of a shared id keeps it and the rest get fresh ones.
    shared = db.query(conn, """SELECT vl_id FROM users WHERE vl_id IS NOT NULL
                               GROUP BY vl_id HAVING COUNT(*) > 1 ORDER BY vl_id""")
    for dup in shared:
        victims = db.query(conn, "SELECT id FROM users WHERE vl_id = ? ORDER BY id",
                           (dup["vl_id"],))
        for v in list(victims)[1:]:
            vlid.assign(db, conn, int(v["id"]), force=True)
            log.warning("vl_id_duplicate_reassigned",
                        extra={"ctx": {"user_id": v["id"], "was": dup["vl_id"]}})
    _index(db, conn, "ux_users_vl_id", "users", "vl_id", unique=True)


def _v0009_perf_indexes(db: Database, conn) -> None:
    """Final index pass — covers every field the spec calls out by name."""
    for name, table, cols, unique in [
        ("ix_users_created", "users", "id", False),
        ("ix_users_role", "users", "role, id", False),
        ("ix_users_status", "users", "status, id", False),
        ("ix_ug_status", "user_games", "status, user_id", False),
        ("ix_gp_gamertag", "gaming_profiles", "gamertag", False),
        ("ix_gi_room_state", "game_room_invites", "to_user_id, state", False),
        ("ix_gi_room", "game_room_invites", "room_id, state", False),
        ("ix_sp_server", "server_players", "server_id, left_at", False),
        ("ix_sf_user", "server_follows", "user_id, server_id", False),
        ("ix_n_type", "notifications", "type, id", False),
        ("ix_pl_user", "post_likes", "user_id, post_id", False),
        ("ix_pv_user", "post_views", "user_id, post_id", False),
        ("ix_sv_user", "story_views", "user_id, story_id", False),
        ("ix_fr_state", "friendships", "state, user_a, user_b", False),
        ("ix_msg_search", "messages", "search_text", False),
        ("ix_msg_sender", "messages", "sender_id, id", False),
        ("ix_block_pair", "user_blocks", "user_id, blocked_user_id", False),
    ]:
        if _table_exists(db, conn, table):
            _index(db, conn, name, table, cols, unique=unique)
    # denormalised counters need to agree with their source tables once
    for table, col, sub in [
        ("posts", "like_count",
         "UPDATE posts SET like_count = (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id = posts.id)"),
        ("posts", "comment_count",
         "UPDATE posts SET comment_count = (SELECT COUNT(*) FROM post_comments pc WHERE pc.post_id = posts.id)"),
        ("posts", "view_count",
         "UPDATE posts SET view_count = (SELECT COUNT(*) FROM post_views pv WHERE pv.post_id = posts.id)"),
    ]:
        if _table_exists(db, conn, table) and _has_col(db, conn, table, col):
            _backfill(db, conn, sub)


MIGRATIONS: list[Migration] = [
    Migration(1, "baseline_legacy_schema", _v0001_baseline,
              "Legacy 15 tables recreated verbatim so fresh and existing DBs match."),
    Migration(2, "auth_hardening", _v0002_auth_hardening,
              "Session expiry/revocation, ban flags, presence, per-user notification settings."),
    Migration(3, "social_graph", _v0003_social_graph,
              "Friendships/blocks, post visibility+counters, reactions, saves, mentions."),
    Migration(4, "stories_messaging", _v0004_stories_messaging,
              "Story privacy/expiry, message lifecycle columns, reactions, thread hints."),
    Migration(5, "group_roles", _v0005_group_roles,
              "Group avatar/description/invite, mute+ban, per-group posting policy."),
    Migration(6, "gaming_hub", _v0006_gaming,
              "games, user_games, gaming_profiles/activity, lan_hosts extended, game_rooms."),
    Migration(7, "notifications_reports", _v0007_notifications_reports,
              "Rich notifications, reports + moderation queue, search_index."),
    Migration(8, "seed_game_catalog", _v0008_seed_games,
              "Offline game catalog seed (no third-party API)."),
    Migration(10, "comment_reply_counts", _v0010_comment_replies,
              "denormalised reply_count on top-level comments + parent index"),
    Migration(11, "vl_identity", _v0011_vl_identity,
              "users.vl_id — one stable, non-sequential public id, backfilled"),
    Migration(9, "perf_indexes", _v0009_perf_indexes,
              "Indexes for every hot query field + counter backfill."),
]


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
@dataclass
class MigrationReport:
    engine: str
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    current_version: int = 0
    legacy_detected: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _ensure_version_table(db: Database, conn) -> None:
    _create(db, conn, """
    CREATE TABLE IF NOT EXISTS vx_schema_version (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        note TEXT,
        applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")


def _applied_versions(db: Database, conn) -> set[int]:
    return {r["version"] for r in db.query(conn, f"SELECT version FROM {VERSION_TABLE}")}


def _stamp(db: Database, conn, version: int, name: str, note: str = "") -> None:
    # Portable: an existence check beats dialect-specific upsert syntax here.
    exists = db.query_one(conn, f"SELECT version FROM {VERSION_TABLE} WHERE version = ?", (version,))
    if exists:
        db.execute(conn, f"UPDATE {VERSION_TABLE} SET name = ?, note = ? WHERE version = ?",
                   (name, note, version)).close()
    else:
        db.execute(conn, f"INSERT INTO {VERSION_TABLE} (version, name, note) VALUES (?, ?, ?)",
                   (version, name, note)).close()


def is_legacy_database(db: Database, conn) -> bool:
    """True when the old build's tables exist but versioning does not."""
    return (db.has_table(conn, "users")
            and not db.has_table(conn, VERSION_TABLE))


def run_migrations(db: Database | None = None, *, force: bool = False) -> MigrationReport:
    """
    Bring the schema up to date. Safe to call at every boot — it is a no-op
    once the database is current.
    """
    db = db or Database()
    report = MigrationReport(engine=db.engine)
    conn = db.connect()
    try:
        with db.tx(conn):
            legacy = db.has_table(conn, "users") and not db.has_table(conn, VERSION_TABLE)
            _ensure_version_table(db, conn)
            if legacy:
                report.legacy_detected = True
                log.info("legacy_schema_detected",
                         extra={"ctx": {"note": "stamping baseline; existing rows untouched"}})

            applied = _applied_versions(db, conn)
            for mig in sorted(MIGRATIONS, key=lambda m: m.version):
                if mig.version in applied and not force:
                    # Baseline stamping: a legacy DB gets v1 recorded without
                    # re-running DDL that could conflict with real data.
                    if mig.version == 1 and legacy:
                        _stamp(db, conn, 1, mig.name, mig.note)
                        report.skipped.append(f"{mig.version:04d}:{mig.name} (stamped)")
                    else:
                        report.skipped.append(f"{mig.version:04d}:{mig.name}")
                    continue
                try:
                    mig.apply(db, conn)
                    _stamp(db, conn, mig.version, mig.name, mig.note)
                    report.applied.append(f"{mig.version:04d}:{mig.name}")
                    log.info("migration_applied", extra={"ctx": {
                        "version": mig.version, "name": mig.name, "engine": db.engine}})
                except Exception as exc:
                    report.errors.append(f"{mig.version:04d}:{mig.name}: {exc}")
                    log.error("migration_failed", extra={"ctx": {
                        "version": mig.version, "name": mig.name, "err": str(exc)[:300]}})
                    raise

            # `migrations/*.sql` on disk, applied in numeric order then stamped.
            _run_disk_migrations(db, conn, report)
            report.current_version = int(db.scalar(
                conn, f"SELECT COALESCE(MAX(version), 0) FROM {VERSION_TABLE}"))
    finally:
        db.close(conn)
    return report


def _run_disk_migrations(db: Database, conn, report: MigrationReport) -> None:
    if not DISK_MIGRATIONS_DIR.exists():
        return
    applied = _applied_versions(db, conn)
    max_v = max(applied) if applied else 0
    for path in sorted(DISK_MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"(\d+)_", path.name)
        if not m:
            continue
        version = int(m.group(1))
        if version in applied or version <= max_v:
            report.skipped.append(f"{version:04d}:{path.stem} (disk)")
            continue
        sql = path.read_text(encoding="utf-8")
        # A disk file may carry `-- @engine: postgres` / `-- @engine: sqlite`
        wanted = re.search(r"--\s*@engine:\s*(\w+)", sql)
        if wanted and wanted.group(1).lower() not in (db.engine, "any"):
            report.skipped.append(f"{version:04d}:{path.stem} (engine)")
            continue
        with db.tx(conn):
            for stmt in split_sql(sql):
                if stmt.strip().startswith("--"):
                    continue
                db.execute(conn, translate_ddl(stmt, db.engine)).close()
            _stamp(db, conn, version, path.stem, "from migrations/")
            report.applied.append(f"{version:04d}:{path.stem} (disk)")
            log.info("disk_migration_applied", extra={"ctx": {"file": path.name}})


def status(db: Database | None = None) -> list[Row]:
    db = db or Database()
    conn = db.connect()
    try:
        if not db.has_table(conn, VERSION_TABLE):
            return []
        return db.query(conn, f"SELECT * FROM {VERSION_TABLE} ORDER BY version")
    finally:
        db.close(conn)


def pending_count(db: Database | None = None) -> int:
    db = db or Database()
    conn = db.connect()
    try:
        _ensure_version_table(db, conn)
        applied = _applied_versions(db, conn)
        return sum(1 for m in MIGRATIONS if m.version not in applied)
    finally:
        db.close(conn)


def expected_version() -> int:
    return max(m.version for m in MIGRATIONS)
