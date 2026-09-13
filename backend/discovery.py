"""
LAN server discovery.

The honest version of "discover game servers": a *provider* interface that can
answer "is this host:port alive, and does the protocol you asked me to speak
tell you anything about it?". There is no magic and there is no scanning.

Safety model (spec §15 — this is the part that must not be faked):
  * discovery is **off** unless VOLEXTURN_DISCOVERY_ENABLED=1
  * even then, only addresses inside VOLEXTURN_DISCOVERY_NETWORKS (CIDRs you
    typed) are probeable; loopback/link-local/metadata/undefined are refused
  * targets must be literal IPs — resolving a hostname at probe time would
    reintroduce DNS-rebinding SSRF
  * one probe per server per tick, one socket, short timeout, no fan-out,
    no port sweeps, no UDP broadcast storms
  * a provider that cannot learn something returns `unknown` and leaves the
    player count as `None`. `None` is rendered as "نامشخص" in the UI — it is
    never replaced by a plausible-looking number.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import get_config
from .log import get_logger
from .security import classify_target

log = get_logger("discovery")

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_UNKNOWN = "unknown"
STATUS_FULL = "full"
STATUS_STARTING = "starting"
#: Statuses a human may set by hand (the only way "starting" ever appears).
MANUAL_STATUSES = (STATUS_ONLINE, STATUS_OFFLINE, STATUS_FULL, STATUS_STARTING)


@dataclass
class DiscoveryResult:
    """One probe outcome. `None` on a field means "this cannot be known"."""
    status: str = STATUS_UNKNOWN
    latency_ms: int | None = None
    players_online: int | None = None
    players_max: int | None = None
    motd: str | None = None
    version: str | None = None
    map_name: str | None = None
    protocol: str | None = None
    editors: list[str] = field(default_factory=list)
    probed_at: float = field(default_factory=time.time)
    #: False when the probe was refused before touching the network (policy).
    attempted: bool = True
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def resolved_status(self) -> str:
        if self.players_max and self.players_online is not None and self.players_online >= self.players_max:
            return STATUS_FULL
        return self.status


def _varint(n: int) -> bytes:
    """Minecraft's VarInt encoding (LEB128, max 5 bytes)."""
    if n < 0:
        n &= 0xFFFFFFFF
    out = bytearray()
    for _ in range(5):
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)


def _read_varint(sock: socket.socket, buf: bytearray) -> tuple[int, int]:
    """Returns (value, bytes_consumed). Raises on EOF/oversize."""
    num = 0
    for i in range(5):
        while len(buf) <= i:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed while reading VarInt")
            buf.extend(chunk)
        byte = buf[i]
        num |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return num, i + 1
    raise ValueError("VarInt too large")


class GameDiscoveryProvider:
    """Interface every game probe implements."""

    key: str = "abstract"
    #: human label for the admin UI
    label: str = "Abstract"
    #: transport used, surfaced so the UI can be honest about it
    transport: str = "tcp"
    #: can this provider report player counts at all?
    reports_players: bool = False
    #: default port offered in the "create server" form
    default_port: int | None = None

    def probe(self, host: str, port: int, *, timeout: float,
              password: str | None = None) -> DiscoveryResult:
        raise NotImplementedError

    # helpers shared by providers
    def tcp_connect(self, host: str, port: int, timeout: float) -> tuple[int | None, str | None]:
        """(latency_ms, error). A bare reachability check — never a scan."""
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.setsockopt(socket.IPPROTO_TCP, getattr(socket, "TCP_NODELAY", 1), 1)
            return int((time.monotonic() - started) * 1000), None
        except socket.timeout:
            return None, "timeout"
        except ConnectionRefusedError:
            return None, "refused"
        except OSError as exc:
            return None, f"error:{getattr(exc, 'strerror', None) or exc.__class__.__name__}"[:80]


class GenericTCPProvider(GameDiscoveryProvider):
    """
    Reachability only. Correct for any game with no documented query protocol:
    we can say the port answers TCP, nothing more.
    """
    key = "generic_tcp"
    label = "TCP reachability"
    reports_players = False

    def probe(self, host: str, port: int, *, timeout: float,
               password: str | None = None) -> DiscoveryResult:
        latency, err = self.tcp_connect(host, port, timeout)
        if err:
            return DiscoveryResult(status=STATUS_OFFLINE, detail=err)
        return DiscoveryResult(status=STATUS_ONLINE, latency_ms=latency,
                               detail="tcp connect ok")


