"""Book workspace + asset store — the server's state layer.

A "book" is a directory holding one HTML document plus its images, so a model
driving this server from claude.ai (no filesystem, no shell) can address content
by id instead of by path:

    <DATA_DIR>/books/<book_id>/
        meta.json
        doc.html                current document (may not exist yet)
        assets/<filename>       book-scoped images
        out/                    preview artifacts (preview.pdf, page-01.jpg, ...)

``<DATA_DIR>`` itself stays a legacy flat folder for the local skill; nothing
here touches it outside ``books/``.

``book_id`` is ``<slug>-<12 hex>``. The hex suffix is what makes assets safe to
serve over public HTTPS: the path is unguessable, so no auth is needed on the
static asset route.

Every image in the server funnels through :func:`store_asset` — that is the one
place e-ink normalisation (grayscale, downscale, format choice) happens, so a
Gemini render, a fetched URL, a PlantUML diagram and a matplotlib formula all
land in the EPUB looking the same.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image as PILImage

logger = logging.getLogger(__name__)

# Same bomb guard as fetch.py: books.py is importable on its own (diagrams and
# generated images never pass through fetch), so it cannot rely on that import.
PILImage.MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(64_000_000)))

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
BOOKS_DIR = DATA_DIR / "books"

ALLOWED_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MIME_TO_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}

MAX_SLUG_LEN = 24
MAX_STEM_LEN = 48
BOOK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{12}$")

# Distinct-colour ceiling under which a PNG is treated as line art (keep it
# lossless) rather than a photo (JPEG). Grayscale sources need a tighter bound —
# mode "L" can only ever hold 256 values, so 256 would match every photo.
_DIAGRAM_COLORS = 256
_DIAGRAM_COLORS_GRAY = 64

# meta.json is read-modify-written from a thread pool (several images can be
# stored concurrently). Re-entrant so the internal helpers can compose.
_LOCK = threading.RLock()
# Names handed out by unique_asset_name but not yet written by store_asset.
# In-process only, which is all we need: one uvicorn process owns DATA_DIR.
_reserved_stems: dict[str, set[str]] = {}


class BookError(Exception):
    """Bad caller input or missing book/asset. Message is written for an LLM."""


# --------------------------------------------------------------------------
# paths & ids
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _reject_nul(value: str, field: str) -> str:
    if "\x00" in value:
        raise BookError(f"{field} contains a NUL byte; pass plain text")
    return value


def _guard(child: Path, parent: Path) -> Path:
    """Resolve ``child`` and verify it stays under ``parent`` (symlinks included)."""
    try:
        resolved = child.resolve()
        base = parent.resolve()
    except (OSError, ValueError) as e:
        raise BookError(f"Invalid path {str(child)[:120]!r}: {e}") from e
    if resolved != base and base not in resolved.parents:
        raise BookError(
            f"Path {str(child)[:120]!r} escapes {base}. Pass a bare name with no "
            f"'/', '\\' or '..' components."
        )
    return resolved


def slugify(text: str) -> str:
    """Lowercase ascii ``[a-z0-9-]`` slug, max 24 chars. Falls back to "book"."""
    text = _reject_nul(text or "", "title")
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:MAX_SLUG_LEN].strip("-")
    return slug or "book"


def _validate_book_id(book_id: str) -> str:
    book_id = _reject_nul(str(book_id or "").strip(), "book_id")
    if not BOOK_ID_RE.match(book_id):
        raise BookError(
            f"Invalid book_id {book_id[:80]!r}. Expected the '<slug>-<12 hex>' id returned "
            f"by create_book (e.g. 'quantum-notes-9f2a1c0b4d7e'). Call list_books() to see "
            f"the existing ids."
        )
    return book_id


def book_dir(book_id: str) -> Path:
    """Path of an existing book directory (traversal-guarded)."""
    book_id = _validate_book_id(book_id)
    path = _guard(BOOKS_DIR / book_id, BOOKS_DIR)
    if not path.is_dir():
        raise BookError(
            f"No book with id {book_id!r}. Call list_books() to see existing books, or "
            f"create_book(title=...) to start a new one."
        )
    return path


def assets_dir(book_id: str) -> Path:
    path = book_dir(book_id) / "assets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def out_dir(book_id: str) -> Path:
    path = book_dir(book_id) / "out"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _doc_path(book_id: str) -> Path:
    return book_dir(book_id) / "doc.html"


def public_asset_url(book_id: str, filename: str) -> str | None:
    """Public HTTPS URL of an asset, or None if no public base is configured.

    MCP_PUBLIC_ASSET_BASE_URL wins; otherwise MCP_PUBLIC_BASE_URL + the server's
    ``/<mcp-name>/assets`` route (``/kindle/assets`` by default).
    """
    base = (os.getenv("MCP_PUBLIC_ASSET_BASE_URL") or "").rstrip("/")
    if not base:
        root = (os.getenv("MCP_PUBLIC_BASE_URL") or "").rstrip("/")
        if not root:
            return None
        name = re.sub(r"[^a-z0-9_-]", "-", os.getenv("MCP_NAME", "kindle").lower()).strip("-")
        base = f"{root}/{name or 'kindle'}/assets"
    return f"{base}/{book_id}/{filename}"


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

def _default_meta(book_id: str) -> dict:
    now = _now_iso()
    return {
        "book_id": book_id,
        "title": book_id.rsplit("-", 1)[0].replace("-", " ") or book_id,
        "created": now,
        "updated": now,
        "doc_chars": 0,
        "asset_count": 0,
        "notes": "",
    }


def load_meta(book_id: str) -> dict:
    """Read meta.json, filling in any missing keys. Never raises on a bad file."""
    path = book_dir(book_id) / "meta.json"
    meta: dict = {}
    with _LOCK:
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
                else:
                    logger.warning("meta.json for %s is not an object; rebuilding", book_id)
            except (OSError, ValueError) as e:
                logger.warning("Unreadable meta.json for %s (%s); rebuilding", book_id, e)
        else:
            logger.warning("Missing meta.json for %s; rebuilding", book_id)
    merged = _default_meta(book_id)
    merged.update({k: v for k, v in meta.items() if v is not None})
    merged["book_id"] = book_id  # the directory name is the authority
    return merged


def save_meta(meta: dict) -> None:
    """Write meta.json atomically. ``meta`` must carry a valid ``book_id``."""
    if not isinstance(meta, dict) or not meta.get("book_id"):
        raise BookError("save_meta needs a dict containing 'book_id' (as returned by load_meta)")
    book_id = _validate_book_id(str(meta["book_id"]))
    path = book_dir(book_id) / "meta.json"
    payload = json.dumps(meta, ensure_ascii=False, indent=2)
    with _LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)  # atomic: readers never see a half-written file


def _touch_meta(book_id: str, **fields) -> dict:
    """Read-modify-write meta.json under the lock, refreshing ``updated``."""
    with _LOCK:
        meta = load_meta(book_id)
        meta.update(fields)
        meta["updated"] = _now_iso()
        save_meta(meta)
        return meta


def _count_assets(book_id: str) -> int:
    adir = book_dir(book_id) / "assets"
    if not adir.is_dir():
        return 0
    return sum(1 for p in adir.iterdir() if p.is_file())


# --------------------------------------------------------------------------
# books
# --------------------------------------------------------------------------

def create_book(title: str, notes: str = "") -> dict:
    """Create an empty book workspace and return its meta dict."""
    title = _reject_nul((title or "").strip(), "title")
    if not title:
        raise BookError(
            "create_book requires a non-empty title — it becomes the EPUB title and the "
            "book_id slug. Example: create_book(title='Kalman Filters, Explained')."
        )
    notes = _reject_nul(str(notes or ""), "notes")

    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(title)
    with _LOCK:
        for _ in range(5):
            book_id = f"{slug}-{secrets.token_hex(6)}"
            path = _guard(BOOKS_DIR / book_id, BOOKS_DIR)
            if not path.exists():
                break
        else:  # pragma: no cover - 5 collisions on 48 random bits is impossible
            raise BookError("Could not allocate a unique book_id; retry")
        (path / "assets").mkdir(parents=True, exist_ok=True)
        (path / "out").mkdir(parents=True, exist_ok=True)
        meta = _default_meta(book_id)
        meta["title"] = title[:300]
        meta["notes"] = notes[:2000]
        save_meta(meta)
    logger.info("Created book %s (%r)", book_id, meta["title"])
    return meta


def list_books(limit: int = 50) -> list[dict]:
    """Meta dicts of all books, newest ``updated`` first."""
    limit = max(1, min(int(limit or 50), 500))
    if not BOOKS_DIR.is_dir():
        return []
    metas: list[dict] = []
    for path in BOOKS_DIR.iterdir():
        if not path.is_dir() or not BOOK_ID_RE.match(path.name):
            continue
        try:
            metas.append(load_meta(path.name))
        except BookError as e:  # a directory that vanished mid-scan
            logger.warning("Skipping book %s: %s", path.name, e)
    metas.sort(key=lambda m: str(m.get("updated") or m.get("created") or ""), reverse=True)
    return metas[:limit]


def delete_book(book_id: str) -> None:
    path = book_dir(book_id)
    with _LOCK:
        shutil.rmtree(path)
        _reserved_stems.pop(path.name, None)
    logger.info("Deleted book %s", path.name)


def prune(max_age_days: int) -> int:
    """Delete books not updated within ``max_age_days``. Returns the count.

    ``max_age_days <= 0`` disables pruning (returns 0) so a misconfigured
    retention value can never wipe the workspace.
    """
    if max_age_days <= 0 or not BOOKS_DIR.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    removed = 0
    for path in list(BOOKS_DIR.iterdir()):
        if not path.is_dir() or not BOOK_ID_RE.match(path.name):
            continue
        try:
            stamp = str(load_meta(path.name).get("updated") or "")
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (BookError, ValueError):
            when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if when < cutoff:
            try:
                with _LOCK:
                    shutil.rmtree(path)
                    _reserved_stems.pop(path.name, None)
                removed += 1
            except OSError as e:
                logger.warning("prune: could not remove %s: %s", path.name, e)
    if removed:
        logger.info("prune: removed %d book(s) older than %d days", removed, max_age_days)
    return removed


# --------------------------------------------------------------------------
# assets
# --------------------------------------------------------------------------

def _sanitise_stem(base: str) -> str:
    """Reduce a caller-supplied name to a safe ``[a-z0-9_-]`` stem."""
    base = _reject_nul(str(base or ""), "asset name")
    base = Path(base).name  # drop any directory part the caller tacked on
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-_")
    stem = re.sub(r"-{2,}", "-", stem)[:MAX_STEM_LEN].strip("-_")
    return stem or "image"


def _normalise_ext(ext: str | None, mime: str | None = None) -> str:
    """Return an allowed ``.ext``, preferring the explicit one, then the mime."""
    if ext:
        candidate = ext if ext.startswith(".") else f".{ext}"
        candidate = candidate.lower()
        if candidate in ALLOWED_EXT:
            return candidate
        if not mime:
            raise BookError(
                f"Unsupported image extension {candidate!r}. Allowed: "
                f"{', '.join(sorted(ALLOWED_EXT))}. Convert the image first (SVG must be "
                f"rasterised to PNG)."
            )
    from_mime = _MIME_TO_EXT.get((mime or "").lower())
    if from_mime:
        return from_mime
    raise BookError(
        f"Cannot determine an image extension from ext={ext!r} mime={mime!r}. Allowed: "
        f"{', '.join(sorted(ALLOWED_EXT))}."
    )


def unique_asset_name(book_id: str, base: str, ext: str) -> str:
    """Free filename for ``base``+``ext`` in the book; appends -2, -3, … if taken.

    Uniqueness is by stem, not full name, because :func:`store_asset` may switch
    the extension when it re-encodes (PNG line art vs JPEG photo).
    """
    adir = assets_dir(book_id)
    ext = _normalise_ext(ext)
    stem = _sanitise_stem(base)
    with _LOCK:
        taken = _reserved_stems.setdefault(book_id, set())
        n = 1
        while True:
            candidate = stem if n == 1 else f"{stem}-{n}"
            if candidate not in taken and not any(adir.glob(f"{candidate}.*")):
                taken.add(candidate)
                return f"{candidate}{ext}"
            n += 1


def _flatten_on_white(im: PILImage.Image) -> PILImage.Image:
    """Composite alpha onto white — a plain RGBA->L conversion ignores alpha and
    renders transparent regions with their (usually black) underlying RGB."""
    rgba = im.convert("RGBA")
    background = PILImage.new("RGBA", rgba.size, (255, 255, 255, 255))
    return PILImage.alpha_composite(background, rgba).convert("RGB")


def _looks_like_diagram(im: PILImage.Image, src_format: str) -> bool:
    """True for flat-colour PNG line art, which JPEG would smear with ringing."""
    if src_format != "PNG":
        return False
    limit = _DIAGRAM_COLORS_GRAY if im.mode in ("1", "L", "I;16") else _DIAGRAM_COLORS
    try:
        return im.getcolors(maxcolors=limit) is not None
    except Exception:
        return False


def store_asset(
    book_id: str,
    name: str,
    data: bytes,
    *,
    grayscale: bool = True,
    max_px: int = 1400,
    mime: str | None = None,
) -> dict:
    """Normalise image bytes for e-ink and save them as a book asset.

    Every image producer (Gemini, URL fetch, PlantUML/d2, matplotlib) goes
    through here so normalisation lives in exactly one place: optional grayscale,
    downscale to ``max_px`` on the long side, then PNG (transparency or flat
    line art) or JPEG q85 (everything else). Overwrites any asset with the same
    stem, including one saved under a different extension.

    Returns ``{"filename", "bytes", "width", "height", "url"}``; ``url`` is the
    public asset URL, or None when no public base URL is configured.
    """
    adir = assets_dir(book_id)
    if not data:
        raise BookError(f"store_asset got 0 bytes for {name!r}; pass the actual image bytes")

    try:
        im = PILImage.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise BookError(
            f"Could not decode {len(data)} bytes as an image ({e}). Supported: PNG, JPEG, "
            f"GIF, WEBP. If this came from a URL, the server probably returned an HTML "
            f"error page instead of the image."
        ) from e

    src_format = (im.format or "").upper()
    has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    diagram_like = _looks_like_diagram(im, src_format)
    if getattr(im, "n_frames", 1) > 1:
        logger.info("store_asset: %s is animated; keeping the first frame only", name)

    if grayscale:
        im = _flatten_on_white(im) if has_alpha else im.convert("RGB") if im.mode == "P" else im
        im = im.convert("L")
    elif im.mode not in ("RGB", "L", "RGBA", "LA"):
        im = im.convert("RGBA" if has_alpha else "RGB")

    max_px = max(64, min(int(max_px or 1400), 4096))
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), PILImage.Resampling.LANCZOS)

    use_png = has_alpha or diagram_like
    ext = ".png" if use_png else ".jpg"
    stem = _sanitise_stem(name)
    target = _guard(adir / f"{stem}{ext}", adir)

    buf = io.BytesIO()
    if use_png:
        im.save(buf, format="PNG", optimize=True)
    else:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(buf, format="JPEG", quality=85, optimize=True)
    payload = buf.getvalue()

    with _LOCK:
        # One asset per stem: drop a previous encoding under another extension.
        for stale in adir.glob(f"{stem}.*"):
            if stale != target and stale.is_file():
                stale.unlink()
        target.write_bytes(payload)
        _reserved_stems.setdefault(book_id, set()).add(stem)
        _touch_meta(book_id, asset_count=_count_assets(book_id))

    logger.info(
        "store_asset %s/%s: %dx%d %s %d bytes (grayscale=%s)",
        book_id, target.name, im.size[0], im.size[1], ext[1:], len(payload), grayscale,
    )
    return {
        "filename": target.name,
        "bytes": len(payload),
        "width": im.size[0],
        "height": im.size[1],
        "url": public_asset_url(book_id, target.name),
    }


def asset_ref(book_id: str, filename: str) -> Path:
    """Traversal-guarded path of an existing asset."""
    adir = assets_dir(book_id)
    filename = _reject_nul(str(filename or "").strip(), "filename")
    if not filename or filename != Path(filename).name:
        raise BookError(
            f"Asset filename {filename[:80]!r} must be a bare name with no path "
            f"components (e.g. 'cover.jpg')."
        )
    path = _guard(adir / filename, adir)
    if not path.is_file():
        available = sorted(p.name for p in adir.iterdir() if p.is_file())[:20]
        raise BookError(
            f"Book {book_id} has no asset {filename!r}. Available: "
            f"{', '.join(available) or '(none)'}. Add one with add_image/fetch_image, or "
            f"call list_assets({book_id!r})."
        )
    return path


def list_assets(book_id: str) -> list[dict]:
    """All assets of a book: filename, bytes, width, height, url."""
    adir = assets_dir(book_id)
    items: list[dict] = []
    for path in sorted(adir.iterdir()):
        if not path.is_file():
            continue
        width = height = None
        try:
            with PILImage.open(path) as im:  # header read only, no full decode
                width, height = im.size
        except Exception as e:
            logger.warning("list_assets: %s is not a readable image (%s)", path.name, e)
        items.append({
            "filename": path.name,
            "bytes": path.stat().st_size,
            "width": width,
            "height": height,
            "url": public_asset_url(book_id, path.name),
        })
    return items


def delete_asset(book_id: str, filename: str) -> None:
    path = asset_ref(book_id, filename)
    with _LOCK:
        path.unlink()
        _reserved_stems.get(book_id, set()).discard(path.stem)
        _touch_meta(book_id, asset_count=_count_assets(book_id))
    logger.info("Deleted asset %s/%s", book_id, path.name)


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------

def set_document(book_id: str, html: str) -> dict:
    """Replace the book's HTML document. Returns ``{"book_id","doc_chars","updated"}``."""
    path = _doc_path(book_id)
    html = _reject_nul(html or "", "html")
    if not html.strip():
        raise BookError(
            "set_document got empty html. Pass the document body (fragments are fine); "
            "chapters split at every <h1>."
        )
    with _LOCK:
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(path)
        meta = _touch_meta(book_id, doc_chars=len(html))
    logger.info("set_document %s: %d chars", book_id, len(html))
    return {"book_id": book_id, "doc_chars": len(html), "updated": meta["updated"]}


