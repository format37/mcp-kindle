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
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import smtplib
import tempfile
import threading
import time
from pathlib import Path

import anyio
import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage
from mcp.server.transport_security import TransportSecuritySettings

# BEFORE the project imports, not after: settings.py freezes the environment at
# import time (its ENV table is what the console calls "from .env"), and
# books/preview/library all read DATA_DIR at import too. Loading .env after them
# would leave every one of those computed from an environment that had not been
# populated yet. In the container .env is mounted at /app/.env and the CWD is
# /app; running from a checkout the CWD is usually backend/ while .env sits at
# the repo root — try all three rather than starting silently misconfigured.
for _candidate in (
    Path(".env"),
    Path(__file__).resolve().parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
):
    if _candidate.is_file():
        load_dotenv(_candidate)
        break

import books  # noqa: E402
import console  # noqa: E402
import diagrams  # noqa: E402
import fetch  # noqa: E402
import imagegen  # noqa: E402
import jobs  # noqa: E402
import library  # noqa: E402
import mathrender  # noqa: E402
import preview as preview_mod  # noqa: E402
import settings  # noqa: E402
import thumbs  # noqa: E402
from books import BookError  # noqa: E402
from html_tools import build_epub_from_html, convert_html_and_send  # noqa: E402
from kindle_tools import SMTP_TIMEOUT_S, send_epub_to_kindle  # noqa: E402
from library import LibraryError  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MCP_NAME = os.getenv("MCP_NAME", "kindle")
_safe_name = re.sub(r"[^a-z0-9_-]", "-", MCP_NAME.lower()).strip("-") or "service"
BASE_PATH = f"/{_safe_name}"
STREAM_PATH = f"{BASE_PATH}/"
ASSETS_ROUTE = f"{BASE_PATH}/assets"
OUT_ROUTE = f"{BASE_PATH}/out"
CONSOLE_ROUTE = f"{BASE_PATH}/console"

STATIC_DIR = Path(__file__).resolve().parent / "static"

PORT = int(os.getenv("PORT", "8018"))
SOFT_TIMEOUT_S = float(os.getenv("MCP_SOFT_TIMEOUT_S", "90"))
BOOK_MAX_AGE_DAYS = int(os.getenv("BOOK_MAX_AGE_DAYS", "30"))
PUBLIC_BASE_URL = (os.getenv("MCP_PUBLIC_BASE_URL") or "").rstrip("/") or None
MAX_SEND_BYTES = int(os.getenv("MAX_SEND_BYTES", str(24 * 1024 * 1024)))

MCP_TOKEN_CTX = contextvars.ContextVar("mcp_token", default=None)
_TOKENS = {t.strip() for t in os.getenv("MCP_TOKENS", "").split(",") if t.strip()}

#: Secret path segment guarding the web console. Deliberately a *different*
#: secret from MCP_TOKENS: the console URL is the one you paste into a phone or
#: leave open in a tab, and it can send books and read every credential's
#: presence — but it must never be the credential that can drive the MCP.
#: Unset, the console lives at the bare /kindle/console, which is only
#: acceptable when the server is bound to localhost.
CONSOLE_TOKEN = (os.getenv("CONSOLE_TOKEN") or "").strip()

#: Segments the console's own routes already use — a token equal to one of them
#: would shadow a real route rather than guard it.
_RESERVED_SEGMENTS = {"health", "assets", "out", "console", "library", "settings"}