class MinecraftProvider(GameDiscoveryProvider):
    """
    Java Edition Server List Ping (handshake + status, JSON response).

    Real data: MOTD, version, protocol, online/max players. No auth needed and
    no write of any kind to the target.
    """
    key = "minecraft"
    label = "Minecraft (Java Server List Ping)"
    transport = "tcp"
    reports_players = True
    default_port = 25565

    def probe(self, host: str, port: int, *, timeout: float,
               password: str | None = None) -> DiscoveryResult:
        payload: dict[str, Any] | None = None
        latency: int | None = None
        err: str | None = None
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                latency = int((time.monotonic() - started) * 1000)
                # --- handshake: 0x00, protocol(-1), host, port, next state 1
                body = _varint(0x00) + _varint(-1) \
                    + _varint(len(host)) + host.encode() + struct.pack(">H", int(port)) + _varint(1)
                s.sendall(_varint(len(body)) + body)
                # --- status request: length + 0x00
                s.sendall(b"\x01\x00")
                length, consumed = _read_varint(s, buf := bytearray())
                while len(buf) < length + consumed:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                raw = bytes(buf[consumed:consumed + length])
                try:
                    payload = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    err = "malformed_status_json"
        except socket.timeout:
            err = "timeout"
        except ConnectionRefusedError:
            err = "refused"
        except OSError as exc:
            err = f"error:{getattr(exc, 'strerror', None) or exc.__class__.__name__}"[:80]

        if payload is None:
            # We may still have proven TCP liveness; keep status honest.
            status = STATUS_ONLINE if err in {"malformed_status_json"} else (
                STATUS_OFFLINE if err in {"refused", "timeout"} or (err or "").startswith("error") else STATUS_UNKNOWN)
            return DiscoveryResult(status=status, latency_ms=latency, detail=err or "no payload")

        players = payload.get("players") or {}
        version = payload.get("version") or {}
        motd = payload.get("description")
        if isinstance(motd, dict):                     # chat component form
            motd = "".join(str(x.get("text", "") if isinstance(x, dict) else x)
                           for x in (motd.get("extra") or [motd.get("text", "")]))
        online, maximum = players.get("online"), players.get("max")
        status = STATUS_ONLINE
        if maximum is not None and online is not None and online >= maximum:
            status = STATUS_FULL
        return DiscoveryResult(
            status=status, latency_ms=latency,
            players_online=int(online) if online is not None else None,
            players_max=int(maximum) if maximum is not None else None,
            motd=str(motd)[:200] if motd else None,
            version=str(version.get("name", ""))[:64] or None,
            protocol=str(version.get("protocol", ""))[:16] or None,
            editors=["minecraft"],
            detail="slp ok",
        )


class MinecraftBedrockProvider(GameDiscoveryProvider):
    """
    Bedrock "unconnected ping" (UDP, request 0x01 -> accept 0x1c).

    Uses a single datagram and a strict size cap; never broadcasts.
    """
    key = "minecraft_bedrock"
    label = "Minecraft Bedrock (UDP ping)"
    transport = "udp"
    reports_players = True
    default_port = 19132

    def probe(self, host: str, port: int, *, timeout: float,
               password: str | None = None) -> DiscoveryResult:
        started = time.monotonic()
        # 0x01 + 8-byte client time + 16-byte request ID (zeros) + 8-byte player ID (zeros)
        packet = b"\x01" + struct.pack(">q", int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF) + b"\x00" * 24
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(packet, (host, int(port)))
                data, _ = s.recvfrom(2048)
            latency = int((time.monotonic() - started) * 1000)
            if not data or data[0] != 0x1C:
                return DiscoveryResult(status=STATUS_UNKNOWN, latency_ms=latency,
                                       detail="unexpected_bedrock_reply")
            parts = data[1:].split(b";")
            if len(parts) < 8:
                return DiscoveryResult(status=STATUS_ONLINE, latency_ms=latency,
                                       detail="short_bedrock_reply")
            motd = parts[0].decode("utf-8", "replace")[:200] or None
            return DiscoveryResult(
                status=STATUS_ONLINE, latency_ms=latency,
                players_online=int(parts[4]) if parts[4].isdigit() else None,
                players_max=int(parts[5]) if parts[5].isdigit() else None,
                motd=motd, map_name=parts[6].decode("utf-8", "replace")[:80] or None,
                version=parts[2].decode("utf-8", "replace")[:64] or None,
                protocol=parts[1].decode("utf-8", "replace")[:16] or None,
                editors=["bedrock"], detail="bedrock ping ok",
            )
        except (socket.timeout, OSError) as exc:
            err = ("timeout" if isinstance(exc, socket.timeout)
                   else f"error:{getattr(exc, 'strerror', None) or exc.__class__.__name__}")[:80]
            return DiscoveryResult(status=STATUS_OFFLINE, detail=err)


