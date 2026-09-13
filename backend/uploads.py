"""
Upload pipeline shared by posts, stories, chat, avatars, game/room media.

The pre-upgrade build trusted the file *extension* (docs/AUDIT.md §7 S7), which
is trivially bypassed and allowed `.svg` — a stored-XSS vector for an app whose
CSP keeps `'unsafe-inline'`. Here the rules are:

  1. extension must be on the allowlist for the declared purpose
  2. magic bytes must agree with that extension
  3. the browser's declared MIME type must not contradict either
  4. size is enforced per-purpose while streaming, not after
  5. the stored name is **generated** — the client's filename is metadata only
  6. `.svg`/`.html`/`.xhtml` are never accepted

Because the stored key is generated and validated, and reads go through
`StorageProvider._path`, path traversal has no reachable code path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import get_config
from .errors import BadRequest, PayloadTooLarge, UnsupportedMediaType
from .log import get_logger
from .security import stored_filename, validate_upload
from .storage import StoredFile, UploadPolicy, get_storage, read_head

log = get_logger("uploads")

#: Types that are never acceptable regardless of purpose.
DENY_EXTENSIONS = frozenset({
    ".svg", ".html", ".htm", ".xhtml", ".xml", ".js", ".mjs", ".css", ".php",
    ".py", ".sh", ".exe", ".bat", ".cmd", ".com", ".jar", ".apk", ".ipa",
    ".msi", ".dll", ".so", ".dylib", ".wsf", ".vbs", ".ps1", ".scf", ".url",
})

_KIND_FOR_EXT = {
    **{e: "image" for e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")},
    **{e: "video" for e in (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")},
    **{e: "audio" for e in (".mp3", ".wav", ".ogg", ".m4a", ".flac")},
    **{e: "archive" for e in (".zip", ".rar", ".7z")},
    **{e: "doc" for e in (".txt", ".pdf", ".json", ".csv", ".log")},
}


@dataclass
class UploadResult:
    name: str                    # generated stored key
    original_name: str           # what the user called it (display only)
    kind: str                    # image | video | audio | file
    size: int
    mime: str
    sha256: str
    url: str

    def as_dict(self) -> dict[str, Any]:
        return {"file_path": self.name, "file_type": self.kind,
                "file_name": self.original_name, "mime": self.mime,
                "size": self.size, "url": self.url}


def kind_for(filename: str) -> str:
    ext = _ext(filename)
    return _KIND_FOR_EXT.get(ext, "file")


def _ext(filename: str | None) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,6})$", filename or "")
    return m.group(1).lower() if m else ""


def save_upload(file_storage, *, user_id: int, category: str, kind: str | None = None,
                policy: UploadPolicy | None = None) -> UploadResult:
    """
    Validate then persist one `FileStorage`. Raises an ApiError subclass so the
    client gets a code and a Persian message it can show directly.
    """
    if file_storage is None or not getattr(file_storage, "filename", ""):
        raise BadRequest("فایلی ارسال نشده است", code="FILE_MISSING")

    original = str(file_storage.filename)
    ext = _ext(original)
    if ext in DENY_EXTENSIONS:
        raise UnsupportedMediaType(
            f"نوع فایل {ext} به دلایل امنیتی پذیرفته نمی‌شود", code="DENIED_EXTENSION")

    declared_kind = kind or kind_for(original)
    pol = policy or UploadPolicy.for_kind(declared_kind)

    # Size gate #1: the browser's claim (cheap early reject).
    try:
        announced = int(getattr(file_storage, "content_length", 0) or 0)
    except (TypeError, ValueError):
        announced = 0
    if announced and announced > pol.max_bytes:
        raise PayloadTooLarge(
            f"حجم مجاز برای {declared_kind}: {_mb(pol.max_bytes)} مگابایت — فایل شما بزرگ‌تر است",
            code="FILE_TOO_LARGE",
            details={"max_bytes": pol.max_bytes, "announced": announced})

    stream = file_storage.stream
    head, stream = read_head(stream, 32)
    ok, reason, sniffed = validate_upload(original, head, allow=pol.extensions,
                                           declared_mime=getattr(file_storage, "mimetype", None))
    if not ok:
        log.info("upload_rejected", extra={"ctx": {
            "user_id": int(user_id), "reason": reason, "ext": ext, "sniffed": sniffed}})
        raise UnsupportedMediaType(_reject_message(reason, pol), code=f"UPLOAD_{reason.upper()}",
                                    details={"reason": reason, "extension": ext})

    storage = get_storage()
    name = stored_filename(user_id, original)
    try:
        saved = _save_with_size_guard(storage, category=category, key_name=name,
                                      stream=stream, max_bytes=pol.max_bytes, head=head)
    except PayloadTooLarge:
        storage.delete(category, name)
        raise
    except Exception as exc:
        storage.delete(category, name)
        log.warning("upload_save_failed", extra={"ctx": {"err": str(exc)[:200]}})
        raise BadRequest("ذخیره فایل ناموفق بود", code="SAVE_FAILED")

    final_kind = declared_kind if declared_kind != "file" else _KIND_FOR_EXT.get(ext, "file")
    result = UploadResult(
        name=saved.name, original_name=_display_name(original), kind=final_kind,
        size=saved.size, mime=sniffed or saved.mime or "", sha256=saved.sha256,
        url=storage.url_for(category, saved.name),
    )
    log.info("upload_saved", extra={"ctx": {"user_id": int(user_id), "category": category,
                                            "kind": final_kind, "size": saved.size}})
    return result


def _save_with_size_guard(storage, *, category: str, key_name: str, stream,
                          max_bytes: int, head: bytes) -> StoredFile:
    """
    Size gate #2: enforced while streaming, so a lying Content-Length cannot
    smuggle an oversized file past the first check.
    """
    import io

    class Guarded(io.RawIOBase):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner
            self.n = 0

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            chunk = self.inner.read(size) if size and size > 0 else self.inner.read()
            if not chunk:
                return b""
            self.n += len(chunk)
            if self.n > max_bytes:
                raise PayloadTooLarge(
                    f"حجم فایل از {int(max_bytes / 1024 / 1024)} مگابایت بیشتر شد",
                    code="FILE_TOO_LARGE")
            return chunk

        def seek(self, *a):                    # storage may rewind
            try:
                return self.inner.seek(*a)
            except Exception:
                return 0

        def seekable(self) -> bool:
            return False

        def tell(self) -> int:
            return self.n

    return storage.save(category=category, key_name=key_name,
                        stream=Guarded(stream), content_length=None)


def _display_name(original: str) -> str:
    """Strip anything markup-ish; the name is only ever shown as text."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(original))[-120:]
    return name.replace("\\", "/").rsplit("/", 1)[-1][:120]


