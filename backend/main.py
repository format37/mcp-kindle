"""kindle MCP — compose a book, verify how it renders, send it to a Kindle.

Two ways to drive this server:

* **Local** (Claude Code): image files are dropped straight into the
  bind-mounted ``data/`` folder and referenced by bare filename;
  ``send_html_to_kindle`` does the rest. Unchanged from the original server.
* **Remote** (claude.ai web, from a phone): there is no filesystem and no
  shell, so the server itself generates the images, renders the diagrams and
  the equations, and rasterises the built EPUB back into page images the model
  can actually look at before sending. State lives in a *book workspace*
  addressed by ``book_id``.
"""

import contextlib
import contextvars
import datetime
import logging
import os
import re
import shutil
from pathlib import Path

import anyio
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage
from mcp.server.transport_security import TransportSecuritySettings

import books
import diagrams
import fetch
import imagegen
import jobs
import preview as preview_mod
from books import BookError
from html_tools import build_epub_from_html, convert_html_and_send
from kindle_tools import send_epub_to_kindle

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")

MCP_NAME = os.getenv("MCP_NAME", "kindle")
_safe_name = re.sub(r"[^a-z0-9_-]", "-", MCP_NAME.lower()).strip("-") or "service"
BASE_PATH = f"/{_safe_name}"
STREAM_PATH = f"{BASE_PATH}/"
ASSETS_ROUTE = f"{BASE_PATH}/assets"
OUT_ROUTE = f"{BASE_PATH}/out"

PORT = int(os.getenv("PORT", "8018"))
SOFT_TIMEOUT_S = float(os.getenv("MCP_SOFT_TIMEOUT_S", "90"))
BOOK_MAX_AGE_DAYS = int(os.getenv("BOOK_MAX_AGE_DAYS", "30"))
PUBLIC_BASE_URL = (os.getenv("MCP_PUBLIC_BASE_URL") or "").rstrip("/") or None
MAX_SEND_BYTES = int(os.getenv("MAX_SEND_BYTES", str(24 * 1024 * 1024)))

MCP_TOKEN_CTX = contextvars.ContextVar("mcp_token", default=None)
_TOKENS = {t.strip() for t in os.getenv("MCP_TOKENS", "").split(",") if t.strip()}

# Transport security: the allowlist is env-driven so adding a public hostname is
# a compose change, not a rebuild. Behind Caddy the upstream Host header is
# whatever the proxy sends — if it isn't listed here the MCP answers 421.
_allowed_hosts = ["localhost", "127.0.0.1", "0.0.0.0", _safe_name, f"mcp-{_safe_name}"]
# ":*" is the library's port wildcard. Pinning the container's own PORT here
# would 421 any request published on a different host port (dev instance on
# 8028, a second stack, a proxy that keeps the port in Host).
_allowed_hosts += [f"{h}:*" for h in list(_allowed_hosts)]
_allowed_origins = ["http://localhost", "http://127.0.0.1"]
for _h in [x.strip() for x in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if x.strip()]:
    _allowed_hosts.append(_h)
    _allowed_hosts.append(f"{_h}:*")
    _allowed_origins.append(f"https://{_h}")

mcp = FastMCP(
    _safe_name,
    streamable_http_path=STREAM_PATH,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


class _StreamErrorFilter(logging.Filter):
    def filter(self, record):
        return "ClosedResourceError" not in str(record.getMessage())


logging.getLogger("mcp.server.streamable_http").addFilter(_StreamErrorFilter())


class _TokenRedactingFilter(logging.Filter):
    """Tokens ride in the URL path, so without this they land in access logs."""

    def __init__(self, tokens):
        super().__init__()
        self._tokens = sorted((t for t in tokens if t), key=len, reverse=True)

    def filter(self, record):
        if self._tokens:
            try:
                msg = record.getMessage()
                redacted = msg
                for t in self._tokens:
                    redacted = redacted.replace(t, "***")
                if redacted != msg:
                    record.msg = redacted
                    record.args = ()
            except Exception:
                pass
        return True


_redaction_filter = _TokenRedactingFilter(_TOKENS)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_redaction_filter)
logging.getLogger().addFilter(_redaction_filter)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

