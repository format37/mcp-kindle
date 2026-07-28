"""Rasterise a BUILT EPUB into page images, plus cheap static lint over it.

Why the *built* EPUB and never the source HTML: the preview exists to validate
the artifact that is actually mailed to the Kindle — image items really
embedded, spine/reading order, chapter splits, XHTML escaping. An HTML-stage
render would happily show a diagram that never made it into the zip.

Pipeline::

    epub -> unzip -> META-INF/container.xml -> .opf -> <spine> order
         -> one concatenated HTML doc -> WeasyPrint -> PDF
         -> pdftoppm -> downscaled grayscale JPEGs

The MCP layer returns those JPEGs as inline image content blocks, which is how
a phone-side model gets to *see* the book before it is sent.

External dependencies (both optional — preview is a nice-to-have, sending is
not):

    weasyprint  (python package + libpango/libcairo + at least one installed
                 font family; with no fonts the PDF renders blank)
    pdftoppm / pdfinfo   (poppler-utils)

If either is missing, every entry point raises :class:`PreviewError` naming the
missing piece and stating that sending to the Kindle still works.
"""

from __future__ import annotations

import logging
import math
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# EPUB content documents are XHTML, and we parse them with the lenient HTML
# parser on purpose (a real book routinely contains markup an XML parser would
# reject outright — and a preview must still show what the reader would see).
# bs4 warns about exactly that on every chapter; silence just that warning.
try:
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:  # bs4 < 4.11
    pass

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))


def _env_int(name: str, default: int) -> int:
    """Env int that degrades to the default. A typo in .env must not take down
    the whole server: main.py imports this module at startup, so raising here
    would break sending too — and sending works without preview."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


# Hard cap on inline preview pages: each page costs ~1.2k tokens in the model's
# context, so an unbounded "all" on a 200-page book would blow the window.
PREVIEW_MAX_PAGES = _env_int("PREVIEW_MAX_PAGES", 20)
PREVIEW_TIMEOUT = _env_int("PREVIEW_TIMEOUT", 180)
# Zip-bomb guard for the unzip step.
MAX_UNZIP_BYTES = _env_int("PREVIEW_MAX_UNZIP_BYTES", 256 * 1024 * 1024)

# Lint thresholds.
MAX_EPUB_BYTES = _env_int("PREVIEW_MAX_EPUB_BYTES", 20 * 1024 * 1024)
MAX_IMAGE_BYTES = _env_int("PREVIEW_MAX_IMAGE_BYTES", 1_500_000)
MIN_CHAPTER_CHARS = 20
MAX_TABLE_COLS = 8
MAX_FINDINGS = 40

XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html", "application/xml", "text/xml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"}

# Blocks the content pipeline is supposed to have consumed. Anything still
# carrying these classes reached the Kindle as raw source text. The tag sets
# mirror what each renderer actually consumes (diagrams.py: <pre>/<code>;
# mathrender.py: div/pre/span/code), so a stray class="dot" on a <div> is not
# mistaken for a Graphviz block.
DIAGRAM_CLASSES = {"plantuml", "uml", "puml", "graphviz", "dot", "d2", "mermaid"}
DIAGRAM_TAGS = ["pre", "code"]
MATH_CLASSES = {"math", "latex"}
MATH_TAGS = ["div", "pre", "span", "code"]

# Approximate a 6" e-reader page so pagination is at least e-reader-shaped.
PREVIEW_PAGE_CSS = """
@page { size: 90mm 120mm; margin: 6mm; }
html { margin: 0; padding: 0; }
body { margin: 0; padding: 0; font-size: 9pt; line-height: 1.45;
       word-wrap: break-word; }
h1 { font-size: 1.5em; } h2 { font-size: 1.25em; } h3 { font-size: 1.1em; }
/* Height cap as well as width: a tall diagram constrained only by width grows
   past the page and splits across two of them, which makes the preview look
   broken and hides what follows. An e-reader fits an oversized image to the
   screen; this mirrors that. 100mm = 120mm page - 12mm margins - room for a
   caption line. */
img, svg { max-width: 100%; max-height: 100mm; height: auto; }
table { width: 100%; table-layout: fixed; border-collapse: collapse;
        font-size: 0.8em; word-wrap: break-word; }