def _mb(nbytes: int) -> int:
    return max(1, int(nbytes / 1024 / 1024))


def _reject_message(reason: str, pol: UploadPolicy) -> str:
    if reason.startswith("extension"):
        allowed = "، ".join(sorted(pol.extensions)[:10])
        return f"پسوند فایل مجاز نیست. مجاز: {allowed}"
    if reason == "unrecognised_content":
        return "محتوای فایل قابل شناسایی نیست"
    if reason in {"content_type_mismatch", "declared_type_mismatch"}:
        return "محتوای فایل با نوع اعلام‌شده همخوانی ندارد"
    return "فایل نامعتبر است"


def delete_upload(category: str, name: str | None) -> bool:
    """Best-effort removal used by delete endpoints and the janitor sweep."""
    if not name:
        return False
    try:
        return get_storage().delete(category, str(name))
    except Exception as exc:                                    # pragma: no cover
        log.debug("upload_delete_failed", extra={"ctx": {"err": str(exc)[:120]}})
        return False


def referenced_keys(db, conn) -> set[str]:
    """
    Every media key still referenced by a row — input to the orphan sweep.

    Keys are stored bare (filename only) by design, so the category is what
    scopes them; we return "<category>/<name>" to match `sweep_orphans`.
    """
    out: set[str] = set()
    for cat, table, col in (("profiles", "users", "avatar"),
                            ("chat", "messages", "file_path"),
                            ("stories", "stories", "file_path"),
                            ("posts", "posts", "file_path")):
        if not db.has_table(conn, table):
            continue
        try:
            for row in db.query(conn, f"SELECT DISTINCT {col} AS k FROM {table} WHERE {col} IS NOT NULL"):
                if row.get("k"):
                    out.add(f"{cat}/{str(row['k']).strip('/')}")
        except Exception:                                       # pragma: no cover
            continue
    return out


def media_limits_summary() -> dict[str, int]:
    cfg = get_config()
    return {"image_mb": cfg.max_image_mb, "video_mb": cfg.max_video_mb,
            "file_mb": cfg.max_file_mb, "avatar_mb": cfg.max_avatar_mb}
