r"""Rasterise LaTeX to PNG so equations survive the Kindle EPUB converter.

Kindle does not reliably render MathML (and drops it silently on several
device generations), so every equation is baked into a black-on-white PNG
server-side, where it can also be *seen* in the preview pass. The typesetter
is matplotlib's ``mathtext`` engine: it ships inside matplotlib with the
Computer Modern fonts, needs no LaTeX installation, and covers the TeX subset
technical prose actually uses.

Authoring contract (HTML in, ``<img>`` out)::

    <div class="math">E = mc^2</div>          -> display, centred block
    <pre class="math">\frac{a}{b}</pre>       -> display
    <span class="math">\alpha_i</span>        -> inline, line-height sized
    <code class="math">x^2</code>             -> inline

Delimiters are optional: ``$...$``, ``$$...$$``, ``\[...\]`` and ``\(...\)``
are stripped before rendering. A ``display``/``inline`` class overrides the
tag default, so pandoc-style ``<span class="math display">`` still renders as
a block.

mathtext is a TeX *subset*. Supported: ``\frac \sqrt \sum \int \lim \left
\right \text \mathrm \mathbb \boldsymbol \operatorname``, accents, Greek, the
usual symbol set. NOT supported: ``\displaystyle``, ``\begin{...}``
environments (align, matrix, cases), ``\\`` line breaks, ``\label`` / ``\tag``.
Multi-line derivations must be split into one math element per line.

Run order in the pipeline:
    sanitize -> process_math_blocks -> plantuml/diagrams -> embed_images
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))

MATH_DPI = int(os.getenv("MATH_DPI", "200"))
MATH_FONTSIZE = int(os.getenv("MATH_FONTSIZE", "14"))
# Bound total render work per document: ~50ms per equation, so 500 is ~25s.
MATH_MAX_BLOCKS = int(os.getenv("MATH_MAX_BLOCKS", "500"))

DPI_MIN, DPI_MAX = 50, 600
FONTSIZE_MIN, FONTSIZE_MAX = 6, 72
MAX_SOURCE_CHARS = 2000
MAX_PIXELS = 12_000_000
# mathtext has no \displaystyle, so a size bump is the only lever for the
# typographic convention that block equations read larger than inline ones.
DISPLAY_SCALE = 1.15
PAD_PX = 3

# Renderer settings applied per call via rc_context, never globally: a sibling
# module (charts, preview) may own its own rcParams in the same process.
_RC_PARAMS = {
    "mathtext.fontset": "cm",   # Computer Modern - reads like a printed book
    "mathtext.default": "it",
    "figure.facecolor": "white",
    "savefig.facecolor": "white",
    "savefig.edgecolor": "white",
    "savefig.transparent": False,
    "text.color": "black",
    "text.usetex": False,       # never shell out to a real TeX install
}

_DISPLAY_TAGS = ["pre", "div"]
_INLINE_TAGS = ["span", "code"]

# A leftover unescaped "$" means prose and math got mixed in one block.
_BARE_DOLLAR_RE = re.compile(r"(?<!\\)\$")
# Delimiter pairs stripped before rendering; "$$" must be tried before "$".
_DELIMITERS = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))

# Lint: raw LaTeX left in running text. Bounded quantifiers keep a stray "$$"
# (or a price list) from matching across the whole document.
_RAW_MATH_RE = re.compile(
    r"\$\$.{1,400}?\$\$"
    r"|\\\(.{1,400}?\\\)"
    r"|\\\[.{1,400}?\\\]",
    re.DOTALL,
)
_LINT_MAX_HITS = 20
_LINT_EXCERPT = 80

_SYNTAX_HELP = (
    "matplotlib mathtext renders a TeX SUBSET: no \\displaystyle, no "
    "\\begin{...}/\\end{...} environments (align, matrix, cases), no \\\\ line "
    "breaks, no \\label/\\tag. Split multi-line derivations into one math "
    "element per line, and use \\frac/\\sqrt/\\left...\\right/\\text/\\mathrm "
    "instead. Fix the source above and resend."
)

Sink = Callable[[bytes, bool], str]


class MathError(RuntimeError):
    """LaTeX could not be rendered. The message carries matplotlib's own parse
    error so the calling model can repair its input without a second round."""


_import_lock = threading.Lock()
# mathtext parsing + the Agg canvas are not re-entrant; tool calls run in
# worker threads, so serialise every render.
_render_lock = threading.Lock()
_mpl: tuple | None = None
_mpl_error: str | None = None


def _matplotlib() -> tuple:
    """Import matplotlib once (Agg only) -> (matplotlib, mathtext, FontProperties).

    Lazy so a server without matplotlib still starts and every non-math tool
    keeps working. MPLCONFIGDIR is pinned inside DATA_DIR because otherwise a
    read-only $HOME in the container makes matplotlib rebuild (and warn about)
    its font cache on every process start.
    """
    global _mpl, _mpl_error
    if _mpl is not None:
        return _mpl
    with _import_lock:
        if _mpl is not None:
            return _mpl
        if _mpl_error is not None:
            raise MathError(_mpl_error)
        try:
            if not os.environ.get("MPLCONFIGDIR"):
                cache_dir = DATA_DIR / ".mplconfig"
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    os.environ["MPLCONFIGDIR"] = str(cache_dir)
                except OSError as e:
                    logger.info("MPLCONFIGDIR %s not writable (%s); using default",
                                cache_dir, e)
            import matplotlib
            matplotlib.use("Agg")  # headless: set before anything can pull in pyplot
            from matplotlib import mathtext
            from matplotlib.font_manager import FontProperties
        except Exception as e:
            _mpl_error = (
                f"matplotlib is not installed/usable on this server ({e}), so LaTeX "
                "cannot be rendered. Remove the math elements from the document, or "
                "write the equations as plain Unicode text, and resend."
            )
            raise MathError(_mpl_error) from e
        _mpl = (matplotlib, mathtext, FontProperties)
        return _mpl


def available() -> bool:
    """True when LaTeX rendering can actually run (matplotlib importable)."""
    try:
        _matplotlib()
        return True
    except MathError:
        return False


def _normalise(latex: str) -> str:
    """Strip one delimiter pair, validate, and return the source wrapped in ``$``."""
    src = "" if latex is None else str(latex)
    if "\x00" in src:
        raise MathError("LaTeX source contains a NUL byte; remove it and resend.")
    src = src.strip()
    if not src:
        raise MathError(
            'Empty math element. Put the expression inside it, e.g. '
            '<div class="math">E = mc^2</div>, or delete the element.'
        )
    if len(src) > MAX_SOURCE_CHARS:
        raise MathError(
            f"LaTeX source is {len(src)} chars (max {MAX_SOURCE_CHARS}). Split it "
            "into several math elements, one equation each."
        )
    for open_d, close_d in _DELIMITERS:
        if (src.startswith(open_d) and src.endswith(close_d)
                and len(src) > len(open_d) + len(close_d)):
            src = src[len(open_d):-len(close_d)].strip()
            break
    if not src:
        raise MathError("Math element contains only delimiters and no expression.")
    if _BARE_DOLLAR_RE.search(src):
        raise MathError(
            f"LaTeX source has an unescaped '$': {src[:120]!r}. A math element holds "
            "ONE expression with no $ delimiters inside; write \\$ for a literal "
            "dollar sign and keep prose outside the element."
        )
    return f"${src}$"


def _syntax_error(src: str, exc: Exception) -> str:
    return f"LaTeX rejected by mathtext.\nSource: {src}\n{exc}\n{_SYNTAX_HELP}"


def _flatten(raw: bytes) -> bytes:
    """Composite onto opaque white, pad, and drop to 8-bit grey.

    matplotlib emits RGBA and crops flush to the glyph bounding box: Kindle
    paints alpha unpredictably (black boxes on some models) and the antialiased
    top/right edges get shaved without the pad. Grey also costs ~60% fewer bytes
    on a device that has no colour anyway.
    """
    from PIL import Image  # local: only the render path needs Pillow

    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        rgba = im.convert("RGBA")
    canvas = Image.new(
        "RGBA", (rgba.width + 2 * PAD_PX, rgba.height + 2 * PAD_PX), (255, 255, 255, 255)
    )
    canvas.alpha_composite(rgba, (PAD_PX, PAD_PX))
    out = io.BytesIO()
    canvas.convert("L").save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=256)
def _render_cached(src: str, fontsize: float, dpi: int) -> bytes:
    """Render ``$...$`` to PNG bytes. Cached: inline symbols repeat a lot."""
    matplotlib, mathtext, FontProperties = _matplotlib()
    prop = FontProperties(size=fontsize)
    buf = io.BytesIO()
    with _render_lock, matplotlib.rc_context(_RC_PARAMS):
        try:
            # Parse first: it raises the same error math_to_image would, but
            # cheaply, and hands us the size so a monster expression can be
            # refused before the canvas is allocated.
            width_pt, height_pt = mathtext.MathTextParser("path").parse(
                src, dpi=72, prop=prop
            )[:2]
        except Exception as e:
            raise MathError(_syntax_error(src, e)) from e
        pixels = (float(width_pt) * dpi / 72.0) * (float(height_pt) * dpi / 72.0)
        if pixels > MAX_PIXELS:
            raise MathError(
                f"Rendered equation would be {int(pixels):,} pixels (max "
                f"{MAX_PIXELS:,}). Lower the dpi or split the expression."
            )
        try:
            mathtext.math_to_image(
                src, buf, prop=prop, dpi=dpi, format="png", color="black"
            )
        except Exception as e:
            raise MathError(_syntax_error(src, e)) from e
    return _flatten(buf.getvalue())


def _em_width(width_px: int, display: bool) -> float:
    """Convert a rendered equation's pixel width to em at the reader's font size.

    mathtext lays out at ``fontsize`` points and rasterises at ``dpi``, so one em
    is ``dpi/72 * fontsize`` pixels. Expressing the <img> width in em makes the
    equation scale with the body text instead of with the page's pixel width.
    """
    size = MATH_FONTSIZE * (DISPLAY_SCALE if display else 1.0)
    px_per_em = MATH_DPI / 72.0 * size
    return max(1.0, width_px / px_per_em) if px_per_em else 1.0


def _png_size(png: bytes) -> tuple[int, int]:
    """Pixel size straight out of the PNG IHDR - no second decode."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        raise MathError("Math renderer produced non-PNG data; this is a server bug.")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def render_math(
    latex: str,
    display: bool = True,
    dpi: int = MATH_DPI,
    fontsize: int = MATH_FONTSIZE,
) -> tuple[bytes, int, int]:
    """Rasterise one LaTeX expression to a black-on-white PNG.

    Returns ``(png_bytes, width_px, height_px)``. ``display=True`` renders
    ``DISPLAY_SCALE`` larger (see the constant for why); stacked limits on
    ``\\sum``/``\\int`` are mathtext's default either way. Delimiters are
    optional. Raises :class:`MathError` carrying matplotlib's parse message.
    """
    if not isinstance(dpi, int) or not DPI_MIN <= dpi <= DPI_MAX:
        raise MathError(f"dpi must be an int in {DPI_MIN}..{DPI_MAX} (got {dpi!r}).")
    if not isinstance(fontsize, int) or not FONTSIZE_MIN <= fontsize <= FONTSIZE_MAX:
        raise MathError(
            f"fontsize must be an int in {FONTSIZE_MIN}..{FONTSIZE_MAX} (got {fontsize!r})."
        )
    src = _normalise(latex)
    size = round(fontsize * (DISPLAY_SCALE if display else 1.0), 2)
    try:
        png = _render_cached(src, size, dpi)
    except MathError:
        raise
    except Exception as e:  # PIL/Agg failure: still hand the model one error type
        raise MathError(f"Math rendering failed for {src}: {e}") from e
    width, height = _png_size(png)
    return png, width, height