def _validate_console_token() -> None:
    """Fail at startup rather than shipping a console that is open or shadowed."""
    public = bool((os.getenv("MCP_PUBLIC_BASE_URL") or "").strip())
    required = os.getenv("MCP_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
    if not CONSOLE_TOKEN:
        if required or public:
            raise SystemExit(
                "CONSOLE_TOKEN is empty on a publicly reachable deployment "
                "(MCP_REQUIRE_AUTH/MCP_PUBLIC_BASE_URL are set). The console can send "
                "books and change credentials, so it must not sit at a guessable path. "
                "Generate one with: openssl rand -hex 24"
            )
        return
    if CONSOLE_TOKEN in _RESERVED_SEGMENTS:
        raise SystemExit(f"CONSOLE_TOKEN must not be one of {sorted(_RESERVED_SEGMENTS)}")
    if CONSOLE_TOKEN in _TOKENS:
        raise SystemExit(
            "CONSOLE_TOKEN must differ from every MCP token — the whole point of the "
            "separate secret is that sharing a console URL cannot hand over the "
            "credential that drives the server."
        )
    if len(CONSOLE_TOKEN) < 16:
        raise SystemExit("CONSOLE_TOKEN is too short to be a secret; use at least 16 characters")


_validate_console_token()

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


_redaction_filter = _TokenRedactingFilter(_TOKENS | {CONSOLE_TOKEN})
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


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _record_send(
    book_id: str,
    *,
    ok: bool,
    title: str = "",
    recipient: str = "",
    size: int = 0,
    error: str = "",
) -> None:
    """Write the delivery outcome onto the book.

    Both drivers record it, so the console shows what an agent sent and
    ``book_status`` shows what you sent by hand — neither can silently mail the
    same book twice believing it was the first time. Never raises: a book that
    reached the Kindle but failed to note it down is not a failed send.
    """
    try:
        meta = books.load_meta(book_id)
        meta["last_sent"] = {
            "ok": ok,
            "at": _now_iso(),
            "title": title,
            "recipient": recipient,
            "bytes": size,
            "error": error[:500],
        }
        books.save_meta(meta)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail the send
        logger.warning("could not record the send for %s: %s", book_id, exc)


def _image_content(path: Path):
    """Wrap a rendered page image as an MCP image block the model can look at."""
    return MCPImage(data=Path(path).read_bytes(), format="jpeg").to_image_content()


def _preview_available() -> bool:
    try:
        return bool(preview_mod.preview_available()[0])
    except Exception:
        return shutil.which("pdftoppm") is not None


def _epub_for_book(
    book_id: str,
    cover: str,
    title: str,
    *,
    dest: Path | None = None,
    html: str | None = None,
) -> tuple[Path, str]:
    """Build the book's EPUB. Returns (path, resolved_title).

    ``cover`` falls back to the book's stored cover, which is what the console's
    cover picker sets — so choosing one there also applies to an agent's later
    ``send_book()`` with no argument. ``dest`` and ``html`` let the console build
    into its own directory from the exact document it snapshotted, instead of
    re-reading one an agent may have patched mid-build.
    """
    meta = books.load_meta(book_id)
    if html is None:
        html = books.get_document(book_id)
    out = dest or (books.out_dir(book_id) / "book.epub")
    out.parent.mkdir(parents=True, exist_ok=True)
    resolved = build_epub_from_html(
        html,
        (title or "").strip() or meta.get("title") or "Untitled",
        str(out),
        cover=(cover or "").strip() or (meta.get("cover") or "") or None,
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
    sent = meta.get("last_sent") or {}
    # Whoever sent it last — an agent through send_book, or a human through the
    # console — this is where that shows up, so neither re-sends blind.
    if sent.get("ok"):
        sent_line = f"sent:    {sent.get('at')} to {sent.get('recipient')} as {sent.get('title')!r}\n"
    elif sent:
        sent_line = f"sent:    FAILED at {sent.get('at')} — {sent.get('error')}\n"
    else:
        sent_line = "sent:    never\n"
    return (
        f"book_id: {meta['book_id']}\n"
        f"title:   {meta.get('title')}\n"
        f"document: {doc_chars} chars"
        f"{' (not set yet — call set_document)' if not doc_chars else ''}\n"
        f"cover:   {meta.get('cover') or '- (none chosen)'}\n"
        f"updated: {meta.get('updated')}\n"
        f"{sent_line}"
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
    ready, why = settings.delivery_ready()
    if not ready:
        return (
            f"Error: Kindle delivery is not configured — {why}. Set SENDER_EMAIL, "
            f"GMAIL_APP_PASS and RECIPIENT_EMAIL, or fill them in on the console's "
            f"settings page."
        )
    # Read once, here: _work runs on a worker thread and the operator could be
    # saving new credentials in the console at the same moment. One consistent
    # triple beats a torn read between login and recipient.
    sender, password, recipient = settings.delivery()

    def _work():
        resolved = title or book_id
        try:
            epub_path, resolved = _epub_for_book(book_id, cover, title)
            size = epub_path.stat().st_size
            if size > MAX_SEND_BYTES:
                raise RuntimeError(
                    f"EPUB is {size // (1024 * 1024)} MB but the outbound mail limit is "
                    f"{MAX_SEND_BYTES // (1024 * 1024)} MB. Drop or shrink images — "
                    "preview_book lists the biggest ones."
                )
            send_epub_to_kindle(str(epub_path), sender, password, recipient, resolved)
        except Exception as exc:
            # Recorded on the book, not only raised: the console's delivery panel
            # would otherwise say "never sent" about a book an agent has been
            # failing to send all morning.
            _record_send(book_id, ok=False, title=resolved, recipient=recipient, error=str(exc))
            raise
        _record_send(book_id, ok=True, title=resolved, recipient=recipient, size=size)
        return resolved, size

    resolved, size = await anyio.to_thread.run_sync(_work)
    msg = f"Sent '{resolved}' ({size // 1024} KB) to {recipient}."
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
    ready, why = settings.delivery_ready()
    if not ready:
        return (
            f"Error: Kindle delivery is not configured — {why}. Set SENDER_EMAIL, "
            f"GMAIL_APP_PASS and RECIPIENT_EMAIL, or fill them in on the console's "
            f"settings page."
        )
    sender, password, recipient = settings.delivery()
    try:
        return convert_html_and_send(
            html_text=html_text,
            title=title,
            sender_email=sender,
            sender_password=password,
            recipient_email=recipient,
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
# web console
# --------------------------------------------------------------------------
#
# The second driver of this server. Everything below is plain server-rendered
# HTML out of this same process — see console.py for why there is no API in
# between. Long work (a WeasyPrint build, an SMTP send) goes through jobs.py and
# the page follows it with a meta refresh, so no request is ever held open for a
# minute and no JavaScript is needed to watch one.

#: Extra room over the payload cap for multipart framing and field names.
_MULTIPART_SLACK = 256 * 1024

#: How many rendered pages the console shows. "all" is clamped by
#: preview.PREVIEW_MAX_PAGES, and that clamp note is surfaced on the page.
CONSOLE_PREVIEW_PAGES = os.getenv("CONSOLE_PREVIEW_PAGES", "all")
#: Narrower than the MCP's inline pages: these are looked at in a browser at
#: ~300 px tall, and twenty full-size ones would be several MB of page weight.
CONSOLE_PAGE_PX = int(os.getenv("CONSOLE_PAGE_PX", "800"))

_FLASH_TTL_S = 180
_flash_store: dict[str, tuple[float, str]] = {}
_flash_lock = threading.Lock()

#: (kind, book_id) -> job id, so a double-click or a second tab joins the
#: running build instead of starting a rival one over the same output files.
_jobs_by_key: dict[tuple[str, str], str] = {}
_jobs_lock = threading.Lock()

_JOB_LABELS = {"preview": "Preview build", "send": "Send to Kindle"}

_TEST_BOOK_HTML = """
<h1>Kindle delivery test</h1>
<p>If you are reading this on the device, the console's mail path works: the
sender is an approved one, the app password is valid, and Amazon accepted the
attachment. Nothing else in this book matters — delete it.</p>
"""


# --- flash messages -------------------------------------------------------
# One-shot notices for POST-redirect-GET, held in process rather than passed
# through the query string: the console renders them into the page, and a URL
# that carries its own page text is a URL someone else can compose for you.

def _flash(message: str, *, bad: bool = False) -> str:
    now = time.time()
    token = os.urandom(9).hex()
    with _flash_lock:
        for key, (expiry, _body) in list(_flash_store.items()):
            if expiry < now:
                _flash_store.pop(key, None)
        _flash_store[token] = (now + _FLASH_TTL_S, console.notice(message, bad=bad))
    return token


def _take_flash(request) -> str:
    token = request.query_params.get("m") or ""
    if not token:
        return ""
    with _flash_lock:
        entry = _flash_store.pop(token, None)
    return entry[1] if entry and entry[0] >= time.time() else ""


def _redirect(base: str, path: str = "", *, message: str = "", bad: bool = False, job: str = ""):
    """POST-redirect-GET, so a reload never repeats a delete or a send."""
    params = []
    if job:
        params.append(f"job={job}")
    if message:
        params.append(f"m={_flash(message, bad=bad)}")
    url = f"{base}{path}" + (f"?{'&'.join(params)}" if params else "")
    return RedirectResponse(url, status_code=303)


# --- access ---------------------------------------------------------------

def _console_base(request) -> str:
    """The console's own URL prefix, preserving the token segment if one is used."""
    if CONSOLE_TOKEN and request.path_params.get("token") == CONSOLE_TOKEN:
        return f"{BASE_PATH}/{CONSOLE_TOKEN}/console"
    return CONSOLE_ROUTE


def _console_denied(request) -> bool:
    """True when a token is configured and this request did not present it."""
    return bool(CONSOLE_TOKEN) and request.path_params.get("token") != CONSOLE_TOKEN


#: Host header values the console answers to. The MCP transport has its own
#: DNS-rebinding protection inside the mcp library, but console routes are plain
#: Starlette routes registered ahead of the mount and never reach it.
_CONSOLE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", _safe_name, f"mcp-{_safe_name}"}
_CONSOLE_HOSTS |= {h.strip().lower() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()}
if PUBLIC_BASE_URL:
    with contextlib.suppress(Exception):
        from urllib.parse import urlsplit as _urlsplit

        _CONSOLE_HOSTS.add((_urlsplit(PUBLIC_BASE_URL).hostname or "").lower())
_CONSOLE_HOSTS.discard("")


def _bad_host(request) -> bool:
    """True when the Host header is not one this console answers to.

    Without this, a page on the attacker's domain can point DNS at 127.0.0.1 and
    then *read* the console — the book list, any doc.html, the settings page —
    as same-origin, because the browser believes it is talking to its own host.
    """
    host = (request.headers.get("host") or "").split(",")[0].strip().lower()
    if not host:
        return False  # HTTP/1.0 or a direct socket test; nothing to rebind
    return host.rsplit(":", 1)[0].strip("[]").lower() not in {
        h.strip("[]") for h in _CONSOLE_HOSTS
    }


def _cross_site(request) -> bool:
    """True when a state-changing request did not come from the console itself.

    The console's forms are cookie-less — the URL *is* the credential — which
    sounds like it rules CSRF out and does not: in the default local deployment
    CONSOLE_TOKEN is empty, so the URL is guessable, and a form POST is
    CORS-safelisted (no preflight, sent regardless of origin). Any page the
    operator happens to visit could therefore repoint the Send-to-Kindle address
    at itself, or trigger a send, on a console it cannot read.

    Sec-Fetch-Site is sent by every current browser and is the reliable signal;
    Origin is the fallback. Neither present means a non-browser client (curl, a
    script, a test) and is allowed through — those are not the confused deputy.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        return site not in ("same-origin", "none")
    origin = (request.headers.get("origin") or "").strip()
    if origin:
        host = (request.headers.get("host") or "").strip()
        return origin.rstrip("/").lower() not in (
            f"http://{host}".lower(), f"https://{host}".lower(),
        )
    return False


def console_page(handler):
    """Gate a console handler on CONSOLE_TOKEN, the Host header, and the origin.

    404 rather than 401 for a bad token because there is nothing to authenticate
    *with* here: the URL is the credential, and a 401 would confirm that a
    console exists at this path to anyone scanning for one.
    """

    @functools.wraps(handler)
    async def wrapper(request):
        if _console_denied(request):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if _bad_host(request):
            logger.warning("console: refused Host %r", request.headers.get("host"))
            return JSONResponse(
                {"detail": "Invalid Host header. Add this hostname to MCP_ALLOWED_HOSTS."},
                status_code=421,
            )
        if _cross_site(request):
            logger.warning(
                "console: refused a cross-site %s from origin %r",
                request.method, request.headers.get("origin"),
            )
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        return await handler(request)

    return wrapper


# --- jobs -----------------------------------------------------------------

def _live_job(kind: str, book_id: str) -> str:
    with _jobs_lock:
        job_id = _jobs_by_key.get((kind, book_id), "")
    if not job_id:
        return ""
    snap = _job_snapshot(job_id)
    return job_id if snap and snap.get("status") == "running" else ""


def _live_job_any(book_id: str) -> str:
    """Any console job still running for this book, of either kind."""
    for kind in _JOB_LABELS:
        running = _live_job(kind, book_id)
        if running:
            return running
    return ""


def _start_job(kind: str, book_id: str, fn) -> tuple[str, bool]:
    """Submit ``fn`` unless *any* console job is already running for this book.

    Per book, not per kind. A preview and a send both build the book's EPUB, and
    although they now write different filenames they still share the assets, the
    document and the diagram renderers — and the whole point of pressing send
    after a preview is that the thing you looked at is the thing that goes. Two
    at once also doubles the WeasyPrint/JVM load on a small VPS for no gain.
    """
    # Check and submit under one lock hold: two requests arriving together
    # (a double-click, two tabs) would otherwise both see "nothing running" and
    # both submit.
    with _jobs_lock:
        for other in _JOB_LABELS:
            running = _jobs_by_key.get((other, book_id), "")
            snap = _job_snapshot(running) if running else None
            if snap and snap.get("status") == "running":
                return running, False
        job_id = jobs.submit(fn, f"{kind}:{book_id}")
        _jobs_by_key[(kind, book_id)] = job_id
        return job_id, True


def _job_snapshot(job_id: str) -> dict | None:
    """A job's state, or None if it is unknown or expired.

    The exception text is deliberately never shown: jobs.JobError lists the
    recent job labels to help a caller find the right id, and those labels carry
    other books' ids — which are the capability that makes a book's asset URLs
    safe to serve unauthenticated.
    """
    if not job_id:
        return None
    try:
        return jobs.get(job_id)
    except Exception as exc:  # noqa: BLE001 — JobError and anything it wraps
        logger.info("console: job %.12s… not available (%s)", job_id, type(exc).__name__)
        return None


def _display_job(job_id: str) -> dict | None:
    snap = _job_snapshot(job_id)
    if not snap:
        return None
    kind = str(snap.get("label") or "").split(":", 1)[0]
    return {**snap, "kind": kind, "label": _JOB_LABELS.get(kind, "Job")}


# --- book workspace helpers ----------------------------------------------

def _console_out_path(book_id: str) -> Path:
    """The console's build directory — read-only callers use this, no mkdir."""
    return books.book_dir(book_id) / "out" / "console"


def _console_out(book_id: str) -> Path:
    """Same, created.

    Separate from ``out/`` on purpose: ``preview_book()`` from an agent writes
    ``page_NNN.jpg`` there for whatever range it asked for, and two writers
    sharing one directory means the console can show a page from someone else's
    build. Nothing in here is ever touched by the MCP side.
    """
    path = _console_out_path(book_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _doc_sha(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8", "replace")).hexdigest()[:16]


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _referenced_names(html: str) -> dict[str, int]:
    """Bare image filenames the document actually points at, with counts.

    Deleting an asset nothing references is free; deleting a referenced one
    leaves an ``<img>`` resolving to nothing — and html_tools drops it silently,
    so the EPUB still builds and only the lint report notices. The tiles say
    which is which before you click.

    Names keep their case: they are used to stat the real files, and the build
    resolves them case-sensitively too. The tile lookup folds case itself.
    """
    if not html:
        return {}
    from bs4 import BeautifulSoup  # noqa: PLC0415 — lazy: not needed to serve a page
    from urllib.parse import unquote  # noqa: PLC0415

    counts: dict[str, int] = {}
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — a malformed document must still render here
        return {}
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith(("data:", "http://", "https://", "//")):
            continue
        name = Path(unquote(src.split("?")[0].split("#")[0])).name
        if name and name not in (".", ".."):
            counts[name] = counts.get(name, 0) + 1
    return counts


def _inputs_sha(book_id: str, names: set[str]) -> str:
    """Digest of the image files a build would actually pull in.

    The document's hash is not the whole input to an EPUB. Swap the cover,
    re-upload an illustration under the same name, delete one — the document is
    byte-identical and every page proof on screen is now a picture of something
    that no longer exists, under a lint report that says "clean". This is what
    makes the freshness check tell the truth about the build rather than about
    the text.

    Resolution order matches ``html_tools._resolve_image_multi``: the book's own
    assets shadow the flat data folder.
    """
    rows: list[str] = []
    try:
        roots = [books.assets_dir(book_id), books.DATA_DIR]
    except BookError:
        return ""
    for name in sorted(names):
        if not name or name != Path(name).name:
            continue
        for root in roots:
            try:
                stat = (root / name).stat()
            except OSError:
                continue
            rows.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
            break
        else:
            rows.append(f"{name}:missing")
    return hashlib.sha256("|".join(rows).encode()).hexdigest()[:16]


def _preview_record(book_id: str) -> dict | None:
    try:
        raw = (_console_out_path(book_id) / "preview.json").read_text(encoding="utf-8")
        record = json.loads(raw)
    except (OSError, ValueError, BookError):
        return None
    return record if isinstance(record, dict) else None


def _build_preview(book_id: str, html: str, title: str, cover: str) -> dict:
    """Build the EPUB, lint it, rasterise its pages; record all of it on disk.

    Runs on a job thread. Failures are recorded rather than raised so the page
    can show what went wrong instead of a vanished job.
    """
    out = _console_out(book_id)
    # Three freshness keys, because a preview can go stale three ways. doc_sha
    # is exact and used on the book page; doc_chars is cheap and used on the
    # index, where hashing every book's document would cost megabytes of IO;
    # inputs_sha covers the images, which the document's own bytes say nothing
    # about. cover is recorded separately because it can change with no image
    # changing at all.
    record: dict = {
        "built": _now_iso(),
        "doc_sha": _doc_sha(html),
        "doc_chars": len(html),
        "cover": cover,
        "inputs_sha": _build_inputs_sha(book_id, html, cover),
    }
    try:
        epub_path, resolved = _epub_for_book(
            book_id, cover, title, dest=out / "book.epub", html=html
        )
        record.update(
            title=resolved, epub=True, epub_bytes=epub_path.stat().st_size,
            findings=preview_mod.lint_epub(epub_path),
        )
        summary = preview_mod.summarize_epub(epub_path)
        record.update(chapters=summary["chapters"], images=summary["images"])

        # Page images from a longer previous run would otherwise stay on disk
        # and read as pages of a document that no longer exists.
        for stale in out.glob("page_*.jpg"):
            stale.unlink(missing_ok=True)
        record["page_images"] = []

        pdf = preview_mod.epub_to_pdf(epub_path, out / "preview.pdf")
        total = preview_mod.page_count(pdf)
        _first, _last, note = preview_mod.resolve_page_range(CONSOLE_PREVIEW_PAGES, total)
        images = preview_mod.pdf_to_images(
            pdf, out, pages=CONSOLE_PREVIEW_PAGES, max_px=CONSOLE_PAGE_PX
        )
        record.update(
            pdf=True, pages=total, note=note, page_images=[p.name for p in images]
        )
    except Exception as exc:  # noqa: BLE001 — every failure belongs on the page
        record["error"] = f"{type(exc).__name__}: {exc}"[:800]
        logger.warning("console preview failed for %s: %s", book_id, exc)
    (out / "preview.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def _console_send(
    book_id: str, html: str, title: str, cover: str,
    sender: str, password: str, recipient: str,
) -> str:
    out = _console_out(book_id)
    resolved = title or book_id
    try:
        # Its own filename, never the preview's book.epub: a send that read the
        # file while a preview was rewriting it would mail a truncated zip and
        # then record it as SENT. _start_job also refuses to run the two at
        # once — this is the second lock on the same door, because the cost of
        # being wrong is an unrecallable email.
        epub_path, resolved = _epub_for_book(
            book_id, cover, title, dest=out / "send.epub", html=html
        )
        size = epub_path.stat().st_size
        if size > MAX_SEND_BYTES:
            raise RuntimeError(
                f"the EPUB is {size // (1024 * 1024)} MB but the outbound mail limit is "
                f"{MAX_SEND_BYTES // (1024 * 1024)} MB — drop or shrink the biggest images"
            )
        send_epub_to_kindle(str(epub_path), sender, password, recipient, resolved)
    except Exception as exc:
        _record_send(book_id, ok=False, title=resolved, recipient=recipient, error=str(exc))
        raise
    _record_send(book_id, ok=True, title=resolved, recipient=recipient, size=size)
    return f"Sent '{resolved}' ({size // 1024} KB) to {recipient}."


def _store_book_asset(book_id: str, filename: str, data: bytes) -> dict:
    """Upload one image into a book, without ever clobbering an existing asset.

    ``store_asset`` overwrites by stem *across extensions* — uploading
    ``cover.png`` over an existing ``cover.jpg`` would delete the file the
    document references. Reserving a fresh name first is what prevents that; the
    reservation is handed back if the store fails, so a rejected upload does not
    push the next ``cover`` to ``cover-2``.
    """
    if len(data) > library.MAX_UPLOAD_BYTES:
        raise BookError(
            f"{filename}: {len(data) // (1024 * 1024)} MB exceeds the "
            f"{library.MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit"
        )
    fresh = books.unique_asset_name(book_id, filename, ".png")
    try:
        return books.store_asset(book_id, fresh, data)
    except Exception:
        books.release_stem(book_id, fresh)
        raise


def _reject_oversize(request):
    """413 before a byte is buffered.

    Starlette's ``max_part_size`` bounds *field* parts only — file parts are
    spooled unbounded — so the library's own length check runs after the upload
    is already on disk and in memory.
    """
    raw = request.headers.get("content-length") or ""
    try:
        length = int(raw)
    except ValueError:
        return None
    if length > library.MAX_UPLOAD_BYTES + _MULTIPART_SLACK:
        return JSONResponse(
            {"detail": f"Payload too large (limit {library.MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"},
            status_code=413,
        )
    return None


async def _read_uploads(request) -> list[tuple[str, bytes]]:
    """(filename, bytes) for every file part, with the spool cleaned up after.

    Each part is read with an explicit bound rather than ``read()``: Starlette
    applies ``max_part_size`` to *field* parts only — file parts spool
    unbounded — so a request that arrives without a usable Content-Length would
    otherwise be pulled into memory whole. One byte over the limit is enough to
    reject it, and the rest is never read.
    """
    limit = library.MAX_UPLOAD_BYTES
    out: list[tuple[str, bytes]] = []
    async with request.form(max_files=16, max_fields=8) as form:
        for item in form.getlist("file"):
            if hasattr(item, "read"):
                out.append((getattr(item, "filename", "") or "image", await item.read(limit + 1)))
    return out


# --- pages ----------------------------------------------------------------

def _index_view() -> tuple[list[dict], dict]:
    rows = books.list_books(limit=500)
    assets = 0
    total = 0
    for row in rows:
        book_id = str(row.get("book_id"))
        size = _dir_bytes(books.BOOKS_DIR / book_id)
        row["bytes"] = size
        total += size
        assets += int(row.get("asset_count") or 0)
        record = _preview_record(book_id)
        row["has_preview"] = bool(record and not record.get("error"))
        row["preview_stale"] = bool(
            record and record.get("doc_chars") != row.get("doc_chars")
        )
        sent = row.get("last_sent") or {}
        row["sent_at"] = sent.get("at") if sent.get("ok") else ""
    usage = library.usage()
    return rows, {
        "assets": assets,
        "bytes": total + usage["bytes"],
        "library_count": usage["count"],
        "recipient": settings.delivery()[2],
    }


@console_page
async def console_slash(request):
    """Redirect a trailing slash to the canonical path.

    Starlette's own ``redirect_slashes`` never fires here: ``Mount("/")`` matches
    every path, so the router returns on it before reaching the fallback, and
    ``/kindle/console/`` — the shape a person actually types, and the one a proxy
    may normalise to — reaches the MCP transport and gets a bare "Not Found".
    """
    # Derived from the request path, so the token prefix comes along for free.
    target = (request.url.path or "").rstrip("/") or _console_base(request)
    return RedirectResponse(target, status_code=308)


@console_page
async def console_index(request):
    base = _console_base(request)
    rows, totals = await anyio.to_thread.run_sync(_index_view)
    return HTMLResponse(
        console.render_index(
            rows, base=base, totals=totals,
            delivery=settings.delivery_ready(), notices=_take_flash(request),
        ),
        headers={"Cache-Control": "no-store"},
    )


def _build_inputs_sha(book_id: str, html: str, cover: str) -> str:
    names = set(_referenced_names(html))
    if cover:
        names.add(cover)
    return _inputs_sha(book_id, names)


def _book_view(book_id: str) -> tuple:
    meta = books.load_meta(book_id)  # BookError when the id is unknown
    try:
        document = books.get_document(book_id)
    except BookError:
        document = ""
    assets = books.list_assets(book_id)
    used = _referenced_names(document)
    folded: dict[str, int] = {}
    for name, count in used.items():
        folded[name.lower()] = folded.get(name.lower(), 0) + count
    for asset in assets:
        asset["uses"] = folded.get(str(asset["filename"]).lower(), 0)

    cover = str(meta.get("cover") or "")
    doc_sha = _doc_sha(document)
    record = _preview_record(book_id)
    stale = bool(
        record
        and document
        and (
            record.get("doc_sha") != doc_sha
            # An older record has no inputs_sha; treat that as fresh rather than
            # crying wolf about every preview built before this existed.
            or (
                record.get("inputs_sha") is not None
                and record["inputs_sha"] != _build_inputs_sha(book_id, document, cover)
            )
            or (record.get("cover") is not None and record["cover"] != cover)
        )
    )
    return meta, document, assets, record, stale, doc_sha


@console_page
async def console_book(request):
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    try:
        meta, document, assets, record, stale, doc_sha = await anyio.to_thread.run_sync(
            functools.partial(_book_view, book_id)
        )
    except BookError as exc:
        return HTMLResponse(console.render_error(str(exc), base=base), status_code=404)
    # Fall back to whatever is running for this book when the URL has no job id:
    # arriving from the index while a build is in flight should still show the
    # banner and the disabled buttons, not a page that looks idle.
    job_id = request.query_params.get("job", "") or _live_job_any(book_id)
    return HTMLResponse(
        console.render_book(
            meta, base=base, document=document, assets=assets, preview=record,
            stale=stale, delivery=settings.delivery_ready(),
            job=_display_job(job_id), notices=_take_flash(request), doc_sha=doc_sha,
        ),
        headers={"Cache-Control": "no-store"},
    )


@console_page
async def console_document(request):
    """Save the title, cover, notes and the document itself."""
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    form = await request.form()
    html = str(form.get("html") or "")
    title = str(form.get("title") or "").strip()
    cover = str(form.get("cover") or "").strip()
    notes = str(form.get("notes") or "").strip()
    seen = str(form.get("doc_sha") or "")

    def _work() -> str:
        meta = books.load_meta(book_id)
        if cover:
            books.asset_ref(book_id, cover)  # refuse a cover that is not there
        # Optimistic lock. The other driver of this server is an agent running
        # patch_document in a loop; a tab left open on an old version, then a
        # one-word title edit, would otherwise post the stale textarea back over
        # everything it wrote — silently, and with no copy of the lost text.
        current = ""
        with contextlib.suppress(BookError):
            current = books.get_document(book_id)
        if seen and current and _doc_sha(current) != seen:
            raise BookError(
                "The document changed on the server while this page was open — an "
                "agent edited the book. Nothing was saved. Reload to see the current "
                "text, then reapply your edit."
            )
        if html.strip():
            books.set_document(book_id, html)
            meta = books.load_meta(book_id)  # set_document moved doc_chars
        elif meta.get("doc_chars"):
            raise BookError(
                "Refusing to empty the document. Clearing the whole book by accident is "
                "one keystroke away in a textarea; delete the book if that is what you "
                "meant."
            )
        meta["title"] = title[:300] or meta.get("title")
        meta["notes"] = notes[:2000]
        meta["cover"] = cover
        books.save_meta(meta)
        return f"Saved — {len(html):,} characters" + (f", cover {cover}." if cover else ".")

    try:
        message = await anyio.to_thread.run_sync(_work)
    except BookError as exc:
        return _redirect(base, f"/{book_id}", message=str(exc), bad=True)
    return _redirect(base, f"/{book_id}", message=message)


def _build_inputs(book_id: str) -> tuple[str, str, str]:
    meta = books.load_meta(book_id)
    return books.get_document(book_id), str(meta.get("title") or ""), str(meta.get("cover") or "")


@console_page
async def console_preview(request):
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    try:
        html, title, cover = await anyio.to_thread.run_sync(
            functools.partial(_build_inputs, book_id)
        )
    except BookError as exc:
        return _redirect(base, f"/{book_id}", message=str(exc), bad=True)
    # The document is snapshotted here, not re-read on the worker: an agent
    # patching the book mid-build would otherwise produce a preview of text the
    # textarea above never showed.
    job_id, started = _start_job(
        "preview", book_id, functools.partial(_build_preview, book_id, html, title, cover)
    )
    return _redirect(
        base, f"/{book_id}", job=job_id,
        message="" if started else "That preview was already building — showing it.",
    )


@console_page
async def console_send(request):
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    ready, why = settings.delivery_ready()
    if not ready:
        return _redirect(base, f"/{book_id}", message=f"Cannot send — {why}.", bad=True)
    try:
        html, title, cover = await anyio.to_thread.run_sync(
            functools.partial(_build_inputs, book_id)
        )
    except BookError as exc:
        return _redirect(base, f"/{book_id}", message=str(exc), bad=True)
    sender, password, recipient = settings.delivery()
    job_id, started = _start_job(
        "send", book_id,
        functools.partial(_console_send, book_id, html, title, cover, sender, password, recipient),
    )
    return _redirect(
        base, f"/{book_id}", job=job_id,
        message="" if started else "That book is already being sent.",
    )


@console_page
async def console_upload(request):
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    oversize = _reject_oversize(request)
    if oversize:
        return oversize
    uploads = await _read_uploads(request)
    stored: list[str] = []
    failed: list[str] = []
    for filename, data in uploads:
        try:
            info = await anyio.to_thread.run_sync(
                functools.partial(_store_book_asset, book_id, filename, data)
            )
            stored.append(info["filename"])
        except (BookError, LibraryError, OSError) as exc:
            failed.append(f"{filename}: {exc}")
    if not uploads:
        return _redirect(base, f"/{book_id}", message="No file was selected.", bad=True)
    message = f"Stored {', '.join(stored)}." if stored else ""
    if failed:
        message = (message + " " if message else "") + f"Failed — {'; '.join(failed)}"
    return _redirect(base, f"/{book_id}", message=message.strip(), bad=bool(failed and not stored))


@console_page
async def console_asset_delete(request):
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    form = await request.form()
    filename = str(form.get("filename") or "")

    def _work() -> str:
        books.delete_asset(book_id, filename)
        meta = books.load_meta(book_id)
        if meta.get("cover") == filename:
            # A cover pointing at a deleted file builds an EPUB with no cover
            # and says nothing about it.
            meta["cover"] = ""
            books.save_meta(meta)
            return f"Deleted {filename}; it was the cover, so the book now has none."
        return f"Deleted {filename}."

    try:
        message = await anyio.to_thread.run_sync(_work)
    except BookError as exc:
        return _redirect(base, f"/{book_id}", message=str(exc), bad=True)
    return _redirect(base, f"/{book_id}", message=message)


@console_page
async def console_book_delete(request):
    base = _console_base(request)
    book_id = request.path_params["book_id"]
    if _live_job("preview", book_id) or _live_job("send", book_id):
        return _redirect(
            base, f"/{book_id}",
            message="A build is still running for this book. Wait for it to finish — "
                    "deleting now would have it recreate the directory it is writing into.",
            bad=True,
        )
    try:
        await anyio.to_thread.run_sync(functools.partial(books.delete_book, book_id))
    except BookError as exc:
        return HTMLResponse(console.render_error(str(exc), base=base), status_code=404)
    return _redirect(base, message=f"Deleted {book_id}.")


@console_page
async def console_delete_selected(request):
    """Delete every checked book, then redirect with a count.

    Partial failure is normal, not an error: a book can be deleted in another
    tab, or start a build, between the page render and the submit. Each id is
    attempted independently and the outcome is reported.
    """
    base = _console_base(request)
    form = await request.form()
    ids = [str(v) for v in form.getlist("id")]
    deleted = 0
    skipped: list[str] = []
    for book_id in ids:
        if _live_job("preview", book_id) or _live_job("send", book_id):
            skipped.append(f"{book_id} (still building)")
            continue
        try:
            await anyio.to_thread.run_sync(functools.partial(books.delete_book, book_id))
            deleted += 1
        except BookError as exc:
            skipped.append(f"{book_id} ({exc})")
    message = f"Deleted {deleted} book{'s' if deleted != 1 else ''}."
    if skipped:
        message += f" Skipped {len(skipped)}: {'; '.join(skipped[:5])}"
    return _redirect(base, message=message, bad=bool(skipped and not deleted))


# --- library --------------------------------------------------------------

@console_page
async def console_library(request):
    base = _console_base(request)
    rows = await anyio.to_thread.run_sync(library.list_images)
    return HTMLResponse(
        console.render_library(
            rows, base=base, notices=_take_flash(request), data_dir=str(library.DATA_DIR)
        ),
        headers={"Cache-Control": "no-store"},
    )


@console_page
async def console_library_upload(request):
    base = _console_base(request)
    oversize = _reject_oversize(request)
    if oversize:
        return oversize
    uploads = await _read_uploads(request)
    stored: list[str] = []
    failed: list[str] = []
    for filename, data in uploads:
        try:
            info = await anyio.to_thread.run_sync(
                functools.partial(library.store_upload, filename, data)
            )
            stored.append(info["filename"])
        except (LibraryError, OSError) as exc:
            failed.append(f"{filename}: {exc}")
    if not uploads:
        return _redirect(base, "/library", message="No file was selected.", bad=True)
    message = f"Stored {', '.join(stored)}." if stored else ""
    if failed:
        message = (message + " " if message else "") + f"Failed — {'; '.join(failed)}"
    return _redirect(base, "/library", message=message.strip(), bad=bool(failed and not stored))


@console_page
async def console_library_delete(request):
    base = _console_base(request)
    form = await request.form()
    filename = str(form.get("filename") or "")
    try:
        await anyio.to_thread.run_sync(functools.partial(library.delete, filename))
    except LibraryError as exc:
        return _redirect(base, "/library", message=str(exc), bad=True)
    return _redirect(base, "/library", message=f"Deleted {filename}.")


# --- settings -------------------------------------------------------------

def _capabilities() -> dict[str, bool]:
    """What this container can actually do, probed rather than configured."""
    caps = {
        "Kindle delivery": settings.delivery_ready()[0],
        "Image generation (Gemini)": imagegen.available(),
        "Preview rasteriser": _preview_available(),
        "Math (matplotlib)": mathrender.available(),
    }
    caps.update({f"Diagrams: {name}": ok for name, ok in diagrams.available_engines().items()})
    return caps


def _delivery_test() -> tuple[bool, str]:
    """Prove the mail path as far as it can honestly be proven.

    Two stages, because either alone lies. An SMTP login proves the app password
    and that port 587 is open, and says nothing about whether Amazon will accept
    the mail — an unapproved sender is accepted by Gmail and dropped silently.
    So the second stage mails a real one-page book, and the wording never claims
    delivery, only hand-off.
    """
    ready, why = settings.delivery_ready()
    if not ready:
        return False, f"Cannot test — {why}."
    sender, password, recipient = settings.delivery()
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=SMTP_TIMEOUT_S) as server:
            server.starttls()
            server.login(sender, password)
    except smtplib.SMTPAuthenticationError:
        return False, (
            "Gmail rejected the sign-in. The password must be the 16-character app "
            "password from Google account -> Security -> App passwords, not your login "
            "password, and 2-step verification has to be on for that menu to exist."
        )
    except (OSError, smtplib.SMTPException) as exc:
        return False, (
            f"Could not reach Gmail ({exc}). Outbound SMTP on port 587 may be blocked "
            f"on this host."
        )
    try:
        with tempfile.TemporaryDirectory(prefix="kindle_test_") as tmp:
            path = Path(tmp) / "test.epub"
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")
            title = f"Delivery test {stamp}"
            build_epub_from_html(_TEST_BOOK_HTML, title, str(path))
            send_epub_to_kindle(str(path), sender, password, recipient, title)
    except Exception as exc:  # noqa: BLE001 — report, never crash the settings page
        return False, f"Signed in fine, but the test book failed to send: {exc}"
    return True, (
        f"Signed in as {sender} and handed a one-page test book to Gmail for {recipient}. "
        f"Gmail accepting it is not delivery: if it does not reach the device within a "
        f"minute or two, {sender} is not an Approved Personal Document E-mail on Amazon, "
        f"or the Send-to-Kindle address is wrong."
    )


@console_page
async def console_settings(request):
    base = _console_base(request)
    if request.method == "POST":
        form = await request.form()
        # A secret arrives empty unless it was retyped. Empty therefore means
        # "unchanged" and is simply not passed to save(); dropping one takes the
        # explicit clear_<field> checkbox. An address field is pre-filled, so
        # there empty really does mean "clear it".
        fields: dict[str, str] = {}
        for key in settings.FIELDS:
            typed = str(form.get(key) or "").strip()
            if key not in settings.SECRETS:
                # An address field is pre-filled with the live value, so a bare
                # SAVE posts back whatever .env supplied. Storing that would pin
                # it: the file wins over the environment, and the operator's next
                # .env edit would silently stop taking effect. Passing "" clears
                # the override instead, leaving the field env-backed.
                fields[key] = "" if typed == settings.ENV.get(key, "") else typed
            elif form.get(f"clear_{key}"):
                fields[key] = ""
            elif typed:
                fields[key] = typed
        action = str(form.get("action") or "save")
        await anyio.to_thread.run_sync(functools.partial(settings.save, **fields))
        if action == "test":
            ok, detail = await anyio.to_thread.run_sync(_delivery_test)
            return _redirect(base, "/settings", message=detail, bad=not ok)
        return _redirect(
            base, "/settings",
            message="Settings saved. Secrets left blank were kept as they were.",
        )
    return HTMLResponse(
        console.render_settings(
            settings.public_view(), base=base,
            notices=_take_flash(request), capabilities=_capabilities(),
        ),
        # Never cached, and never stored: the page names which credentials exist.
        headers={"Cache-Control": "no-store, private"},
    )


# --- files ----------------------------------------------------------------

#: What the console will serve out of a book. SVG is absent deliberately: it can
#: carry script, and these are served from the console's own origin.
_SERVEABLE_EXT = set(library.ALLOWED_EXT) | {".pdf", ".epub"}

_EXT_TYPES = {
    **library.ALLOWED_EXT,
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
}


def _file_response(path: Path, *, download: bool = False, cache: str = "no-store"):
    headers = {"Cache-Control": cache, "X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(
        path, media_type=_EXT_TYPES.get(path.suffix.lower()), headers=headers
    )


async def _thumbnail(source: Path):
    try:
        path = await anyio.to_thread.run_sync(functools.partial(thumbs.thumb_for, source))
    except thumbs.ThumbError:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # A minute of cache: long enough that a grid of ninety images is not
    # re-fetched while you scroll it, short enough that replacing an image shows
    # up without a hard reload.
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
    )


@console_page
async def console_asset_file(request):
    try:
        path = books.asset_ref(request.path_params["book_id"], request.path_params["filename"])
    except BookError:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return _file_response(path)


@console_page
async def console_asset_thumb(request):
    try:
        source = books.asset_ref(request.path_params["book_id"], request.path_params["filename"])
    except BookError:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return await _thumbnail(source)


@console_page
async def console_out_file(request):
    """Serve the console's own build output — page images, the PDF, the EPUB."""
    filename = request.path_params["filename"]
    try:
        base = _console_out_path(request.path_params["book_id"]).resolve()
        target = (base / filename).resolve()
        if target.parent != base or not target.is_file():
            raise BookError("not found")
        if target.suffix.lower() not in _SERVEABLE_EXT:
            raise BookError("not serveable")
    except (BookError, OSError):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # no-store, not the 24h the MCP asset route uses: these URLs are stable
    # while their content is rebuilt, so a cached page image is a preview that
    # shows yesterday's document and looks entirely convincing.
    return _file_response(target, download=target.suffix.lower() in (".pdf", ".epub"))


@console_page
async def console_doc_file(request):
    book_id = request.path_params["book_id"]
    try:
        document = await anyio.to_thread.run_sync(
            functools.partial(books.get_document, book_id)
        )
    except BookError:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # text/plain + attachment + nosniff, never text/html: this is agent-authored
    # markup, and serving it as a document from the console's own origin would
    # be stored XSS against a page whose URL is the console credential.
    return PlainTextResponse(
        document,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="doc.html"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@console_page
async def console_library_file(request):
    try:
        path = library.image_path(request.path_params["filename"])
    except LibraryError:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return _file_response(path)


@console_page
async def console_library_thumb(request):
    try:
        source = library.image_path(request.path_params["filename"])
    except LibraryError:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return await _thumbnail(source)


async def serve_static(request):
    """Console CSS, icons and the self-hosted fonts. No secrets, no book data.

    A bounded handler rather than a StaticFiles mount, for the same reason the
    asset route is one: a mount would also serve anything that later lands in
    that directory.
    """
    parts = [p for p in request.path_params["path"].split("/") if p not in ("", ".", "..")]
    if not parts:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    target = STATIC_DIR.joinpath(*parts).resolve()
    if STATIC_DIR.resolve() not in target.parents or not target.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(target, headers={"Cache-Control": "public, max-age=604800"})


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
            "email_configured": settings.delivery_ready()[0],
            "image_generation": imagegen.available(),
            "diagram_engines": diagrams.available_engines(),
            "preview": _preview_available(),
            # Whether the console is reachable and whether it is guarded — never
            # the token itself, which is the credential.
            "console": bool(CONSOLE_ROUTE),
            "console_token_set": bool(CONSOLE_TOKEN),
            "public_base_url": PUBLIC_BASE_URL,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    books.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    # A key saved in the console before the last restart has to reach imagegen's
    # environment read before the first tool call, not after the first save.
    with contextlib.suppress(Exception):
        settings.apply_runtime()
    if BOOK_MAX_AGE_DAYS > 0:
        with contextlib.suppress(Exception):
            n = books.prune(BOOK_MAX_AGE_DAYS)
            if n:
                logger.info("pruned %d book(s) older than %d days", n, BOOK_MAX_AGE_DAYS)
    # Both are invisible to everything else in the server, so if nobody sweeps
    # them at startup nobody ever will.
    with contextlib.suppress(Exception):
        thumbs.prune()
    with contextlib.suppress(Exception):
        library.sweep_temp()
    async with mcp.session_manager.run():
        yield


routes = [
    Route("/health", health_check, methods=["GET"]),
    Route(f"{BASE_PATH}/health", health_check, methods=["GET"]),
    Route(f"{ASSETS_ROUTE}/{{book_id}}/{{filename}}", serve_asset, methods=["GET"]),
    Route(f"{OUT_ROUTE}/{{book_id}}/{{filename}}", serve_out, methods=["GET"]),
]

# Both URL shapes exist so the console works identically with and without a
# CONSOLE_TOKEN: bare paths locally, token-prefixed paths on the VPS. The
# token-prefixed routes are only registered when a token is actually set, so an
# unconfigured server cannot serve a guessable "/kindle/<anything>/console".
_console_shapes = [CONSOLE_ROUTE]
if CONSOLE_TOKEN:
    _console_shapes.append(f"{BASE_PATH}/{{token}}/console")

for _console_path in _console_shapes:
    # Starlette matches in order and the first full match wins, so the fixed
    # paths MUST precede the parameterised ones. "assets/console.css" is a
    # perfectly good (book_id, filename) pair, and "library" is a perfectly good
    # book_id — get this order wrong and the stylesheet 404s or the library page
    # renders as a missing book, with nothing in the log either way.
    routes.append(
        Route(f"{_console_path}/assets/{{path:path}}", serve_static, methods=["GET"])
    )
    routes += [
        Route(_console_path, console_index, methods=["GET"]),
        Route(f"{_console_path}/", console_slash, methods=["GET"]),
        Route(f"{_console_path}/delete-selected", console_delete_selected, methods=["POST"]),
        Route(f"{_console_path}/settings", console_settings, methods=["GET", "POST"]),
        Route(f"{_console_path}/settings/", console_slash, methods=["GET"]),
        Route(f"{_console_path}/library", console_library, methods=["GET"]),
        Route(f"{_console_path}/library/", console_slash, methods=["GET"]),
        Route(f"{_console_path}/library/upload", console_library_upload, methods=["POST"]),
        Route(f"{_console_path}/library/delete", console_library_delete, methods=["POST"]),
        Route(f"{_console_path}/library/file/{{filename}}", console_library_file, methods=["GET"]),
        Route(f"{_console_path}/library/thumb/{{filename}}", console_library_thumb, methods=["GET"]),
        Route(f"{_console_path}/{{book_id}}", console_book, methods=["GET"]),
        Route(f"{_console_path}/{{book_id}}/document", console_document, methods=["POST"]),
        Route(f"{_console_path}/{{book_id}}/preview", console_preview, methods=["POST"]),
        Route(f"{_console_path}/{{book_id}}/send", console_send, methods=["POST"]),
        Route(f"{_console_path}/{{book_id}}/upload", console_upload, methods=["POST"]),
        Route(f"{_console_path}/{{book_id}}/asset-delete", console_asset_delete, methods=["POST"]),
        Route(f"{_console_path}/{{book_id}}/delete", console_book_delete, methods=["POST"]),
        Route(f"{_console_path}/{{book_id}}/doc.html", console_doc_file, methods=["GET"]),
        Route(f"{_console_path}/{{book_id}}/file/{{filename}}", console_asset_file, methods=["GET"]),
        Route(f"{_console_path}/{{book_id}}/thumb/{{filename}}", console_asset_thumb, methods=["GET"]),
        Route(f"{_console_path}/{{book_id}}/out/{{filename}}", console_out_file, methods=["GET"]),
    ]

# LAST, always: Mount("/") matches every path, so anything registered after it
# is unreachable — with no warning and no log line, just the MCP transport's own
# 404 or "Invalid Content-Type header", which reads exactly like a broken handler.
routes.append(Mount("/", app=mcp_asgi))

app = Starlette(routes=routes, lifespan=lifespan)


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

    def _is_public(self, path: str) -> bool:
        """Routes the MCP bearer gate does not apply to.

        "Public" means "not guarded by MCP_TOKENS" — not unguarded. The console
        carries its own CONSOLE_TOKEN, checked in its handlers, because a
        browser following a link arrives without an Authorization header; book
        assets sit behind an unguessable book id.

        Segment-aware rather than a bare prefix test: ``/kindle/assetsXYZ`` is
        not the assets route, and must not be treated as one.
        """
        if path in ("/health", f"{BASE_PATH}/health"):
            return True
        for route in (ASSETS_ROUTE, OUT_ROUTE, CONSOLE_ROUTE):
            if path == route or path.startswith(route + "/"):
                return True
        if CONSOLE_TOKEN:
            segments = [s for s in path.split("/") if s]
            if (
                len(segments) >= 3
                and segments[0] == _safe_name
                and segments[1] == CONSOLE_TOKEN
                and segments[2] == "console"
            ):
                return True
        return False

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
        # Before every token branch below, including the "no tokens configured"
        # one: that branch calls _strip_token_segment unconditionally, which
        # would rewrite /kindle/console into /kindle/ and hand the console's own
        # URL to the MCP transport — which answers 406 and looks like a routing
        # bug rather than a middleware one.
        if not path.startswith(BASE_PATH) or self._is_public(path):
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
