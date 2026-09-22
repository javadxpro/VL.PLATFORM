"""
VL ID — the one public identity a VL account carries.

Why not `VL-<user_id>`? `users.id` is serial, so an alias built from it turns
"list every VL user" into a loop of increments — an id that is *meant* to be
posted in a profile, a clip caption or a server listing must not double as a
scraping cursor. Random, and from an alphabet without the shapes people confuse
(Crockford base32: no I, L, O, U), so it survives being read aloud or typed from
a phone screen.

Format: ``VL-XXXX-XXXX`` — 8 symbols ≈ 40 bits, ~1.1e12 combinations, which is
comfortable well past any install this project will host before the collision
retry in `assign()` starts mattering.

Immutable on purpose: no route writes it. Later layers (friends, achievements,
reputation, clips, server reputation) key off it, and an identity that can be
moved between accounts would let history — and blame — be laundered. A username
can change; this cannot.

The unique index on `users.vl_id` (migration 0011) is the real guarantee;
`assign()` only pre-checks so the common path does not raise.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

PREFIX = "VL-"
#: Crockford base32 — 0/O, 1/I/L and U are left out on purpose.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
GROUP_LEN = 4
GROUPS = 2

#: what a canonical id looks like on the wire
FORMAT_RE = re.compile(r"^VL-[0-9A-Z]{4}-[0-9A-Z]{4}$")

#: how many candidate ids to try before giving up on a collision (practically
#: unreachable at this entropy; the loop exists so a duplicate is retried rather
#: than surfacing as a registration failure)
_MAX_TRIES = 40


def new() -> str:
    """A fresh, unassigned VL ID."""
    chars = [secrets.choice(ALPHABET) for _ in range(GROUP_LEN * GROUPS)]
    return PREFIX + "-".join("".join(chars[i:i + GROUP_LEN]) for i in range(0, len(chars), GROUP_LEN))


def normalize(value: Any) -> str | None:
    """
    Canonical form of a user-supplied id, or None if it is not one.

    Accepts the shapes people actually type: `vl-7qk4-m2x9`, `VL 7QK4 M2X9`,
    `7qkm2x9m` without the prefix, lowercase, stray hyphens/underscores.
    """
    if not value:
        return None
    raw = str(value).strip().upper()
    body = re.sub(r"[^0-9A-Z]", "", raw)
    if body.startswith(PREFIX.replace("-", "")):
        body = body[len(PREFIX.replace("-", "")):]
    if len(body) != GROUP_LEN * GROUPS or any(ch not in ALPHABET for ch in body):
        return None
    return PREFIX + "-".join(body[i:i + GROUP_LEN] for i in range(0, len(body), GROUP_LEN))


def looks_valid(value: Any) -> bool:
    """True only for the canonical spelling (use `normalize` for input)."""
    return bool(value) and bool(FORMAT_RE.match(str(value).strip()))


def assign(db, conn, user_id: int, *, table: str = "users", column: str = "vl_id") -> str:
    """
    Give *user_id* its VL ID, or return the one it already has.

    Idempotent by design: registration, the setup bootstrap, `backend create-admin`
    and the 0011 backfill all call this, and whichever runs last must not rotate
    an id somebody may already have shared.
    """
    row = db.query_one(conn, f"SELECT {column} AS v FROM {table} WHERE id = ?", (user_id,))
    current = (row or {}).get("v")
    if current:
        return str(current)
    for _ in range(_MAX_TRIES):
        candidate = new()
        if db.query_one(conn, f"SELECT 1 AS x FROM {table} WHERE {column} = ?", (candidate,)):
            continue
        db.execute(conn, f"UPDATE {table} SET {column} = ? WHERE id = ?", (candidate, user_id)).close()
        return candidate
    raise RuntimeError("could not allocate a unique VL ID")


__all__ = ["PREFIX", "ALPHABET", "FORMAT_RE", "new", "normalize", "looks_valid", "assign"]
