"""
Pluggable file storage.

`StorageProvider` is the seam the spec asks for: today `LocalStorageProvider`
keeps a self-hosted install dependency-free, and an S3/R2/MinIO provider can
be added later by implementing the same six methods — nothing in the API layer
needs to change.

Hard rules encoded here:
  * callers never control the stored key (generated in `security.stored_filename`)
  * `open()`/`url_for()` resolve keys through a category allowlist, so a
    crafted key can never escape the uploads root (path traversal)
  * nothing is read back from disk by path supplied by a client
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

from .config import get_config
from .log import get_logger

log = get_logger("storage")

#: Categories the app is allowed to write under. Mirrors the legacy layout so
#: `/files/<category>/<filename>` keeps working.
CATEGORIES: tuple[str, ...] = ("profiles", "chat", "stories", "posts", "games", "rooms")
_CATEGORY_RE = re.compile(r"^[a-z0-9_]{1,20}$")

KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class UnsafeKey(ValueError):
    """Raised when a key/category cannot be trusted."""


def validate_key(category: str, filename: str) -> tuple[str, str]:
    """Reject traversal, absolute paths, and control characters."""
    cat = (category or "").strip().lower()
    if cat not in CATEGORIES or not _CATEGORY_RE.match(cat):
        raise UnsafeKey("category")
    name = (filename or "").strip()
    if not KEY_RE.match(name) or "/" in name or "\\" in name or "\x00" in name:
        raise UnsafeKey("filename")
    if name in {".", ".."} or name.startswith("."):
        raise UnsafeKey("hidden")
    return cat, name


@dataclass
class StoredFile:
    category: str
    name: str
    original_name: str
    size: int
    mime: str
    sha256: str
    #: stable, DB-friendly reference ("<category>/<name>")
    key: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            self.key = f"{self.category}/{self.name}"


@dataclass
class UploadPolicy:
    """Per-purpose limits — the shape the API layer checks before touching disk."""
    max_bytes: int
    extensions: tuple[str, ...]
    kind: str = "file"

    @classmethod
    def for_kind(cls, kind: str) -> "UploadPolicy":
        cfg = get_config()
        table = {
            "image": (cfg.max_image_mb, cfg.image_extensions),
            "video": (cfg.max_video_mb, cfg.video_extensions),
            "audio": (cfg.max_file_mb, cfg.audio_extensions),
            "archive": (cfg.max_file_mb, cfg.archive_extensions),
            "doc": (cfg.max_file_mb, cfg.doc_extensions),
            "avatar": (cfg.max_avatar_mb, cfg.image_extensions),
            "file": (cfg.max_file_mb, tuple(cfg.image_extensions + cfg.video_extensions
                                            + cfg.audio_extensions + cfg.archive_extensions
                                            + cfg.doc_extensions)),
        }
        mb, exts = table.get(kind, table["file"])
        return cls(max_bytes=int(mb * 1024 * 1024), extensions=tuple(exts), kind=kind)


class StorageProvider:
    """Interface every backend must satisfy."""

    name = "abstract"

    def save(self, *, category: str, key_name: str, stream: BinaryIO,
             content_length: int | None = None) -> StoredFile:
        raise NotImplementedError

    def open(self, category: str, key_name: str) -> BinaryIO:
        raise NotImplementedError

    def delete(self, category: str, key_name: str) -> bool:
        raise NotImplementedError

    def exists(self, category: str, key_name: str) -> bool:
        raise NotImplementedError

    def url_for(self, category: str, key_name: str) -> str:
        """Public URL path the browser should request."""
        raise NotImplementedError

    def stat(self, category: str, key_name: str) -> dict:
        raise NotImplementedError

    # ---- optional conveniences with portable defaults ------------------
    def copy(self, src_category: str, src: str, dst_category: str, dst: str) -> bool:
        try:
            with self.open(src_category, src) as fh:
                self.save(category=dst_category, key_name=dst, stream=fh)
            return True
        except Exception as exc:                                  # pragma: no cover
            log.warning("copy_failed", extra={"ctx": {"err": str(exc)[:160]}})
            return False

    def sweep_orphans(self, referenced: Iterable[str], *, older_than_seconds: int = 86_400) -> int:
        """Delete files no longer referenced by any row and older than the grace window."""
        keep = set(referenced)
        removed = 0
        cutoff = time.time() - older_than_seconds
        for cat in CATEGORIES:
            for name in self.list_keys(cat):
                if name in keep:
                    continue
                info = self.stat(cat, name)
                if info.get("mtime", 0) < cutoff:
                    if self.delete(cat, name):
                        removed += 1
        return removed

    def list_keys(self, category: str) -> list[str]:
        return []

    def usage_bytes(self) -> int:
        return 0


class LocalStorageProvider(StorageProvider):
    """Files under `uploads/<category>/<name>` on the app's own disk."""

    name = "local"

    def __init__(self, root: Path | str | None = None):
        cfg = get_config()
        self.root = Path(root) if root else cfg.uploads_root
        self.root.mkdir(parents=True, exist_ok=True)
        for cat in CATEGORIES:
            (self.root / cat).mkdir(parents=True, exist_ok=True)

    # -- internals -------------------------------------------------------
    def _path(self, category: str, key_name: str) -> Path:
        cat, name = validate_key(category, key_name)
        base = self.root.resolve()
        target = (base / cat / name).resolve()
        # Defence in depth: even after validate_key, never leave the root.
        if base != target and base not in target.parents:
            raise UnsafeKey("escape")
        return target

    def _guard_size(self, size: int) -> None:
        cfg = get_config()
        ceiling = (cfg.max_file_mb + 50) * 1024 * 1024
        if size > ceiling:
            raise ValueError("file exceeds global ceiling")

    # -- interface -------------------------------------------------------
    def save(self, *, category: str, key_name: str, stream: BinaryIO,
             content_length: int | None = None) -> StoredFile:
        path = self._path(category, key_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with tmp.open("wb") as out:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if content_length:
                        self._guard_size(written)
                    digest.update(chunk)
                    out.write(chunk)
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return StoredFile(
            category=category, name=path.name, original_name="",
            size=written, mime="", sha256=digest.hexdigest(),
        )

    def open(self, category: str, key_name: str) -> BinaryIO:
        return self._path(category, key_name).open("rb")

    def delete(self, category: str, key_name: str) -> bool:
        try:
            self._path(category, key_name).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:                                     # pragma: no cover
            log.warning("delete_failed", extra={"ctx": {"err": str(exc)[:120]}})
            return False

    def exists(self, category: str, key_name: str) -> bool:
        try:
            return self._path(category, key_name).is_file()
        except UnsafeKey:
            return False

    def url_for(self, category: str, key_name: str) -> str:
        cat, name = validate_key(category, key_name)
        return f"/files/{cat}/{name}"

    def stat(self, category: str, key_name: str) -> dict:
        try:
            st = self._path(category, key_name).stat()
        except (OSError, UnsafeKey):
            return {}
        return {"size": st.st_size, "mtime": st.st_mtime}

    def list_keys(self, category: str) -> list[str]:
        cat = (category or "").lower()
        if cat not in CATEGORIES:
            return []
        folder = self.root / cat
        if not folder.is_dir():
            return []
        return [p.name for p in folder.iterdir() if p.is_file() and not p.name.endswith(".part")]

    def usage_bytes(self) -> int:
        total = 0
        for cat in CATEGORIES:
            folder = self.root / cat
            if not folder.is_dir():
                continue
            for p in folder.iterdir():
                if p.is_file():
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
        return total

    def disk_free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:                                            # pragma: no cover
            return 0


class MemoryStorageProvider(StorageProvider):
    """For tests: identical contract, nothing touches the filesystem."""

    name = "memory"

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def save(self, *, category: str, key_name: str, stream: BinaryIO,
             content_length: int | None = None) -> StoredFile:
        cat, name = validate_key(category, key_name)
        data = stream.read() if hasattr(stream, "read") else bytes(stream)  # type: ignore[arg-type]
        key = f"{cat}/{name}"
        self._blobs[key] = data
        return StoredFile(category=cat, name=name, original_name="", size=len(data),
                          mime="", sha256=hashlib.sha256(data).hexdigest(), key=key)

    def open(self, category: str, key_name: str) -> BinaryIO:
        cat, name = validate_key(category, key_name)
        return io.BytesIO(self._blobs[f"{cat}/{name}"])

    def delete(self, category: str, key_name: str) -> bool:
        cat, name = validate_key(category, key_name)
        return self._blobs.pop(f"{cat}/{name}", None) is not None

    def exists(self, category: str, key_name: str) -> bool:
        try:
            cat, name = validate_key(category, key_name)
        except UnsafeKey:
            return False
        return f"{cat}/{name}" in self._blobs

    def url_for(self, category: str, key_name: str) -> str:
        cat, name = validate_key(category, key_name)
        return f"/files/{cat}/{name}"

    def stat(self, category: str, key_name: str) -> dict:
        try:
            cat, name = validate_key(category, key_name)
            data = self._blobs[f"{cat}/{name}"]
        except (KeyError, UnsafeKey):
            return {}
        return {"size": len(data), "mtime": time.time()}

    def list_keys(self, category: str) -> list[str]:
        cat = (category or "").strip()
        return [k.split("/", 1)[1] for k in self._blobs if k.startswith(f"{cat}/")]


_providers: dict[str, StorageProvider] = {}


def get_storage() -> StorageProvider:
    """Provider chosen by `VOLEXTURN_STORAGE_BACKEND` (default: local)."""
    cfg = get_config()
    wanted = (os.environ.get("VOLEXTURN_STORAGE_BACKEND") or "local").strip().lower()
    if wanted in {"memory", "test"} and (cfg.env == "test" or wanted == "memory"):
        wanted = "memory"
    else:
        wanted = "local"
    if wanted not in _providers:
        _providers[wanted] = MemoryStorageProvider() if wanted == "memory" else LocalStorageProvider()
        log.info("storage_ready", extra={"ctx": {"backend": wanted}})
    return _providers[wanted]


def set_storage(provider: StorageProvider | None) -> None:
    if provider is None:
        _providers.clear()
        return
    _providers[provider.name if provider.name != "memory" else "memory"] = provider
    _providers[provider.name if provider.name != "local" else "local"] = provider


def reset_storage_cache() -> None:
    _providers.clear()


def read_head(stream: BinaryIO, nbytes: int = 32) -> tuple[bytes, BinaryIO]:
    """Peek at the first bytes, then rewind so the caller can still save."""
    head = stream.read(nbytes)
    try:
        stream.seek(0)
    except Exception:
        stream = io.BytesIO(head + stream.read())
    return head, stream


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"