th, td { padding: 0.2em 0.3em; }
pre, code { white-space: pre-wrap; word-wrap: break-word; font-size: 0.78em; }
.kindle-preview-chapter { break-before: page; page-break-before: always; }
.kindle-preview-chapter:first-child { break-before: auto; page-break-before: avoid; }
"""


class PreviewError(RuntimeError):
    """Preview could not be produced (missing tool, unreadable EPUB, bad range)."""


# --------------------------------------------------------------------------- #
# Path guards
# --------------------------------------------------------------------------- #

def _allowed_roots() -> list[Path]:
    """Directories preview is allowed to read from / write into.

    DATA_DIR is the bind-mounted workspace; the system temp dir is included
    because the send pipeline builds its EPUB with :mod:`tempfile` before this
    module ever sees it.
    """
    roots: list[Path] = []
    for cand in (DATA_DIR, Path(tempfile.gettempdir())):
        try:
            roots.append(cand.resolve())
        except OSError:
            continue
    return roots


def _safe_path(p: str | Path, what: str) -> Path:
    """resolve() then confirm the result stays under an allowed root."""
    raw = str(p)
    if "\x00" in raw:
        raise PreviewError(f"{what} contains a NUL byte; pass a plain file path.")
    if not raw.strip():
        raise PreviewError(f"{what} is empty; pass a file path under {DATA_DIR}.")
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, ValueError) as e:
        raise PreviewError(f"{what} {raw!r} is not a usable path: {e}") from e
    roots = _allowed_roots()
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    allowed = ", ".join(str(r) for r in roots)
    raise PreviewError(
        f"{what} resolves to {resolved}, which is outside the allowed "
        f"directories ({allowed}). Put the file under {DATA_DIR} and retry."
    )


# --------------------------------------------------------------------------- #
# Dependency checks
# --------------------------------------------------------------------------- #

_SEND_STILL_WORKS = (
    "Preview is unavailable, but building and sending the book to the Kindle "
    "still works — skip the preview or ask the operator to install it."
)


def _weasyprint_html():
    """Import WeasyPrint lazily; a missing/broken install must not kill imports."""
    try:
        from weasyprint import HTML  # noqa: PLC0415 - deliberate lazy import
    except Exception as e:  # ImportError, or OSError from missing libpango/cairo
        raise PreviewError(
            f"WeasyPrint is not usable in this container ({e.__class__.__name__}: {e}). "
            f"It needs the 'weasyprint' package plus libpango/libcairo and at least "
            f"one font family. {_SEND_STILL_WORKS}"
        ) from e
    return HTML


def _require_binary(name: str, package: str = "poppler-utils") -> str:
    path = shutil.which(name)
    if not path:
        raise PreviewError(
            f"'{name}' is not installed in this container (provided by the "
            f"{package} package). {_SEND_STILL_WORKS}"
        )
    return path


def preview_available() -> tuple[bool, str]:
    """(usable, reason) — real dependency probe for health checks and tool text.

    Reports the actual missing component instead of claiming preview works and
    failing at call time.
    """
    problems: list[str] = []
    try:
        _weasyprint_html()
    except PreviewError as e:
        problems.append(str(e))
    for binary in ("pdftoppm", "pdfinfo"):
        try:
            _require_binary(binary)
        except PreviewError as e:
            problems.append(str(e))
    if problems:
        return False, " ".join(problems)
    return True, "preview available (weasyprint + poppler-utils present)"


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd, capture_output=True, check=False, timeout=PREVIEW_TIMEOUT
        )
    except FileNotFoundError as e:
        raise PreviewError(
            f"'{cmd[0]}' is not installed in this container (poppler-utils). "
            f"{_SEND_STILL_WORKS}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise PreviewError(
            f"{what} timed out after {PREVIEW_TIMEOUT}s. Preview fewer pages "
            f"(e.g. pages='1-4') or raise PREVIEW_TIMEOUT."
        ) from e
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")[:400]
        raise PreviewError(f"{what} failed (rc={result.returncode}): {err or 'no stderr'}")
    return result


# --------------------------------------------------------------------------- #
# EPUB structure
# --------------------------------------------------------------------------- #

CONTAINER_PATH = "META-INF/container.xml"


@dataclass
class _Structure:
    """Reading-order view of an EPUB, all hrefs normalised to zip paths."""

    opf_href: str
    title: str = "Preview"
    spine: list[str] = field(default_factory=list)      # xhtml docs, reading order
    css: list[str] = field(default_factory=list)
    images: list[tuple[str, str]] = field(default_factory=list)  # (href, media_type)
    nav_href: str | None = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _findall(root: ET.Element, name: str) -> list[ET.Element]:
    """Namespace-agnostic descendant lookup (EPUB namespaces vary by producer)."""
    return [e for e in root.iter() if _local(e.tag) == name]


def _zip_read(zf: zipfile.ZipFile, name: str, what: str) -> bytes:
    try:
        return zf.read(name)
    except KeyError as e:
        raise PreviewError(
            f"EPUB is malformed: {what} ({name}) is missing from the archive."
        ) from e


def _xml(data: bytes, what: str) -> ET.Element:
    # stdlib ElementTree, not lxml: it does not expand external entities, so a
    # hostile EPUB cannot turn XML parsing into a file read.
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise PreviewError(f"EPUB is malformed: {what} is not valid XML ({e}).") from e


def _norm(href: str, base_dir: str = "") -> str:
    """Normalise an OPF/XHTML href into a zip member path."""
    path = unquote(href.split("#", 1)[0].split("?", 1)[0]).strip()
    if path.startswith("/"):
        return posixpath.normpath(path.lstrip("/"))
    return posixpath.normpath(posixpath.join(base_dir, path)) if base_dir else posixpath.normpath(path)


def _parse_structure(zf: zipfile.ZipFile) -> _Structure:
    """container.xml -> OPF -> manifest + spine, in reading order, nav excluded."""
    container = _xml(_zip_read(zf, CONTAINER_PATH, "container.xml"), "container.xml")
    rootfiles = _findall(container, "rootfile")
    opf_href = next(
        (rf.get("full-path") for rf in rootfiles if rf.get("full-path")), None
    )
    if not opf_href:
        raise PreviewError(
            "EPUB is malformed: META-INF/container.xml declares no <rootfile full-path>."
        )
    opf_href = _norm(opf_href)
    opf_dir = posixpath.dirname(opf_href)
    opf = _xml(_zip_read(zf, opf_href, "the OPF package document"), opf_href)

    struct = _Structure(opf_href=opf_href)
    titles = [t.text for t in _findall(opf, "title") if (t.text or "").strip()]
    if titles:
        struct.title = titles[0].strip()

    # id -> (zip href, media-type, properties)
    items: dict[str, tuple[str, str, str]] = {}
    for item in _findall(opf, "item"):
        item_id = item.get("id")
        href = item.get("href")
        if not item_id or not href:
            continue
        media = (item.get("media-type") or "").lower()
        props = (item.get("properties") or "").lower()
        zip_href = _norm(href, opf_dir)
        items[item_id] = (zip_href, media, props)
        if "nav" in props.split() and struct.nav_href is None:
            struct.nav_href = zip_href
        if media == "text/css":
            struct.css.append(zip_href)
        elif media.startswith("image/"):
            struct.images.append((zip_href, media))

    if struct.nav_href is None:
        # EPUB 2 producers ship no nav property; fall back to the conventional name.
        struct.nav_href = next(
            (h for h, m, _ in items.values()
             if m in XHTML_MEDIA_TYPES and posixpath.basename(h) in ("nav.xhtml", "toc.xhtml")),
            None,
        )

    spines = _findall(opf, "spine")
    if not spines:
        raise PreviewError("EPUB is malformed: the OPF has no <spine>, so it has no reading order.")
    for itemref in _findall(spines[0], "itemref"):
        idref = itemref.get("idref")
        if not idref or idref not in items:
            continue
        href, media, _props = items[idref]
        if href == struct.nav_href:
            continue  # the ToC is navigation, not content
        if media and media not in XHTML_MEDIA_TYPES:
            continue
        struct.spine.append(href)

    if not struct.spine:
        raise PreviewError(
            "EPUB has no readable content documents in its spine — the build "
            "produced an empty book. Check that the HTML had body content."
        )
    return struct


def _decode(data: bytes) -> tuple[str, bool]:
    """(text, was_valid_utf8). EPUB content documents must be UTF-8."""
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), False


def _extract_all(zf: zipfile.ZipFile, root: Path) -> None:
    """Unzip with a zip-slip guard and a total-size cap."""
    root_resolved = root.resolve()
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if "\x00" in name or name.startswith("/") or ".." in name.split("/"):
            logger.warning("Skipping unsafe EPUB member %r", name)
            continue
        target = (root / name).resolve()
        if root_resolved not in target.parents:
            logger.warning("Skipping EPUB member escaping the unzip root: %r", name)
            continue
        total += info.file_size
        if total > MAX_UNZIP_BYTES:
            raise PreviewError(
                f"EPUB expands to more than {MAX_UNZIP_BYTES // (1024 * 1024)} MB; "
                f"refusing to unzip it. Shrink the images and rebuild."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 64)


def _resolve_ref(base_dir: str, url: str) -> str | None:
    """Resolve a document-relative URL to a root-relative zip path, or None.

    None means "leave it alone": empty, fragment-only, data: URI, or absolute
    URL — plus anything that would climb out of the EPUB root.
    """
    u = (url or "").strip()
    if not u or u.startswith("#"):
        return None
    parsed = urlsplit(u)
    if parsed.scheme:  # http:, https:, data:, mailto:, file:
        return None
    if not parsed.path:
        return None
    joined = _norm(parsed.path, base_dir)
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return joined


_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)


def _rewrite_css_urls(css_text: str, base_dir: str) -> str:
    def sub(m: re.Match) -> str:
        target = _resolve_ref(base_dir, m.group(2))
        return m.group(0) if target is None else f"url('{quote(target)}')"

    return _CSS_URL_RE.sub(sub, css_text)


# Attributes carrying resource URLs that must survive the flattening.
_REF_ATTRS = (("src",), ("xlink:href",), ("poster",))


def _rewrite_refs(soup: BeautifulSoup, base_dir: str) -> None:
    """Rewrite relative resource URLs so they resolve against the unzip root."""
    for tag in soup.find_all(True):
        for attrs in _REF_ATTRS:
            for attr in attrs:
                val = tag.get(attr)
                if not isinstance(val, str):
                    continue
                target = _resolve_ref(base_dir, val)
                if target is not None:
                    tag[attr] = quote(target)
        # <image href=...> inside SVG; plain <a href> is left as-is (dead links
        # in a throwaway preview PDF are harmless).
        if tag.name in ("image", "use"):
            val = tag.get("href")
            if isinstance(val, str):
                target = _resolve_ref(base_dir, val)
                if target is not None:
                    tag["href"] = quote(target)


# --------------------------------------------------------------------------- #
# EPUB -> PDF
# --------------------------------------------------------------------------- #

def _build_preview_html(root: Path, struct: _Structure) -> str:
    """Concatenate spine documents (reading order) into one HTML document."""
    epub_css: list[str] = []
    for css_href in struct.css:
        css_file = root / css_href
        if not css_file.is_file():
            continue
        text, _ok = _decode(css_file.read_bytes())
        epub_css.append(_rewrite_css_urls(text, posixpath.dirname(css_href)))

    bodies: list[str] = []
    for href in struct.spine:
        doc = root / href
        if not doc.is_file():
            logger.warning("Spine item %s missing from the archive, skipping", href)
            continue
        text, _ok = _decode(doc.read_bytes())
        soup = BeautifulSoup(text, "lxml")
        base_dir = posixpath.dirname(href)
        # Head-level <style> would be lost when we keep only the body; hoist it.
        # <style> inside an <svg> is left in place — it styles the diagram.
        if soup.head:
            for style in soup.head.find_all("style", recursive=False):
                epub_css.append(_rewrite_css_urls(style.get_text(), base_dir))
                style.decompose()
        _rewrite_refs(soup, base_dir)
        body = soup.body
        inner = body.decode_contents() if body else soup.decode_contents()
        bodies.append(f'<div class="kindle-preview-chapter">{inner}</div>')

    title = (
        struct.title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>\n"
        f"<style>{''.join(epub_css)}</style>\n"
        f"<style>{PREVIEW_PAGE_CSS}</style>\n"
        "</head><body>\n" + "\n".join(bodies) + "\n</body></html>"
    )


def epub_to_pdf(epub_path: str | Path, out_pdf: str | Path) -> Path:
    """Render a BUILT EPUB to a paginated PDF and return the PDF path.

    The EPUB is unzipped to a temp dir and walked properly:
    ``META-INF/container.xml`` -> the ``.opf`` -> the ``<spine>`` itemref order
    -> manifest hrefs, so chapters render in READING ORDER (globbing ``*.xhtml``
    both misorders chapters and drags in the nav document). The nav document is
    skipped, chapter bodies are concatenated into one document with a page break
    between them, the EPUB's own stylesheet is inlined, and WeasyPrint renders
    with ``base_url`` set to the unzip root so relative image/CSS hrefs resolve.

    Page box is 90mm x 120mm with 6mm margins — A6-ish, close to a 6" Kindle.
    This APPROXIMATES the content and its pagination; it is NOT Kindle's
    renderer. Use it to check that equations, diagrams, tables and escaping came
    out right, not to predict exact page breaks. MathML is not rendered by
    WeasyPrint — math must already be an image in the EPUB.

    Raises PreviewError if WeasyPrint is unusable or the EPUB is malformed.
    """
    src = _safe_path(epub_path, "epub_path")
    dst = _safe_path(out_pdf, "out_pdf")
    if not src.is_file():
        raise PreviewError(f"EPUB not found at {src}. Build the book before previewing it.")
    html_cls = _weasyprint_html()
    dst.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="epub_preview_") as tmp:
        root = Path(tmp)
        try:
            with zipfile.ZipFile(src) as zf:
                struct = _parse_structure(zf)
                _extract_all(zf, root)
        except zipfile.BadZipFile as e:
            raise PreviewError(
                f"{src.name} is not a valid EPUB (zip) file: {e}. Rebuild the book."
            ) from e
        document = _build_preview_html(root, struct)
        # base_url is only used to join relative URLs; the file itself need not exist.
        base_url = (root / "_preview.html").as_uri()
        try:
            html_cls(string=document, base_url=base_url).write_pdf(str(dst))
        except Exception as e:
            raise PreviewError(
                f"WeasyPrint failed to render {src.name} ({e.__class__.__name__}: {e}). "
                f"This usually means malformed markup survived into the EPUB."
            ) from e

    if not dst.is_file() or dst.stat().st_size == 0:
        raise PreviewError(f"WeasyPrint produced no output at {dst}.")
    logger.info("Rendered %s -> %s (%d bytes)", src.name, dst, dst.stat().st_size)
    return dst


# --------------------------------------------------------------------------- #
# PDF -> images
# --------------------------------------------------------------------------- #

def _pdfinfo(pdf: Path) -> dict[str, float]:
    """Best-effort pdfinfo scrape: {'pages', 'width_pts', 'height_pts'}."""
    if not shutil.which("pdfinfo"):
        return {}
    try:
        result = _run(["pdfinfo", str(pdf)], "pdfinfo")
    except PreviewError as e:
        logger.warning("pdfinfo failed on %s: %s", pdf, e)
        return {}
    text = result.stdout.decode("utf-8", errors="replace")
    out: dict[str, float] = {}
    m = re.search(r"^Pages:\s+(\d+)", text, re.MULTILINE)
    if m:
        out["pages"] = float(m.group(1))
    m = re.search(r"^Page size:\s+([\d.]+)\s*x\s*([\d.]+)\s*pts", text, re.MULTILINE)
    if m:
        out["width_pts"] = float(m.group(1))
        out["height_pts"] = float(m.group(2))
    return out


def page_count(pdf: str | Path) -> int:
    """Number of pages in the PDF (pdfinfo, falling back to a cheap render)."""
    path = _safe_path(pdf, "pdf")
    if not path.is_file():
        raise PreviewError(f"PDF not found at {path}. Run epub_to_pdf first.")
    info = _pdfinfo(path)
    if "pages" in info:
        return int(info["pages"])
    # Fallback: rasterise at a throwaway resolution and count the files.
    _require_binary("pdftoppm")
    with tempfile.TemporaryDirectory(prefix="pdf_count_") as tmp:
        _run(["pdftoppm", "-jpeg", "-r", "10", str(path), str(Path(tmp) / "p")],
             "pdftoppm (page count)")
        n = len(list(Path(tmp).glob("p-*.jpg")))
    if n == 0:
        raise PreviewError(f"Could not determine the page count of {path.name}; it may be empty.")
    return n


def resolve_page_range(pages: str, total: int) -> tuple[int, int, str | None]:
    """Parse a ``pages`` spec against a real page count.

    Returns ``(first, last, note)``. ``note`` is non-None whenever the returned
    range is NARROWER than what was asked for (clamped to the document, or
    truncated at PREVIEW_MAX_PAGES) — surface it to the caller, otherwise
    "4 pages back" reads as "the book is 4 pages long".
    """
    if total < 1:
        raise PreviewError("The PDF has no pages.")
    spec = (pages or "").strip().lower() or "1-4"
    if spec == "all":
        first, last = 1, total
    elif re.fullmatch(r"\d+", spec):
        first = last = int(spec)
    elif re.fullmatch(r"\d+\s*-\s*\d*", spec):
        head, _, tail = spec.partition("-")
        first = int(head.strip())
        last = int(tail.strip()) if tail.strip() else total
    else:
        raise PreviewError(
            f"pages={pages!r} is not a valid range. Use 'all', a single page "
            f"like '3', or a range like '1-4'."
        )
    if first < 1:
        raise PreviewError(f"pages={pages!r} starts below page 1; pages are 1-indexed.")
    if first > total:
        raise PreviewError(
            f"pages={pages!r} starts at page {first} but the document has only "
            f"{total} page(s). Use 'all' or a range inside 1-{total}."
        )

    notes: list[str] = []
    if last > total:
        notes.append(f"the document ends at page {total}")
        last = total
    if last - first + 1 > PREVIEW_MAX_PAGES:
        last = first + PREVIEW_MAX_PAGES - 1
        notes.append(
            f"capped at PREVIEW_MAX_PAGES={PREVIEW_MAX_PAGES} to keep the inline "
            f"preview affordable"
        )
    note = None
    if notes:
        note = (
            f"Requested pages={pages!r}; returning pages {first}-{last} of {total} "
            f"({'; '.join(notes)}). The book is {total} pages long — this is NOT "
            f"the whole book."
        )
    return first, last, note


def _auto_dpi(pdf: Path, max_px: int) -> int:
    """DPI that makes the long side land near max_px (100 floor, 300 ceiling).

    At the A6-ish preview page size a flat 100 dpi yields ~354x472 px, which is
    too small to read the text the preview exists to check.
    """
    info = _pdfinfo(pdf)
    long_pts = max(info.get("width_pts", 0.0), info.get("height_pts", 0.0))
    if long_pts <= 0:
        return 100
    inches = long_pts / 72.0
    return max(100, min(300, int(math.ceil(max_px / inches))))


def _postprocess(src: Path, dst: Path, max_px: int) -> None:
    """Grayscale + downscale to max_px long side + JPEG q80 (~1.2k tokens/page)."""
    from PIL import Image  # noqa: PLC0415 - lazy, keeps import cost off the hot path

    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:  # Pillow < 10
        resample = Image.LANCZOS
    with Image.open(src) as im:
        im.load()
        page = im.convert("L")
    page.thumbnail((max_px, max_px), resample=resample)
    page.save(dst, format="JPEG", quality=80, optimize=True)


def pdf_to_images(
    pdf: str | Path,
    out_dir: str | Path,
    pages: str = "1-4",
    max_px: int = 1100,
    dpi: int | None = None,
) -> list[Path]:
    """Rasterise PDF pages to grayscale JPEGs, in page order.

    ``pages`` accepts ``"all"``, a single page (``"3"``) or a range (``"1-4"``).
    The range is clamped to the document and capped at PREVIEW_MAX_PAGES
    (default 20); when that narrows the request, a warning is logged — call
    :func:`resolve_page_range` yourself if you need that note as text to show
    the model, because a short list of pages otherwise reads as a short book.

    Output files are ``page_NNN.jpg`` named by TRUE page number. ``dpi`` defaults
    to whatever makes the long side ~``max_px``; images are then downscaled to
    ``max_px``, grayscaled and saved at JPEG q80 to keep the inline token cost
    around 1.2k tokens per page.
    """
    src = _safe_path(pdf, "pdf")
    dest_dir = _safe_path(out_dir, "out_dir")
    if not src.is_file():
        raise PreviewError(f"PDF not found at {src}. Run epub_to_pdf first.")
    if max_px < 200:
        raise PreviewError(f"max_px={max_px} is too small to read; use 600-1600.")
    _require_binary("pdftoppm")

    total = page_count(src)
    first, last, note = resolve_page_range(pages, total)
    if note:
        logger.warning("%s", note)
    dest_dir.mkdir(parents=True, exist_ok=True)
    render_dpi = dpi or _auto_dpi(src, max_px)

    with tempfile.TemporaryDirectory(prefix="pdf_pages_") as tmp:
        prefix = Path(tmp) / "pg"
        _run(
            ["pdftoppm", "-jpeg", "-r", str(render_dpi),
             "-f", str(first), "-l", str(last), str(src), str(prefix)],
            f"pdftoppm (pages {first}-{last})",
        )
        # pdftoppm zero-pads the page suffix to the width of the last page
        # number, so sort numerically rather than lexicographically.
        rendered: list[tuple[int, Path]] = []
        for f in Path(tmp).iterdir():
            m = re.search(r"-(\d+)\.jpe?g$", f.name)
            if m:
                rendered.append((int(m.group(1)), f))
        rendered.sort()
        if not rendered:
            raise PreviewError(
                f"pdftoppm produced no images for pages {first}-{last} of {src.name}."
            )
        out_paths: list[Path] = []
        for page_no, raw in rendered:
            dst = dest_dir / f"page_{page_no:03d}.jpg"
            _postprocess(raw, dst, max_px)
            out_paths.append(dst)

    logger.info(
        "Rasterised %s pages %d-%d at %ddpi -> %d image(s) in %s",
        src.name, first, last, render_dpi, len(out_paths), dest_dir,
    )
    return out_paths


# --------------------------------------------------------------------------- #
# Lint
# --------------------------------------------------------------------------- #

def _fmt_bytes(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 1_000_000 else f"{n / 1e3:.0f} kB"


def _label(rank: int) -> str:
    if rank <= 30:
        return "BLOCKER"
    if rank <= 60:
        return "WARNING"
    return "NOTE"


def _find_unrendered_math(text: str) -> tuple[list[str], str | None]:
    """(snippets, skip_reason) — delegates to :mod:`mathrender`.

    Soft dependency on purpose: lint must still run its other checks on a
    deployment where the math module is absent, and it must SAY that the math
    check did not run rather than implying the document is clean.
    """
    try:
        from mathrender import find_unrendered_math  # noqa: PLC0415
    except ImportError:
        return [], "the mathrender module is not available in this deployment"
    try:
        found = find_unrendered_math(text) or []
    except Exception as e:
        logger.warning("mathrender.find_unrendered_math failed: %s", e)
        return [], f"mathrender.find_unrendered_math raised {e.__class__.__name__}: {e}"
    snippets: list[str] = []
    for item in found:
        if isinstance(item, (list, tuple)):
            item = " ".join(str(x) for x in item)
        snippets.append(str(item).strip()[:80])
    return snippets, None


def _max_table_cols(table) -> int:
    widest = 0
    for row in table.find_all("tr"):
        width = 0
        for cell in row.find_all(["td", "th"], recursive=False) or row.find_all(["td", "th"]):
            try:
                width += max(1, int(cell.get("colspan", 1)))
            except (TypeError, ValueError):
                width += 1
        widest = max(widest, width)
    return widest


def lint_epub(epub_path: str | Path) -> list[str]:
    """Static checks over a BUILT EPUB; returns findings, most severe first.

    Reports, never raises, for content problems — the caller decides whether to
    send anyway. Returns ``[]`` when clean. (A missing or corrupt file IS raised:
    that is not a lint finding.)
    """
    path = _safe_path(epub_path, "epub_path")
    if not path.is_file():
        raise PreviewError(f"EPUB not found at {path}. Build the book before linting it.")

    findings: list[tuple[int, str]] = []

    def add(rank: int, msg: str) -> None:
        findings.append((rank, f"{_label(rank)}: {msg}"))

    size = path.stat().st_size
    if size > MAX_EPUB_BYTES:
        add(10,
            f"the EPUB is {_fmt_bytes(size)}. Gmail hard-caps the outbound "
            f"attachment at 25 MB and base64 encoding adds ~33% (so ~18 MB of "
            f"EPUB is the real ceiling) — this send will FAIL. Drop or shrink "
            f"the largest images and rebuild.")

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise PreviewError(f"{path.name} is not a valid EPUB (zip) file: {e}.") from e

    with zf:
        struct = _parse_structure(zf)
        names = {_norm(i.filename) for i in zf.infolist() if not i.is_dir()}
        sizes = {_norm(i.filename): i.file_size for i in zf.infolist() if not i.is_dir()}

        for href, _media in struct.images:
            item_size = sizes.get(href, 0)
            if item_size > MAX_IMAGE_BYTES:
                add(50,
                    f"image {href} is {_fmt_bytes(item_size)} (limit "
                    f"{_fmt_bytes(MAX_IMAGE_BYTES)}). Re-render it smaller or "
                    f"convert to grayscale JPEG; several of these will breach "
                    f"the 25 MB mail cap.")

        all_text: list[str] = []
        for href in struct.spine:
            if href not in names:
                add(30, f"spine document {href} is declared in the OPF but missing from the zip.")
                continue
            raw, valid_utf8 = _decode(zf.read(href))
            if not valid_utf8:
                add(15,
                    f"{href} is not valid UTF-8 — the Kindle will show replacement "
                    f"characters. Re-encode the source as UTF-8 and rebuild.")
            soup = BeautifulSoup(raw, "lxml")
            text = soup.get_text(" ", strip=True)
            all_text.append(text)
            base_dir = posixpath.dirname(href)

            # 1. Blocks the render pipeline should have consumed.
            left_diagram: set[str] = set()
            left_math: set[str] = set()
            for tags, wanted, bucket in (
                (DIAGRAM_TAGS, DIAGRAM_CLASSES, left_diagram),
                (MATH_TAGS, MATH_CLASSES, left_math),
            ):
                for tag in soup.find_all(tags):
                    for cls in tag.get("class") or []:
                        key = str(cls).strip().lower()
                        # markdown-minded agents write class="language-mermaid"
                        key = key[len("language-"):] if key.startswith("language-") else key
                        if key in wanted:
                            bucket.add(key)
            if left_diagram:
                add(20,
                    f"{href} still contains unrendered "
                    f"{', '.join(sorted(left_diagram))} block(s) — the reader will see "
                    f"raw diagram source. Fix the block's syntax and rebuild; the "
                    f"renderer silently skipped it.")
            if left_math:
                # Separate message: telling the model to fix "diagram source"
                # when the leftover is an equation sends it hunting in the wrong
                # place.
                add(20,
                    f"{href} still contains unrendered "
                    f"{', '.join(sorted(left_math))} block(s) — the reader will see "
                    f"raw LaTeX instead of an equation. Check the LaTeX syntax "
                    f"(matplotlib mathtext) and rebuild; the math renderer skipped it.")
            if "@startuml" in text or "@enduml" in text:
                add(20,
                    f"{href} contains literal '@startuml'/'@enduml' in the text — "
                    f"a PlantUML block was not rendered. Put the source inside "
                    f"<pre class=\"plantuml\">...</pre> and rebuild.")

            # 2. Double escaping: the source really contains "&amp;lt;", which
            #    the Kindle shows as a literal "&lt;".
            for entity in ("&amp;lt;", "&amp;gt;", "&amp;amp;"):
                n = raw.count(entity)
                if n:
                    add(35,
                        f"{href} contains {n} occurrence(s) of '{entity}' — the HTML "
                        f"was escaped twice, so the reader sees literal "
                        f"'&{entity[5:]}'. Escape the source exactly once.")

            # 3. Mojibake (UTF-8 bytes decoded as latin-1/cp1252 somewhere upstream).
            markers = [m for m in ("Ã©", "Ã¨", "Ã¼", "â€", "â€™", "Ð ", "Ñ€") if m in text]
            if markers:
                add(40,
                    f"{href} contains mojibake sequence(s) {markers[:3]} — text was "
                    f"decoded with the wrong codec upstream. Re-fetch/re-decode the "
                    f"source as UTF-8 and rebuild.")

            # 4. Images: resolvable + described.
            missing_alt = 0
            for img in soup.find_all("img"):
                src = (img.get("src") or "").strip()
                if not src:
                    add(30, f"{href} has an <img> with no src — the picture is gone.")
                elif src.startswith("data:"):
                    add(45,
                        f"{href} keeps an image as an inline data: URI instead of an "
                        f"embedded EPUB item; many Kindle renderers will not show it.")
                else:
                    target = _resolve_ref(base_dir, src)
                    if target is None or target not in names:
                        add(30,
                            f"{href} references image '{src}' which is NOT in the "
                            f"EPUB — it will render as a broken placeholder. Make "
                            f"sure the file exists before building.")
                alt = img.get("alt")
                if alt is None or not str(alt).strip():
                    missing_alt += 1
            if missing_alt:
                add(75,
                    f"{href} has {missing_alt} <img> without alt text; add alt so the "
                    f"content survives when an image fails to render.")

            # 5. Empty chapter (usually a stray <h1> creating a phantom split).
            has_images = bool(soup.find_all(["img", "svg", "image"]))
            if len(text) < MIN_CHAPTER_CHARS and not has_images:
                add(60,
                    f"chapter {href} holds only {len(text)} character(s) of text and "
                    f"no image — probably an accidental <h1> split. Merge it or drop "
                    f"the heading.")

            # 6. Wide tables are unreadable on a 6" screen.
            for table in soup.find_all("table"):
                cols = _max_table_cols(table)
                if cols > MAX_TABLE_COLS:
                    add(70,
                        f"{href} has a table with {cols} columns (>{MAX_TABLE_COLS}); "
                        f"it will not fit a 6\" e-ink screen. Split it or transpose it.")

        # 7. Raw LaTeX left in the rendered text.
        joined = "\n".join(all_text)
        math_snippets, math_skipped = _find_unrendered_math(joined)
        if math_snippets:
            add(25,
                f"unrendered LaTeX left in the book text ({len(math_snippets)} "
                f"occurrence(s)), e.g. {math_snippets[:3]} — the reader sees raw "
                f"markup instead of an equation. Render the math and rebuild.")
        if math_skipped:
            add(90, f"the raw-LaTeX check did NOT run: {math_skipped}.")

    findings.sort(key=lambda f: f[0])
    messages = [msg for _rank, msg in findings]
    if len(messages) > MAX_FINDINGS:
        extra = len(messages) - MAX_FINDINGS
        messages = messages[:MAX_FINDINGS]
        messages.append(f"NOTE: {extra} further finding(s) suppressed; fix these first.")
    return messages


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

def _doc_title(soup: BeautifulSoup, href: str) -> str:
    if soup.title and soup.title.string and soup.title.string.strip():
        return soup.title.string.strip()
    heading = soup.find(["h1", "h2", "h3"])
    if heading:
        t = heading.get_text(" ", strip=True)
        if t:
            return t
    return posixpath.basename(href)


def summarize_epub(epub_path: str | Path) -> dict:
    """Cheap structural summary of a BUILT EPUB (no rendering involved).

    ``{"bytes", "chapters": [{"title", "chars", "images"}], "images",
    "total_image_bytes"}`` — ``images`` counts embedded image ITEMS, while each
    chapter's ``images`` counts references from that chapter.
    """
    path = _safe_path(epub_path, "epub_path")
    if not path.is_file():
        raise PreviewError(f"EPUB not found at {path}. Build the book first.")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise PreviewError(f"{path.name} is not a valid EPUB (zip) file: {e}.") from e

    with zf:
        struct = _parse_structure(zf)
        sizes = {_norm(i.filename): i.file_size for i in zf.infolist() if not i.is_dir()}
        chapters = []
        for href in struct.spine:
            if href not in sizes:
                continue
            text, _ok = _decode(zf.read(href))
            soup = BeautifulSoup(text, "lxml")
            chapters.append({
                "title": _doc_title(soup, href),
                "chars": len(soup.get_text(" ", strip=True)),
                "images": len(soup.find_all(["img", "image"])),
            })
        image_bytes = sum(sizes.get(href, 0) for href, _m in struct.images)

    return {
        "bytes": path.stat().st_size,
        "chapters": chapters,
        "images": len(struct.images),
        "total_image_bytes": image_bytes,
    }
