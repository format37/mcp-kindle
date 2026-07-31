"""The flat ``data/`` image folder — the local skill's shared picture library.

Two image stores exist in this server and they are not interchangeable:

* ``data/books/<book_id>/assets/`` — book-scoped, normalised for e-ink on the
  way in by :func:`books.store_asset`, addressed by an unguessable id. This is
  what the claude.ai flow uses.
* ``data/`` itself — a flat folder of prepared images that ``send_html_to_kindle``
  resolves bare ``<img src="cover.jpg">`` filenames against. Claude Code writes
  straight into it over the bind mount. This module owns *that* one.

Bytes here are stored and embedded **as-is** (``html_tools._embed_images`` does
not re-encode them), which is the whole point: an image prepared deliberately
must reach the EPUB unchanged. The console reports size and dimensions instead
of silently fixing them.
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

PILImage.MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(64_000_000)))

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))

#: What ``html_tools.IMAGE_MEDIA_TYPES`` can actually embed.
ALLOWED_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_FORMAT_TO_EXT = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}

#: Above this an image is flagged in the listing — it is preview.py's own
#: per-image lint threshold, so the warning matches what a preview would say.
BIG_IMAGE_BYTES = int(os.getenv("PREVIEW_MAX_IMAGE_BYTES", "1500000"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

MAX_STEM_LEN = 64

#: Never listed, never served, never deleted through the console.
_SKIP_NAMES = {"books", ".thumbs", ".gitkeep"}


class LibraryError(Exception):
    """Bad caller input or a missing image. Message is written for a human."""


def _guard(child: Path) -> Path:
    """Resolve ``child`` and verify it is a direct child of DATA_DIR.

    ``resolve()`` follows symlinks, so a link planted in ``data/`` pointing at
    ``/etc/passwd`` fails here rather than being served. Direct child, not
    descendant: the flat folder is deliberately one level deep, and ``books/``
    has its own guarded accessors.
    """
    try:
        resolved = child.resolve()
        base = DATA_DIR.resolve()
    except (OSError, ValueError) as exc:
        raise LibraryError(f"invalid path: {exc}") from exc
    if resolved.parent != base:
        raise LibraryError("path escapes the data folder")
    return resolved


def _sanitise_stem(base: str) -> str:
    """Reduce a caller-supplied name to a safe ``[a-z0-9_-]`` stem."""
    if "\x00" in base:
        raise LibraryError("filename contains a NUL byte")
    base = Path(base).name
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-_")
    stem = re.sub(r"-{2,}", "-", stem)[:MAX_STEM_LEN].strip("-_")
    return stem or "image"


def _is_library_file(path: Path) -> bool:
    # is_symlink() before is_file(): is_file() follows the link, so without this
    # a symlink planted in data/ is listed and thumbnailed while image_path's
    # _guard rejects it — a tile that 404s its own image.
    return (
        not path.is_symlink()
        and path.is_file()
        and not path.name.startswith(".")
        and path.name not in _SKIP_NAMES
        and path.suffix.lower() in ALLOWED_EXT
    )


def list_images() -> list[dict]:
    """Every embeddable image directly inside ``data/``, newest first.

    Dimensions come from a header read, not a full decode, so a folder of
    hundreds costs nothing to list.
    """
    if not DATA_DIR.is_dir():
        return []
    rows: list[dict] = []
    for path in DATA_DIR.iterdir():
        if not _is_library_file(path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        width = height = None
        try:
            with PILImage.open(path) as im:
                width, height = im.size
        except Exception as exc:
            logger.warning("library: %s is not a readable image (%s)", path.name, exc)
        rows.append({
            "filename": path.name,
            "bytes": stat.st_size,
            "mtime": stat.st_mtime,
            "width": width,
            "height": height,
            "big": stat.st_size > BIG_IMAGE_BYTES,
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def image_path(filename: str) -> Path:
    """Traversal-guarded path of an existing library image."""
    filename = str(filename or "").strip()
    if not filename or filename != Path(filename).name:
        raise LibraryError(f"{filename[:80]!r} must be a bare filename")
    if filename.startswith(".") or filename in _SKIP_NAMES:
        raise LibraryError("not a library image")
    path = _guard(DATA_DIR / filename)
    if not _is_library_file(path):
        raise LibraryError(f"no library image named {filename!r}")
    return path


def _unique_name(stem: str, ext: str) -> str:
    n = 1
    while True:
        candidate = f"{stem}{ext}" if n == 1 else f"{stem}-{n}{ext}"
        if not (DATA_DIR / candidate).exists():
            return candidate
        n += 1


def store_upload(filename: str, data: bytes) -> dict:
    """Validate uploaded bytes as an image and save them unchanged.

    The extension comes from what Pillow actually decoded, not from the name the
    browser sent — a ``.png`` that is really a JPEG would otherwise be embedded
    with the wrong media type and render as a broken image on the device.
    Existing files are never overwritten; a taken name gets ``-2``, ``-3``, …
    """
    if not data:
        raise LibraryError("the upload was empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LibraryError(
            f"{len(data) // (1024 * 1024)} MB exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
            f"upload limit"
        )
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            # open() reads the header only. Check the pixel count here, because
            # Pillow's own MAX_IMAGE_PIXELS merely warns up to twice the limit
            # and then decodes: a 120 MP grayscale PNG slips under the byte cap
            # above and expands to ~360 MB of RAM inside load().
            if im.size[0] * im.size[1] > PILImage.MAX_IMAGE_PIXELS:
                raise LibraryError(
                    f"{im.size[0]}x{im.size[1]} is more pixels than this server will "
                    f"decode; downscale it first"
                )
            im.load()
            fmt = (im.format or "").upper()
            size = im.size
    except LibraryError:
        raise
    except Exception as exc:
        raise LibraryError(f"not a readable image ({exc})") from exc

    ext = _FORMAT_TO_EXT.get(fmt)
    if not ext:
        raise LibraryError(
            f"{fmt or 'unknown'} images cannot be embedded in an EPUB; "
            f"use PNG, JPEG, GIF or WEBP"
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stem = _sanitise_stem(filename)
    # Write the bytes first, then claim a name by hard-linking them into place.
    # Both halves matter. Linking is atomic and fails if the name is taken, so
    # two uploads of "cover.png" arriving together cannot both pass the
    # existence probe in _unique_name and have one land on top of the other; and
    # because the link publishes a file that is already complete, a concurrent
    # EPUB build can never resolve the name to an empty or half-written image.
    #
    # Dotted, and unique per writer: a ".part" left behind by a crash is
    # invisible to _is_library_file either way, but a dotted one is also
    # invisible to a human ls and gets swept at startup.
    tmp = DATA_DIR / f".upload.{os.getpid()}-{threading.get_ident()}.part"
    target = None
    try:
        tmp.write_bytes(data)
        for _ in range(50):
            candidate = _guard(DATA_DIR / _unique_name(stem, ext))
            try:
                os.link(tmp, candidate)
            except FileExistsError:
                continue
            except OSError:
                # No hardlinks on this filesystem. Rename instead: still atomic,
                # just without the "fails if taken" guarantee.
                tmp.replace(candidate)
            target = candidate
            break
        if target is None:
            raise LibraryError(f"could not find a free filename for {stem}{ext}")
    finally:
        tmp.unlink(missing_ok=True)
    logger.info(
        "library upload: %s (%dx%d %s, %d bytes)", target.name, size[0], size[1], fmt, len(data)
    )
    return {
        "filename": target.name,
        "bytes": len(data),
        "width": size[0],
        "height": size[1],
        "big": len(data) > BIG_IMAGE_BYTES,
    }


def delete(filename: str) -> None:
    path = image_path(filename)
    path.unlink()
    logger.info("library: deleted %s", path.name)


def usage() -> dict:
    """``{"count", "bytes"}`` for the console's summary strip."""
    rows = list_images()
    return {"count": len(rows), "bytes": sum(r["bytes"] for r in rows)}


def sweep_temp(max_age_s: float = 3600) -> int:
    """Remove upload temp files a killed process left behind. Returns the count.

    They are dotfiles with no allowed extension, so nothing else in the server
    can see them — without this they would accumulate silently forever.
    """
    if not DATA_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for path in DATA_DIR.glob(".*.part"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("swept %d stale upload temp file(s)", removed)
    return removed