WORKFLOW = """\
Workflow (remote / claude.ai mode):
  1. create_book(title)                       -> book_id
  2. generate_images(book_id, prompts=[...])   illustrations, and kind="cover"
     add_images_from_urls(book_id, urls)       for images you already have
     render_diagram(book_id, engine, source)   optional: check a diagram compiles
  3. set_document(book_id, html)               reference assets by BARE FILENAME
  4. preview_book(book_id)                     LOOK at the page images + lint report
  5. patch_document(...) then preview again until it is right
  6. send_book(book_id, cover="cover.jpg")
"""


def _book_dirs(book_id: str) -> list[Path]:
    """Image search roots for a book: its own assets first, then the legacy folder."""
    return [books.assets_dir(book_id), books.DATA_DIR]


def _fmt_assets(rows: list[dict]) -> str:
    if not rows:
        return "  (no assets yet)"
    out = []
    for r in rows:
        url = f"  {r['url']}" if r.get("url") else ""
        out.append(
            f"  {r['filename']}  {r.get('width')}x{r.get('height')}  "
            f"{r.get('bytes', 0) // 1024} KB{url}"
        )
    return "\n".join(out)


def _fresh_name(book_id: str, base: str) -> str:
    """Reserve an unused asset stem.

    Without this, two images asked for under the same name (or two
    ``render_diagram`` calls with one engine) overwrite each other and the tool
    still reports N assets — the model would reference a filename holding
    someone else's picture.
    """
    return books.unique_asset_name(book_id, base, ".png")


def _image_content(path: Path):
    """Wrap a rendered page image as an MCP image block the model can look at."""
    return MCPImage(data=Path(path).read_bytes(), format="jpeg").to_image_content()


def _preview_available() -> bool:
    try:
        return bool(preview_mod.preview_available()[0])
    except Exception:
        return shutil.which("pdftoppm") is not None


def _epub_for_book(book_id: str, cover: str, title: str) -> tuple[Path, str]:
    """Build the book's EPUB into its out/ dir. Returns (path, resolved_title)."""
    meta = books.load_meta(book_id)
    html = books.get_document(book_id)
    out = books.out_dir(book_id) / "book.epub"
    resolved = build_epub_from_html(
        html,
        (title or "").strip() or meta.get("title") or "Untitled",
        str(out),
        cover=cover or None,
        data_dirs=_book_dirs(book_id),
    )
    return out, resolved


# --------------------------------------------------------------------------
# workspace tools
# --------------------------------------------------------------------------


@mcp.tool()
def create_book(title: str, notes: str = "") -> str:
    """Open a new book workspace and get back its ``book_id``.

    Everything else in this server is scoped to that id: generated images,
    diagrams, the document itself, previews. Use this first when composing a
    book from claude.ai, where there is no local filesystem.

    Args:
        title: Book title. Keep it SHORT — 2-3 words reads best in the Kindle
            library. Do not put a date in it; the server prefixes one itself.
        notes: Optional free-text note to yourself about the book's plan.
    """
    meta = books.create_book(title, notes)
    return (
        f"book_id: {meta['book_id']}\n"
        f"title:   {meta['title']}\n\n"
        f"{WORKFLOW}\n"
        "Notes:\n"
        "  - The Kindle is black-and-white e-ink. Images are converted to\n"
        "    grayscale automatically; never rely on colour alone to distinguish\n"
        "    things in a diagram — use line style, hatching, shape and labels.\n"
        "  - Chapters split at every <h1>; content before the first <h1> becomes\n"
        "    a Preface.\n"
        '  - Every book should have a cover: generate_images(kind="cover").'
    )


@mcp.tool()
def list_books(limit: int = 20) -> str:
    """List recent book workspaces (newest first) with their ids and sizes."""
    rows = books.list_books(limit=limit)
    if not rows:
        return "No books yet. Start with create_book(title)."
    return "\n".join(
        f"{r['book_id']}  {r.get('doc_chars', 0):>7} chars  "
        f"{r.get('asset_count', 0):>3} assets  {r.get('updated', '?')}  {r.get('title', '')}"
        for r in rows
    )


@mcp.tool()
def book_status(book_id: str) -> str:
    """Show a book's document size, its assets (filename, dimensions, URL) and notes.

    Use this to recover after losing context, or to check which asset filenames
    you can reference from the HTML.
    """
    meta = books.load_meta(book_id)
    assets = books.list_assets(book_id)
    doc_chars = meta.get("doc_chars", 0)
    return (
        f"book_id: {meta['book_id']}\n"
        f"title:   {meta.get('title')}\n"
        f"document: {doc_chars} chars"
        f"{' (not set yet — call set_document)' if not doc_chars else ''}\n"
        f"updated: {meta.get('updated')}\n"
        f"notes:   {meta.get('notes') or '-'}\n"
        f"assets ({len(assets)}):\n{_fmt_assets(assets)}"
    )