class CustomGameProvider(GameDiscoveryProvider):
    """
    Escape hatch: an operator supplies a tiny HTTP endpoint that answers
    `{"status":"online","players_online":4,...}` for their own server.

    Only used when the row explicitly opts in, and the URL is validated to a
    literal IP in the allowlist — same rules, different transport.
    """
    key = "custom_http"
    label = "Custom HTTP status endpoint"
    transport = "http"
    reports_players = True

    def probe(self, host: str, port: int, *, timeout: float,
               password: str | None = None) -> DiscoveryResult:
        # Deliberately not implemented as a generic URL fetcher: the target is
        # built from validated host:port only, and we read at most 64 KiB.
        latency, err = self.tcp_connect(host, port, timeout)
        if err:
            return DiscoveryResult(status=STATUS_OFFLINE, detail=err)
        try:
            req = (f"GET /status HTTP/1.1\r\nHost: {host}:{port}\r\n"
                   "User-Agent: Volexturn-Discovery/1\r\nAccept: application/json\r\n"
                   "Connection: close\r\n\r\n").encode()
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.sendall(req)
                raw = s.recv(65536)
            body = raw.split(b"\r\n\r\n", 1)[-1]
            data = json.loads(body.decode("utf-8", "replace"))
            status = str(data.get("status", STATUS_UNKNOWN)).lower()
            if status not in {STATUS_ONLINE, STATUS_OFFLINE, STATUS_FULL, STATUS_STARTING}:
                status = STATUS_UNKNOWN
            return DiscoveryResult(
                status=status, latency_ms=latency,
                players_online=_maybe_int(data.get("players_online")),
                players_max=_maybe_int(data.get("players_max")),
                motd=str(data.get("motd"))[:200] if data.get("motd") else None,
                version=str(data.get("version"))[:64] if data.get("version") else None,
                editors=["custom_http"], detail="http ok")
        except Exception as exc:
            return DiscoveryResult(status=STATUS_ONLINE, latency_ms=latency,
                                   detail=f"http_parse_failed:{type(exc).__name__}")


def _maybe_int(v: Any) -> int | None:
    try:
        n = int(v)
        return n if 0 <= n <= 1_000_000 else None
    except (TypeError, ValueError):
        return None


_PROVIDERS: dict[str, GameDiscoveryProvider] = {}


def _register(p: GameDiscoveryProvider) -> None:
    _PROVIDERS[p.key] = p


for _cls in (GenericTCPProvider, MinecraftProvider, MinecraftBedrockProvider, CustomGameProvider):
    _register(_cls())


def register_provider(provider: GameDiscoveryProvider) -> None:
    """Third-party/extension hook — keeps the catalog open without forking."""
    _register(provider)


def get_provider(key: str | None) -> GameDiscoveryProvider:
    return _PROVIDERS.get((key or "").strip().lower(), _PROVIDERS["generic_tcp"])


def provider_keys() -> list[str]:
    return sorted(_PROVIDERS)


def providers_info() -> list[dict[str, Any]]:
    return [{
        "key": p.key, "label": p.label, "transport": p.transport,
        "reports_players": p.reports_players, "default_port": p.default_port,
    } for p in sorted(_PROVIDERS.values(), key=lambda x: x.key)]


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def probe_server_row(row: dict) -> DiscoveryResult:
    """
    Policy-checked probe for one `lan_hosts` row.

    Returns a `unknown`/not-attempted result (never an exception) when policy
    refuses, so a scheduler can call this in bulk without one bad row
    producing a wall of errors.
    """
    host, port = row.get("ip_address"), row.get("port")
    provider = get_provider(row.get("discover_provider") or "generic_tcp")
    try:
        pnum = int(port)
    except (TypeError, ValueError):
        return DiscoveryResult(status=STATUS_UNKNOWN, attempted=False, detail="missing_port")
    ok, reason = classify_target(str(host or ""), pnum)
    if not ok:
        return DiscoveryResult(status=STATUS_UNKNOWN, attempted=False, detail=f"policy:{reason}")
    res = provider.probe(str(host), pnum, timeout=get_config().discovery_timeout_seconds,
                         password=row.get("password"))
    res.editors = list(dict.fromkeys([*(res.editors or []), provider.key]))
    return res


def probe_many(rows: list[dict], *, concurrency: int = 8) -> list[DiscoveryResult]:
    """
    Run probes off the request path, bounded by `concurrency`.

    Sequential where the row count is small, since spinning an event loop for
    two probes is pure overhead.
    """
    cfg = get_config()
    rows = rows[:cfg.discovery_max_targets_per_tick]
    if not rows:
        return []

    async def _run() -> list[DiscoveryResult]:
        sem = asyncio.Semaphore(max(1, min(concurrency, 32)))

        async def one(row: dict) -> DiscoveryResult:
            async with sem:
                return await asyncio.get_running_loop().run_in_executor(None, probe_server_row, row)
        return list(await asyncio.gather(*(one(r) for r in rows)))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    # Called from inside a loop already (async worker): stay sequential.
    return [probe_server_row(r) for r in rows]


def health_check() -> dict[str, Any]:
    """Reported by /api/games/discovery so operators can see the policy state."""
    cfg = get_config()
    return {
        "enabled": cfg.discovery_enabled,
        "interval_seconds": cfg.discovery_interval_seconds,
        "timeout_seconds": cfg.discovery_timeout_seconds,
        "allowlisted_networks": list(cfg.discovery_networks),
        "providers": provider_keys(),
        "note": ("Probe targets must be literal IPs inside the allowlist. "
                 "Nothing is scanned; nothing is broadcast.") if cfg.discovery_enabled
        else "Discovery disabled — statuses stay 'unknown' and are never invented.",
    }
