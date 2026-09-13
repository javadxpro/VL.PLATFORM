"""
Presence tracking.

Pre-upgrade, presence came from `join {user_id: <anything>}` — a client could
claim to be anyone (docs/AUDIT.md §7 S5). Here, presence is derived from the
**authenticated socket** and counted per connection, so one user in three tabs
is one user online, and a disconnect in one tab does not mark them offline.

Kept in-process on purpose: a Redis presence set would make the simplest
self-hosted install depend on a second service. Multi-worker deployments get
per-worker presence, which is documented in docs/security.md as a known limit.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from .log import get_logger

log = get_logger("presence")

_LOCK = threading.Lock()
#: user_id -> set of socket ids currently connected for that user
_CONNS: dict[int, set[str]] = defaultdict(set)
#: user_id -> monotonic ts of their most recent activity
_LAST_ACTIVE: dict[int, float] = {}
#: user_id -> explicit status choice ('online' | 'away' | 'dnd' | 'offline')
_INTENT: dict[int, str] = {}


def attach(user_id: int, sid: str) -> bool:
    """Register a connection. Returns True when the user just came online."""
    uid = int(user_id)
    with _LOCK:
        was = bool(_CONNS.get(uid))
        _CONNS[uid].add(sid)
        _LAST_ACTIVE[uid] = time.monotonic()
    return not was


def detach(user_id: int, sid: str) -> bool:
    """Drop a connection. Returns True when the user just went fully offline."""
    uid = int(user_id)
    with _LOCK:
        conns = _CONNS.get(uid)
        if not conns:
            return False
        conns.discard(sid)
        if conns:
            return False
        _CONNS.pop(uid, None)
        _INTENT.pop(uid, None)
        _LAST_ACTIVE.pop(uid, None)
    return True


def touch(user_id: int) -> None:
    with _LOCK:
        _LAST_ACTIVE[int(user_id)] = time.monotonic()


def set_intent(user_id: int, status: str | None) -> None:
    status = (status or "").strip().lower()
    if status in {"online", "away", "dnd", "invisible", "offline"}:
        with _LOCK:
            _INTENT[int(user_id)] = status
    elif not status:
        with _LOCK:
            _INTENT.pop(int(user_id), None)


def is_online(user_id: int) -> bool:
    with _LOCK:
        return bool(_CONNS.get(int(user_id)))


def online_count() -> int:
    with _LOCK:
        return len(_CONNS)


def online_ids() -> set[int]:
    with _LOCK:
        return set(_CONNS.keys())


def status_for(user_id: int) -> str:
    if is_online(user_id):
        intent = _INTENT.get(int(user_id))
        return intent or "online"
    return "offline"


def sids_for(user_id: int) -> list[str]:
    with _LOCK:
        return list(_CONNS.get(int(user_id), ()))


def connections_for(user_ids: list[int]) -> dict[int, list[str]]:
    with _LOCK:
        return {int(u): list(_CONNS.get(int(u), ())) for u in user_ids}


def stamp(row: dict | None, user_id: int, *, field: str = "presence") -> dict:
    """Overlay live presence on a user dict for API responses."""
    if row is None:
        return {}
    out = dict(row)
    out[field] = status_for(user_id)
    out["is_online"] = out[field] != "offline"
    return out


def stamp_many(rows: list[dict], *, id_key: str = "id") -> list[dict]:
    """Bulk overlay — avoids the per-row helper-call pattern in the old code."""
    with _LOCK:
        online = {u for u, conns in _CONNS.items() if conns}
        intents = dict(_INTENT)
    out = []
    for row in rows:
        row = dict(row)
        uid = int(row.get(id_key) or 0)
        state = (intents.get(uid) or "online") if uid in online else "offline"
        row["is_online"] = state != "offline" and state != "invisible"
        row["presence"] = state
        out.append(row)
    return out


def prune_stale(seconds: int = 90) -> list[int]:
    """
    Forget users whose socket vanished without a clean disconnect (proxy
    restarts do this). Returns ids that went offline, for DB reconciliation.
    """
    cutoff = time.monotonic() - seconds
    gone: list[int] = []
    with _LOCK:
        for uid, ts in list(_LAST_ACTIVE.items()):
            if ts < cutoff:
                _LAST_ACTIVE.pop(uid, None)
                _CONNS.pop(uid, None)
                _INTENT.pop(uid, None)
                gone.append(uid)
    return gone


def snapshot() -> dict:
    with _LOCK:
        return {
            "online_users": len(_CONNS),
            "connections": sum(len(v) for v in _CONNS.values()),
        }


def reset() -> None:
    with _LOCK:
        _CONNS.clear()
        _LAST_ACTIVE.clear()
        _INTENT.clear()