@mcp.tool()
def set_document(book_id: str, html: str) -> str:
    """Store the book's HTML on the server (replaces any previous version).

    Keeping the document server-side is what makes the preview -> fix -> preview
    loop cheap: you patch it instead of re-sending the whole book each time.

    The HTML may be a full document or a fragment. Reference images by BARE
    FILENAME (``<img src="fig_arch.png">``) — they resolve against this book's
    assets. Rich content that survives conversion: tables with rowspan/colspan,
    <figure>/<figcaption>, <dl>, <blockquote>, <sup>/<sub>/<mark>, inline <svg>.
    Stripped for safety: <script>, <style>, <iframe>, <form>, on* handlers.
    Inline ``style="..."`` attributes are kept — use those for styling.

    Blocks the server renders to images at build time:
      <pre class="mermaid">   modern flow / sequence / ER / state diagrams
      <pre class="plantuml">  full UML (class, sequence, activity, component, ...)
      <pre class="d2">        clean architecture diagrams, crispest output
      <pre class="graphviz">  raw DOT graphs
      <pre class="math">      display equation (LaTeX)
      <span class="math">     inline equation (LaTeX)
    Escape ``<`` and ``>`` as &lt; / &gt; inside those blocks — PlantUML and
    Mermaid arrow syntax is full of them and the HTML parser would eat them.
    """
    info = books.set_document(book_id, html)
    return (
        f"Stored {info['doc_chars']} chars for {book_id}. "
        "Next: preview_book(book_id) and look at the pages before sending."
    )


@mcp.tool()
def patch_document(book_id: str, find: str, replace: str, count: int = 1) -> str:
    """Literal find/replace inside the stored document — cheap iteration.

    ``find`` is plain text, not a regex. Pick an anchor long enough to be
    unique. Use this instead of re-sending the whole book after a preview.
    """
    info = books.patch_document(book_id, find, replace, count)
    return (
        f"Replaced {info['replacements']} occurrence(s); "
        f"document is now {info['doc_chars']} chars."
    )


# --------------------------------------------------------------------------
# content generation
# --------------------------------------------------------------------------


@mcp.tool()
async def generate_images(
    book_id: str,
    prompts: list[str],
    names: list[str] = [],
    kind: str = "illustration",
    aspect_ratio: str = "",
    image_size: str = "1K",
    model: str = "pro",
) -> str:
    """Generate illustrations (or the cover) with Gemini, straight into the book.

    All prompts run CONCURRENTLY and the results are stored as grayscale,
    e-ink-sized assets — one call instead of a round trip per image. You get
    back filenames to reference from the HTML as ``<img src="NAME.jpg">``.

    Args:
        book_id: From create_book.
        prompts: One prompt per image. Describe the subject concretely; the
            server appends the black-and-white / high-contrast style guidance.
        names: Optional base filenames, positionally matched to prompts
            (e.g. ["fig_reactor", "cover"]). Auto-named when omitted.
        kind: "illustration" (default) or "cover". "cover" asks for a bold
            central subject on white with NO text — the EPUB renders the title
            itself — and defaults to a 2:3 portrait shape.
        aspect_ratio: "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16",
            "16:9", "21:9". Defaults to 2:3 for a cover, 4:3 otherwise.
        image_size: "1K" (default, fast and plenty for e-ink), "2K" or "4K".
        model: "pro" (best quality) or "flash" (faster).

    Each image takes ~15-25s and concurrency is bounded, so a large batch may
    outlive the tool timeout — you then get a job_id to poll with get_job().
    Inspect the results later with preview_book.
    """
    if not imagegen.available():
        return (
            "Error: GEMINI_API_KEY is not configured on this server, so image "
            "generation is unavailable here. Use add_images_from_urls() with an "
            "image you already have, or a diagram block instead."
        )
    books.load_meta(book_id)  # validate the id before doing slow work
    kind = (kind or "illustration").strip().lower()
    aspect = aspect_ratio or ("2:3" if kind == "cover" else "4:3")
    prepared = [imagegen.eink_prompt(p, kind=kind) for p in prompts]

    def _work():
        results = imagegen.generate_many(
            prepared, aspect_ratio=aspect, image_size=image_size, model=model
        )
        stored, failed = [], []
        for r in results:
            if not r["ok"]:
                failed.append(f"  [{r['index']}] {r['error']}")
                continue
            base = (
                names[r["index"]]
                if r["index"] < len(names)
                else ("cover" if kind == "cover" else f"img_{r['index'] + 1}")
            )
            stored.append(books.store_asset(book_id, _fresh_name(book_id, base), r["data"]))
        return stored, failed

    finished, job_id, result = await anyio.to_thread.run_sync(
        lambda: jobs.run_with_soft_timeout(_work, SOFT_TIMEOUT_S, label=f"images:{book_id}")
    )
    if not finished:
        return (
            f"Still generating {len(prompts)} image(s) — roughly 20s each.\n"
            f"job_id: {job_id}\n"
            f'Poll with get_job("{job_id}"), then book_status("{book_id}") for the filenames.'
        )
    stored, failed = result
    lines = [f"Stored {len(stored)} image(s) in {book_id}:", _fmt_assets(stored)]
    if failed:
        lines.append(f"{len(failed)} failed:")
        lines.extend(failed)
    lines.append('Reference them in the HTML as <img src="FILENAME" alt="...">.')
    return "\n".join(lines)


