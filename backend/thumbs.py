"""Cached thumbnails for the console's image grids.

**This module does not validate paths.** It renders whatever file it is handed,
so every caller must resolve the name through a guarded accessor first —
``library.image_path`` or ``books.asset_ref``. A route that passed
``DATA_DIR / filename`` straight in would be an arbitrary-file-read dressed up
as a JPEG.


A book page shows every asset and the library page shows the whole flat data
folder. Serving the originals would be ~90 images at up to 2.5 MB each on one
page load; at that point the grid is slower than the EPUB it describes. So each
source image gets one cached 320 px grayscale JPEG (~10 kB), regenerated only
when the source changes.

The cache key carries the source's mtime, so a replaced image cannot be served
from a stale thumbnail, and generating a new one drops the old versions of that
same source. Everything lives in ``<DATA_DIR>/.thumbs`` — a dotfile directory
the library listing skips, so thumbnails never show up as library images.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

# Same decompression-bomb ceiling books.py applies; this module is importable
# on its own, so it cannot rely on that import having happened.
PILImage.MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(64_000_000)))

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
THUMB_DIR = DATA_DIR / ".thumbs"

SIZE = int(os.getenv("CONSOLE_THUMB_PX", "320"))
QUALITY = 78
#: Thumbnails untouched for this long are dropped at startup. Regenerating one
#: costs milliseconds, so the cache is free to be forgetful.
MAX_AGE_S = 30 * 24 * 3600


class ThumbError(RuntimeError):
    """The source is missing or is not a decodable image."""


def _key(source: Path, size: int) -> tuple[str, str]:
    """``(path_hash, filename)`` — the hash groups every thumb of one source."""
    digest = hashlib.sha1(str(source).encode("utf-8", "surrogateescape")).hexdigest()[:16]
    try:
        stamp = source.stat().st_mtime_ns
    except OSError as exc:
        raise ThumbError(f"cannot stat {source.name}: {exc}") from exc
    return digest, f"{digest}-{stamp}-{size}.jpg"


def thumb_for(source: Path, size: int = SIZE) -> Path:
    """Path of the cached thumbnail for ``source``, generating it if needed."""
    source = Path(source)
    if not source.is_file():
        raise ThumbError(f"no such image: {source.name}")
    digest, name = _key(source, size)
    target = THUMB_DIR / name
    if target.is_file():
        return target

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with PILImage.open(source) as im:
            # Pillow's MAX_IMAGE_PIXELS only *warns* between one and two times
            # the limit and decodes anyway, and open() is a header read — so
            # the size check has to happen here, before load() commits the RAM.
            if im.size[0] * im.size[1] > PILImage.MAX_IMAGE_PIXELS:
                raise ThumbError(
                    f"{source.name} is {im.size[0]}x{im.size[1]}; too large to decode"
                )
            im.load()
            # Alpha over black would render transparent line art as a black
            # square; the grid is the one place that looks like a broken image.
            if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
                flat = PILImage.new("RGBA", im.size, (255, 255, 255, 255))
                im = PILImage.alpha_composite(flat, im.convert("RGBA"))
            page = im.convert("L")
        page.thumbnail((size, size), PILImage.Resampling.LANCZOS)
        # Two requests for the same image race here (a browser opens a grid of
        # them at once); the temp name carries the writer's identity so neither
        # renames the other's half-written file into place.
        tmp = target.with_name(f".{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            page.save(tmp, format="JPEG", quality=QUALITY, optimize=True)
            tmp.replace(target)  # atomic: a reader never sees a partial file
        finally:
            tmp.unlink(missing_ok=True)
    except ThumbError:
        raise
    except Exception as exc:
        raise ThumbError(f"{source.name} is not a readable image ({exc})") from exc

    # Older thumbnails of this same source (previous mtimes or sizes) are dead.
    # Matching ".jpg" and not "*" matters: a bare glob would also catch the
    # in-flight temp file of a concurrent render at another size.
    for stale in THUMB_DIR.glob(f"{digest}-*.jpg"):
        if stale != target and stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass
    return target


def prune(max_age_s: float = MAX_AGE_S) -> int:
    """Drop thumbnails not written within ``max_age_s``. Returns the count."""
    if not THUMB_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    for path in THUMB_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("pruned %d cached thumbnail(s)", removed)
    return removed