def _attached(tag: Tag, root: BeautifulSoup | Tag) -> bool:
    """False once an ancestor has been replaced (find_all results go stale).

    Walks up to ``root`` rather than to the document root, so a caller that
    passes a subtree (``soup.body``, one chapter) does not silently render
    nothing.
    """
    node: Tag | None = tag
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


def _alt_text(latex: str) -> str:
    """Alt text = the author's LaTeX, delimiters stripped, whitespace collapsed.

    Delimiters are dropped so :func:`find_unrendered_math` does not flag our own
    output as unrendered math when a lint pass reads the raw HTML.
    """
    src = _normalise(latex)[1:-1]
    return " ".join(src.split())


def process_math_blocks(soup: BeautifulSoup, sink: Sink) -> int:
    """Replace every ``class="math"`` element with a rendered ``<img>``.

    Display first (``<pre>``/``<div>``), then inline (``<span>``/``<code>``);
    an explicit ``display``/``inline`` class wins over the tag default.
    ``sink(png_bytes, display) -> str`` supplies the img ``src`` (data URI,
    stored asset, whatever the caller wants) - same contract as
    ``diagrams.process_diagram_blocks``.

    Returns the number of equations rendered. Raises :class:`MathError` naming
    the offending element.
    """
    # new_tag() lives on the document, not on a Tag: resolve it so a caller may
    # also hand in a subtree (soup.body, one chapter) instead of the whole doc.
    doc: BeautifulSoup | Tag = soup
    while doc.parent is not None:
        doc = doc.parent
    if not isinstance(doc, BeautifulSoup):
        raise MathError(
            "process_math_blocks needs a BeautifulSoup document (or a tag still "
            "attached to one); the tag passed in is detached from any soup."
        )

    rendered = 0
    for names, default_display in ((_DISPLAY_TAGS, True), (_INLINE_TAGS, False)):
        for tag in soup.find_all(names):
            classes = tag.get("class") or []
            if "math" not in classes:
                continue
            if not _attached(tag, soup):
                continue  # nested inside an already-replaced math element
            if rendered >= MATH_MAX_BLOCKS:
                raise MathError(
                    f"Document has more than {MATH_MAX_BLOCKS} math elements. Split it "
                    "into several documents, or raise MATH_MAX_BLOCKS on the server."
                )

            display = default_display
            if "inline" in classes:
                display = False
            elif "display" in classes:
                display = True

            # get_text() decodes entities, so &lt; and &amp; in raw HTML reach
            # mathtext as < and &.
            source = tag.get_text()
            try:
                alt = _alt_text(source)
                png, _w, _h = render_math(source, display=display)
                src = sink(png, display)
            except MathError as e:
                raise MathError(
                    f'<{tag.name} class="math"> #{rendered + 1}: {e}'
                ) from e

            img = doc.new_tag("img", src=src, alt=alt)
            if display:
                # Size the equation in em, not pixels: the PNG is rasterised at
                # MATH_DPI so its natural pixel width would swallow a 6" page
                # (a one-line formula rendered ~85% of the page before this).
                # em keeps it proportional to the surrounding text on any device.
                img["style"] = f"width:{_em_width(_w, True):.2f}em;max-width:100%;height:auto"
                block = doc.new_tag("div", style="text-align:center;margin:0.8em 0")
                block.append(img)
                tag.replace_with(block)
            else:
                img["style"] = "vertical-align:middle;height:1.15em"
                tag.replace_with(img)
            rendered += 1

    if rendered:
        logger.info("Rendered %d equation(s)", rendered)
    return rendered


def find_unrendered_math(text: str) -> list[str]:
    """Find raw LaTeX left in running text: ``$$...$$``, ``\\(...\\)``, ``\\[...\\]``.

    Returns deduplicated, whitespace-collapsed excerpts (at most
    ``_LINT_MAX_HITS``) for the preview lint pass, which is why this stays
    stdlib-only and never touches matplotlib. Single ``$...$`` is deliberately
    ignored - prices and shell prompts make it far too noisy.
    """
    if not text or ("$$" not in text and "\\(" not in text and "\\[" not in text):
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for match in _RAW_MATH_RE.finditer(text):
        excerpt = " ".join(match.group(0).split())
        if len(excerpt) > _LINT_EXCERPT:
            excerpt = excerpt[:_LINT_EXCERPT - 3] + "..."
        if excerpt in seen:
            continue
        seen.add(excerpt)
        hits.append(excerpt)
        if len(hits) >= _LINT_MAX_HITS:
            break
    return hits