@mcp.tool()
async def add_images_from_urls(book_id: str, urls: list[str], names: list[str] = []) -> str:
    """Download images from public https URLs into the book (grayscale, resized).

    Use this for images that already exist somewhere — including results from
    the separate ``imagine`` MCP, which returns a full-resolution asset URL.
    The bytes are baked into the EPUB, so nothing is fetched when the book is
    read (no view-time IP leak) and the source URL may expire afterwards.

    Only public http(s) addresses are allowed; private and loopback addresses
    are refused.
    """
    books.load_meta(book_id)

    def _work():
        stored, failed = [], []
        for i, url in enumerate(urls):
            try:
                data, _mime = fetch.fetch_bytes(url)
                base = names[i] if i < len(names) else f"url_{i + 1}"
                stored.append(books.store_asset(book_id, _fresh_name(book_id, base), data))
            except Exception as e:
                # fetch_bytes raises requests' own errors too, not just ValueError.
                failed.append(f"  [{i}] {url[:70]} -> {e}")
        return stored, failed

    # A slow host can hold a redirect chain open for minutes; don't let that
    # burn the whole tool call.
    finished, job_id, result = await anyio.to_thread.run_sync(
        lambda: jobs.run_with_soft_timeout(_work, SOFT_TIMEOUT_S, label=f"urls:{book_id}")
    )
    if not finished:
        return (
            f"Still downloading {len(urls)} image(s) — a slow host is holding the "
            f"connection.\njob_id: {job_id}\n"
            f'Poll with get_job("{job_id}"), then book_status("{book_id}").'
        )
    stored, failed = result
    lines = [f"Stored {len(stored)} image(s) in {book_id}:", _fmt_assets(stored)]
    if failed:
        lines.append(f"{len(failed)} failed:")
        lines.extend(failed)
    return "\n".join(lines)


@mcp.tool()
async def render_diagram(book_id: str, engine: str, source: str, name: str = "") -> str:
    """Render a diagram to a stored PNG asset and get its filename back.

    You normally do NOT need this: put the diagram source directly in the
    document inside ``<pre class="mermaid">`` / ``plantuml`` / ``d2`` /
    ``graphviz`` and the server renders it at build time. Use this tool to
    check that a tricky diagram compiles BEFORE committing it to the document,
    or to reuse one rendered image in several places.

    Args:
        engine: "mermaid", "plantuml", "d2" or "graphviz".
        source: Raw diagram source (no HTML escaping needed here).
        name: Optional base filename.
    """
    books.load_meta(book_id)

    def _work():
        png = diagrams.render(engine, source, fmt="png")
        return books.store_asset(
            book_id, _fresh_name(book_id, name or f"diag_{engine}"), png
        )

    asset = await anyio.to_thread.run_sync(_work)
    return (
        f"Rendered {engine} diagram -> {asset['filename']} "
        f"({asset['width']}x{asset['height']}, {asset['bytes'] // 1024} KB)\n"
        f'Reference it as <img src="{asset["filename"]}" alt="{engine} diagram">.'
    )


# --------------------------------------------------------------------------
# verification + delivery
# --------------------------------------------------------------------------