def get_document(book_id: str) -> str:
    path = _doc_path(book_id)
    if not path.is_file():
        raise BookError(
            f"Book {book_id} has no document yet. Call set_document(book_id=..., html=...) "
            f"before reading, patching, previewing or sending it."
        )
    return path.read_text(encoding="utf-8")


def patch_document(book_id: str, find: str, replace: str, count: int = 1) -> dict:
    """Literal (non-regex) find/replace on the stored document.

    ``count`` is how many occurrences the caller expects to change; ``count <= 0``
    means all of them. A mismatch is an error rather than a silent partial edit —
    the message reports the real occurrence count so the caller can lengthen the
    anchor or pass the right count.

    Returns ``{"replacements", "doc_chars"}``.
    """
    find = _reject_nul(find or "", "find")
    replace = _reject_nul(replace or "", "replace")
    if not find:
        raise BookError(
            "patch_document needs a non-empty 'find' anchor — the exact literal text to "
            "replace (no regex)."
        )
    # Read AND write under the lock: two parallel patches on one book would
    # otherwise both edit the same snapshot and the later write would silently
    # drop the earlier edit while still reporting success.
    with _LOCK:
        doc = get_document(book_id)
        occurrences = doc.count(find)
        if occurrences == 0:
            raise BookError(
                f"Anchor not found in book {book_id} (0 occurrences of {find[:80]!r}). It must "
                f"match the stored HTML exactly, including whitespace and entity escaping. Call "
                f"get_document to read the current text, then retry with a shorter, distinctive "
                f"anchor."
            )
        if count > 0 and occurrences > count:
            raise BookError(
                f"Anchor {find[:80]!r} occurs {occurrences} times in book {book_id} but count="
                f"{count} was requested. Refusing a partial edit: lengthen the anchor to make it "
                f"unique, or pass count={occurrences} (or count=0) to replace all of them."
            )

        updated = doc.replace(find, replace) if count <= 0 else doc.replace(find, replace, count)
        replacements = occurrences if count <= 0 else min(occurrences, count)
        path = _doc_path(book_id)
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(path)
        _touch_meta(book_id, doc_chars=len(updated))
    logger.info("patch_document %s: %d replacement(s)", book_id, replacements)
    return {"replacements": replacements, "doc_chars": len(updated)}
