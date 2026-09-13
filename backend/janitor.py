"""
Background housekeeping.

One daemon thread, guarded so a multi-worker gunicorn deployment runs it at
most once per host (`VOLEXTURN_JANITOR_WORKER=1` elects a single worker). It
owns everything the spec asks to happen "automatically":

  * expired stories deleted, their files swept
  * LAN servers transitioning online -> offline on heartbeat timeout
  * stale server rows archived (never deleted outright — spec §16)
  * abandoned game rooms closed
  * expired/revoked sessions purged
  * presence reconciliation for sockets that vanished uncleanly

Every tick is wrapped so a single bad query cannot kill the thread, because a
dead janitor means the platform quietly rots.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .config import get_config
from .db import Database
from .log import get_logger
from .storage import StorageProvider

log = get_logger("janitor")

_STATE: dict[str, Any] = {"running": False, "ticks": 0, "last_tick": 0.0,
                          "last_report": {}, "errors": 0, "started_at": 0.0,
                          "interval": 0, "thread": None, "elected": False}
_START_LOCK = threading.Lock()


def _should_run() -> bool:
    """
    Worker election. gunicorn forks N workers; each would otherwise probe
    servers N times. With >1 worker we honour an explicit opt-in.
    """
    workers = int(os.environ.get("VOLEXTURN_JANITOR_WORKERS", "1") or 1)
    if workers <= 1:
        return True
    return os.environ.get("VOLEXTURN_JANITOR_WORKER", "0") == "1"


def tick(app, db: Database, storage: StorageProvider) -> dict[str, int]:
    """One maintenance pass. Returns counts for logging/health output."""
    from . import presence
    report: dict[str, int] = {}
    conn = db.connect()
    try:
        with db.tx(conn):
            # ---- expired stories (+ their media files) ---------------
            dead = db.query(conn, """
                SELECT id, file_path FROM stories
                WHERE expires_at IS NOT NULL AND expires_at < %s""" % _now_expr(db))
            ids = [r["id"] for r in dead]
            for path in (r["file_path"] for r in dead if r.get("file_path")):
                try:
                    storage.delete("stories", str(path))
                except Exception:
                    pass
            if ids:
                marks = ", ".join("?" for _ in ids)
                db.execute(conn, f"DELETE FROM story_views WHERE story_id IN ({marks})", ids).close()
                db.execute(conn, f"DELETE FROM stories WHERE id IN ({marks})", ids).close()
            report["stories_expired"] = len(ids)

            # ---- heartbeat expiry: online -> offline (never deleted) --
            cfg = get_config()
            if db.has_table(conn, "lan_hosts"):
                stale = f"""status = 'online' AND is_enabled = 1
                           AND {db.is_expired_sql('last_heartbeat', cfg.heartbeat_timeout_seconds)}"""
                # the ids first: the transition log is the only place that says
                # *when* a server went quiet, and the sweep used to flip the
                # status without writing it, so the timeline had a hole in it
                dying = [int(r["id"]) for r in db.query(
                    conn, f"SELECT id FROM lan_hosts WHERE {stale}")]
                cur = db.execute(conn, f"""
                    UPDATE lan_hosts
                       SET status = 'offline',
                           last_status_change = {db.now_sql()},
                           heartbeat_fails = COALESCE(heartbeat_fails, 0) + 1
                     WHERE {stale}""")
                report["servers_timed_out"] = cur.rowcount
                cur.close()
                if dying and db.has_table(conn, "server_status_log"):
                    marks = ", ".join("?" for _ in dying)
                    db.execute(conn, f"""INSERT INTO server_status_log
                                             (server_id, status, changed_by, created_at)
                                         SELECT id, 'offline', NULL, {db.now_sql()}
                                           FROM lan_hosts WHERE id IN ({marks})""", dying).close()
                # stale-but-offline rows are archived, not removed
                cur = db.execute(conn, f"""
                    UPDATE lan_hosts SET archived_at = {db.now_sql()}
                     WHERE status = 'offline' AND archived_at IS NULL
                       AND {db.is_expired_sql('last_heartbeat', cfg.server_offline_grace_seconds)}""")
                report["servers_archived"] = cur.rowcount
                cur.close()

                # ---- discovery sweep (policy-gated inside discovery) ---
                if cfg.discovery_enabled:
                    report["servers_probed"] = _discovery_sweep(app, db, conn)

            # ---- stale game rooms ------------------------------------
            if db.has_table(conn, "game_rooms"):
                cur = db.execute(conn, f"""
                    UPDATE game_rooms SET status = 'closed', closed_at = {db.now_sql()}
                     WHERE status IN ('open', 'starting', 'ingame')
                       AND {db.is_expired_sql('last_activity_at', cfg.room_stale_minutes * 60)}""")
                report["rooms_closed"] = cur.rowcount
                cur.close()
                cur = db.execute(conn, f"""
                    UPDATE game_rooms SET status = 'closed', closed_at = {db.now_sql()}
                     WHERE status IN ('open','starting') AND expires_at IS NOT NULL
                       AND expires_at < {db.now_sql()}""")
                report["rooms_expired"] = cur.rowcount
                cur.close()
                cur = db.execute(conn, """
                    UPDATE game_room_invites SET state = 'expired'
                     WHERE state = 'pending' AND created_at < %s"""
                    % _minus_hours(db, 48))
                report["invites_expired"] = cur.rowcount
                cur.close()

            # ---- sessions --------------------------------------------
            from .auth import purge_expired_sessions
            report["sessions_purged"] = purge_expired_sessions(db, conn)

            # ---- presence reconciliation -----------------------------
            gone = presence.prune_stale(max(60, cfg.socket_ping_interval * 3))
            for uid in gone:
                db.execute(conn, f"UPDATE users SET presence = 'offline', last_seen_at = {db.now_sql()} "
                                 f"WHERE id = ?", (uid,)).close()
            report["presence_reconciled"] = len(gone)
            if gone:
                try:
                    from .app import socketio as sio
                    sio.emit("user_status", {"online": sorted(presence.online_ids())})
                except Exception:
                    pass
    finally:
        db.close(conn)
    return report


def _now_expr(db: Database) -> str:
    return db.now_sql()


def _minus_hours(db: Database, hours: int) -> str:
    return db.hours_ahead_sql(-hours)


def _discovery_sweep(app, db: Database, conn) -> int:
    """
    Probe at most `discovery_max_targets_per_tick` servers and reconcile status.

    Only rows the owner enabled are touched, and every target passes the
    allowlist policy inside `discovery.probe_server_row` — refused rows are
    counted as "not attempted" rather than reported offline.
    """
    from .discovery import probe_many
    rows = db.query(conn, f"""
        SELECT h.id, h.user_id, h.ip_address, h.port, h.password_hash,
               COALESCE(g.discover_provider, 'generic_tcp') AS discover_provider,
               h.last_heartbeat, h.status
        FROM lan_hosts h
        LEFT JOIN games g ON g.id = h.game_id
        WHERE h.is_enabled = 1 AND h.archived_at IS NULL AND h.ip_address IS NOT NULL
          AND {db.is_expired_sql('h.last_heartbeat', get_config().discovery_interval_seconds)}
        ORDER BY h.id ASC""")
    if not rows:
        return 0
    results = probe_many([dict(r) for r in rows])
    probed = 0
    for row, res in zip(rows, results):
        if not res.attempted:
            continue
        probed += 1
        db.execute(conn, """
            UPDATE lan_hosts
               SET status = ?, last_heartbeat = ?, latency_ms = ?,
                   player_count = ?, players_max = ?, motd = ?, server_version = ?,
                   heartbeat_fails = CASE WHEN ? = 'offline' THEN COALESCE(heartbeat_fails,0) + 1 ELSE 0 END,
                   last_status_change = CASE WHEN status = ? THEN last_status_change ELSE ? END,
                   updated_at = ?
             WHERE id = ?""", (
            res.status, db.now_sql(), res.latency_ms, res.players_online, res.players_max,
            (res.motd or "")[:200] or None, (res.version or "")[:64] or None,
            res.status, res.status, db.now_sql(), db.now_sql(), row["id"])).close()
        if row.get("status") and row["status"] != res.status:
            _notify_status_change(app, db, conn, row, res.status)
    return probed


def _notify_status_change(app, db: Database, conn, row: dict, new_status: str) -> None:
    """Tell followers of a server that it flipped online/offline (spec §18)."""
    from .notify import notify
    from .app import socketio as sio
    targets: set[int] = {int(row["user_id"])} if row.get("user_id") else set()
    follows = db.query(conn, "SELECT user_id FROM server_follows WHERE server_id = ?", (row["id"],))
    targets |= {int(f["user_id"]) for f in follows if f.get("user_id")}
    ntype = "server_online" if new_status == "online" else "server_offline"
    for uid in list(targets)[:200]:
        notify(db, conn, user_id=uid, actor_id=row.get("user_id"), ntype=ntype,
               target_id=row["id"], server_id=row["id"],
               body=f"{'آنلاین' if new_status == 'online' else 'آفلاین'} شد",
               socketio=sio)


def _loop(app, db: Database, storage: StorageProvider, interval: int) -> None:
    _STATE.update(running=True, started_at=time.time(), interval=interval)
    log.info("janitor_started", extra={"ctx": {"interval": interval}})
    while True:
        try:
            with app.app_context():
                report = tick(app, db, storage)
            _STATE["ticks"] += 1
            _STATE["last_tick"] = time.time()
            _STATE["last_report"] = report
            if any(v for v in report.values()):
                log.info("janitor_tick", extra={"ctx": dict(report)})
        except Exception as exc:
            _STATE["errors"] += 1
            log.warning("janitor_tick_failed", extra={"ctx": {"err": str(exc)[:240]}})
        time.sleep(max(10, interval))


def start_janitor(app, db: Database, storage: StorageProvider) -> bool:
    """Spawn the daemon thread once per process. Returns whether it started."""
    with _START_LOCK:
        if _STATE["thread"] is not None:
            return False
        if not _should_run():
            log.info("janitor_skipped", extra={"ctx": {"reason": "not elected worker"}})
            _STATE["elected"] = False
            return False
        interval = get_config().janitor_interval_seconds
        thread = threading.Thread(target=_loop, args=(app, db, storage, interval),
                                   name="volexturn-janitor", daemon=True)
        _STATE["thread"] = thread
        _STATE["elected"] = True
        thread.start()
        return True


def janitor_status() -> dict[str, Any]:
    return {
        "running": bool(_STATE["running"]),
        "elected": bool(_STATE["elected"]),
        "ticks": _STATE["ticks"],
        "errors": _STATE["errors"],
        "interval_seconds": _STATE["interval"],
        "seconds_since_last_tick": (round(time.time() - _STATE["last_tick"], 1)
                                    if _STATE["last_tick"] else None),
        "last_report": _STATE["last_report"],
    }


def run_once_for_tests(app, db: Database, storage: StorageProvider) -> dict[str, int]:
    """Deterministic single pass for the test-suite (no thread)."""
    with app.app_context():
        return tick(app, db, storage)