@mcp.tool()
async def preview_book(
    book_id: str, pages: str = "1-4", cover: str = "", title: str = ""
) -> list:
    """Build the EPUB and return page IMAGES plus a lint report — LOOK at them.

    This is how you verify a book before it reaches the Kindle. It builds the
    real EPUB, rasterises its pages and hands them back as inline images you
    can inspect for rendered equations, rendered diagrams, broken images and
    stray symbols. The lint report catches what pixels hide: unrendered blocks,
    double-escaped entities, missing images, oversize files.

    The page images approximate an e-reader's shape; they are not Kindle's own
    renderer, so read them as a content check rather than exact layout.

    Args:
        pages: "1-4" (default), "3", or "all" (capped). More pages cost more
            context — preview a range around whatever you just changed.
        cover: Cover asset filename, same as send_book.
        title: Overrides the stored title for this build.
    """

    def _work():
        epub_path, resolved = _epub_for_book(book_id, cover, title)
        findings = preview_mod.lint_epub(epub_path)
        summary = preview_mod.summarize_epub(epub_path)
        out = books.out_dir(book_id)
        pdf = preview_mod.epub_to_pdf(epub_path, out / "preview.pdf")
        total = preview_mod.page_count(pdf)
        # Carry the clamp note: "4 pages back" must not read as "the book is 4
        # pages long" when the model asked for a range the book doesn't have.
        _first, _last, note = preview_mod.resolve_page_range(pages, total)
        imgs = preview_mod.pdf_to_images(pdf, out, pages=pages)
        return resolved, findings, summary, pdf, imgs, total, note

    finished, job_id, result = await anyio.to_thread.run_sync(
        lambda: jobs.run_with_soft_timeout(_work, SOFT_TIMEOUT_S, label=f"preview:{book_id}")
    )
    if not finished:
        return [
            f"Still building the preview.\njob_id: {job_id}\n"
            f'Poll with get_job("{job_id}"), then call preview_book again to see the pages.'
        ]

    resolved, findings, summary, pdf, imgs, total, note = result
    lines = [
        f"Preview of '{resolved}' ({book_id})",
        f"  {summary['bytes'] // 1024} KB EPUB, {len(summary['chapters'])} chapter(s), "
        f"{summary['images']} image(s), {total} preview page(s)",
    ]
    for ch in summary["chapters"]:
        lines.append(f"    - {ch['title']}: {ch['chars']} chars, {ch['images']} image(s)")
    if PUBLIC_BASE_URL:
        lines.append(f"  Full PDF: {PUBLIC_BASE_URL}{OUT_ROUTE}/{book_id}/{Path(pdf).name}")
    if findings:
        lines.append(f"  LINT — {len(findings)} finding(s), fix these before sending:")
        lines.extend(f"    * {f}" for f in findings)
    else:
        lines.append("  LINT — clean.")
    if note:
        lines.append(f"  {note}")
    lines.append(f"  {len(imgs)} of {total} page image(s) follow.")

    blocks: list = ["\n".join(lines)]
    for p in imgs:
        blocks.append(_image_content(p))
    return blocks


@mcp.tool()
async def send_book(book_id: str, cover: str = "", title: str = "") -> str:
    """Build the book's EPUB and email it to the Kindle.

    Run preview_book first and read its lint report — this is the step that
    cannot be undone from a phone.

    Args:
        cover: Filename of a cover asset in this book (strongly recommended;
            it becomes the library thumbnail and the opening page).
        title: Overrides the stored title. Keep it to 2-3 words with no date —
            the server prefixes the datetime itself.
    """
    if not (SENDER_EMAIL and GMAIL_APP_PASS and RECIPIENT_EMAIL):
        return "Error: missing email configuration (SENDER_EMAIL, GMAIL_APP_PASS, RECIPIENT_EMAIL)"

    def _work():
        epub_path, resolved = _epub_for_book(book_id, cover, title)
        size = epub_path.stat().st_size
        if size > MAX_SEND_BYTES:
            raise RuntimeError(
                f"EPUB is {size // (1024 * 1024)} MB but the outbound mail limit is "
                f"{MAX_SEND_BYTES // (1024 * 1024)} MB. Drop or shrink images — "
                "preview_book lists the biggest ones."
            )
        send_epub_to_kindle(
            str(epub_path), SENDER_EMAIL, GMAIL_APP_PASS, RECIPIENT_EMAIL, resolved
        )
        return resolved, size

    resolved, size = await anyio.to_thread.run_sync(_work)
    msg = f"Sent '{resolved}' ({size // 1024} KB) to {RECIPIENT_EMAIL}."
    if PUBLIC_BASE_URL:
        # The built EPUB stays in the workspace, so it is also downloadable —
        # handy when the mail is slow or you want the file on another device.
        msg += f"\nEPUB also at: {PUBLIC_BASE_URL}{OUT_ROUTE}/{book_id}/book.epub"
    return msg


@mcp.tool()
def get_job(job_id: str) -> str:
    """Check a background job started by a tool that ran past its time budget."""
    j = jobs.get(job_id)
    if j["status"] == "running":
        return f"{j['label']}: still running ({j['elapsed_s']:.0f}s elapsed). Poll again."
    if j["status"] == "error":
        return f"{j['label']}: FAILED after {j['elapsed_s']:.0f}s — {j['error']}"
    return (
        f"{j['label']}: done in {j['elapsed_s']:.0f}s. "
        "Call book_status(book_id) or preview_book(book_id) to use the result."
    )


@mcp.tool()
def send_html_to_kindle(html_text: str, title: str = "", cover: str = "") -> str:
    """One-shot: convert HTML to EPUB and email it, with no book workspace.

    This is the original local-mode tool and behaves exactly as before: images
    are read by bare filename from the server's shared ``data/`` folder. From
    claude.ai (no filesystem) use the book workspace instead — create_book /
    set_document / preview_book / send_book — which lets you see the result
    before sending.

    Diagram and math blocks (<pre class="mermaid|plantuml|d2|graphviz|math">)
    are rendered here too.

    Args:
        html_text: Full document or fragment. Chapters split at every <h1>.
        title: Book title; falls back to <title>, then the first <h1>.
        cover: Bare filename of a cover image in the data folder.
    """
    if not (SENDER_EMAIL and GMAIL_APP_PASS and RECIPIENT_EMAIL):
        return "Error: missing email configuration (SENDER_EMAIL, GMAIL_APP_PASS, RECIPIENT_EMAIL)"
    try:
        return convert_html_and_send(
            html_text=html_text,
            title=title,
            sender_email=SENDER_EMAIL,
            sender_password=GMAIL_APP_PASS,
            recipient_email=RECIPIENT_EMAIL,
            cover=cover,
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.resource(
    f"{_safe_name}://documentation",
    name="Kindle MCP documentation",
    description="How to compose, verify and send a book to the Kindle",
    mime_type="text/markdown",
)
def get_documentation_resource() -> str:
    engines = ", ".join(k for k, v in diagrams.available_engines().items() if v) or "none"
    return f"""# kindle MCP

{WORKFLOW}

Diagram engines available here: {engines}.
Image generation: {"enabled" if imagegen.available() else "disabled (no GEMINI_API_KEY)"}.
Preview rasteriser: {"enabled" if _preview_available() else "disabled"}.

The destination is a black-and-white e-ink screen. Use hatching, line style,
shape and labels — never colour alone — to distinguish things in a diagram.
"""


# --------------------------------------------------------------------------
# ASGI app
# --------------------------------------------------------------------------

mcp_asgi = mcp.streamable_http_app()


async def _serve_book_file(request, subdir: str):
    """Serve one file out of a book's assets/ or out/ dir.

    Deliberately not a StaticFiles mount over the books root — that would also
    expose meta.json and doc.html. Book ids carry 12 random hex characters, so
    the URL itself is the capability.
    """
    book_id = request.path_params["book_id"]
    filename = request.path_params["filename"]
    try:
        base = (books.assets_dir(book_id) if subdir == "assets" else books.out_dir(book_id)).resolve()
        target = (base / filename).resolve()
        if base not in target.parents or not target.is_file():
            raise BookError("not found")
    except Exception:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=86400"})


async def serve_asset(request):
    return await _serve_book_file(request, "assets")


async def serve_out(request):
    return await _serve_book_file(request, "out")


async def health_check(request):
    return JSONResponse(
        {
            "status": "healthy",
            "service": _safe_name,
            "email_configured": bool(SENDER_EMAIL and GMAIL_APP_PASS and RECIPIENT_EMAIL),
            "image_generation": imagegen.available(),
            "diagram_engines": diagrams.available_engines(),
            "preview": _preview_available(),
            "public_base_url": PUBLIC_BASE_URL,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    books.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    if BOOK_MAX_AGE_DAYS > 0:
        with contextlib.suppress(Exception):
            n = books.prune(BOOK_MAX_AGE_DAYS)
            if n:
                logger.info("pruned %d book(s) older than %d days", n, BOOK_MAX_AGE_DAYS)
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health_check, methods=["GET"]),
        Route(f"{BASE_PATH}/health", health_check, methods=["GET"]),
        Route(f"{ASSETS_ROUTE}/{{book_id}}/{{filename}}", serve_asset, methods=["GET"]),
        Route(f"{OUT_ROUTE}/{{book_id}}/{{filename}}", serve_out, methods=["GET"]),
        Mount("/", app=mcp_asgi),
    ],
    lifespan=lifespan,
)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Token gate for everything under BASE_PATH.

    Accepts ``Authorization: Bearer <token>``, ``?token=`` and
    ``/kindle/<token>/…`` path tokens (a claude.ai custom connector can only
    carry the secret in the URL). Health and asset routes stay public — assets
    already sit behind an unguessable book id.

    MCP_REQUIRE_AUTH=true is what makes a public deployment safe: without it an
    empty MCP_TOKENS means "no auth", which is only acceptable on localhost.
    """

    def __init__(self, app):
        super().__init__(app)
        self.allowed_tokens = set(_TOKENS)
        self.allow_url_tokens = os.getenv("MCP_ALLOW_URL_TOKENS", "true").lower() in (
            "1", "true", "yes",
        )
        self.require_auth = os.getenv("MCP_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
        if self.require_auth and not self.allowed_tokens:
            logger.warning("MCP_REQUIRE_AUTH=true but MCP_TOKENS is empty -> rejecting everything")
        elif not self.allowed_tokens:
            logger.warning("MCP_TOKENS empty -> token auth DISABLED for %s", BASE_PATH)

    def _strip_token_segment(self, request, path, token=None) -> bool:
        """Rewrite /kindle/<seg>/... -> /kindle/... so the MCP app sees its own path."""
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2 or segs[0] != _safe_name:
            return False
        if token is not None and segs[1] != token:
            return False
        remainder = "/".join([_safe_name] + segs[2:])
        new_path = "/" + (
            remainder + "/" if path.endswith("/") and not remainder.endswith("/") else remainder
        )
        if new_path == BASE_PATH:
            new_path = STREAM_PATH
        request.scope["path"] = new_path
        if "raw_path" in request.scope:
            request.scope["raw_path"] = new_path.encode("utf-8")
        return True

    async def dispatch(self, request, call_next):
        path = request.url.path or "/"
        if not path.startswith(BASE_PATH):
            return await call_next(request)
        if path in ("/health", f"{BASE_PATH}/health") or path.startswith(
            (ASSETS_ROUTE, OUT_ROUTE)
        ):
            return await call_next(request)

        async def proceed(token_value, source):
            scope = MCP_TOKEN_CTX.set(token_value)
            request.state.mcp_token = token_value
            logger.info("Authenticated %s %s via %s", request.method, path, source)
            try:
                return await call_next(request)
            finally:
                MCP_TOKEN_CTX.reset(scope)

        if not self.allowed_tokens:
            if self.require_auth:
                return JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # Local mode: tolerate (and discard) a token-shaped path segment.
            self._strip_token_segment(request, path)
            return await call_next(request)

        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        token = (
            auth.split(" ", 1)[1].strip()
            if auth and auth.lower().startswith("bearer ")
            else None
        )
        if token and token in self.allowed_tokens:
            return await proceed(token, "header")

        if self.allow_url_tokens:
            url_token = request.query_params.get("token")
            if url_token and url_token in self.allowed_tokens:
                return await proceed(url_token, "query")
            segs = [s for s in path.split("/") if s]
            if len(segs) >= 2 and segs[0] == _safe_name and segs[1] in self.allowed_tokens:
                candidate = segs[1]
                self._strip_token_segment(request, path, candidate)
                return await proceed(candidate, "path")

        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )


app.add_middleware(TokenAuthMiddleware)


def main():
    logger.info("Starting %s MCP on port %s at %s", MCP_NAME, PORT, STREAM_PATH)
    uvicorn.run(
        app=app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=PORT,
        log_level=os.getenv("LOG_LEVEL", "info"),
        access_log=True,
        log_config=None,  # let access logs reach the root handler's token redaction
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=660,
    )


if __name__ == "__main__":
    main()
