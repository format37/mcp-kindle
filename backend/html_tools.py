"""Core logic: HTML -> EPUB -> email to Kindle.

HTML is a richer authoring format than markdown (tables with rowspan/colspan,
definition lists, inline SVG, figure/figcaption, semantic attributes…). This
module mirrors :mod:`kindle_tools` but takes HTML as input.

Image sources allowed:
    - ``data:`` URIs (decoded inline)
    - filenames that resolve under ``/app/data`` (same convention as the
      markdown tool — see ``kindle_tools.DATA_DIR``)

External URLs (``http://``, ``https://``) and other schemes are intentionally
*not* fetched, to keep the server free of SSRF and surprise outbound traffic.
The chapter splits at every ``<h1>``; content before the first ``<h1>`` (if
any) becomes a "Preface" chapter.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import re
import tempfile
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import epub

from kindle_tools import (
    CSS as MARKDOWN_CSS,
    DATA_DIR,
    IMAGE_MEDIA_TYPES,
    _resolve_image,
    send_epub_to_kindle,
)

logger = logging.getLogger(__name__)

# Tags removed wholesale before conversion (XSS / non-EPUB safe / privacy).
STRIP_TAGS = {
    "script", "iframe", "embed", "object", "noscript", "form",
    "input", "button", "meta", "link", "style", "base",
}
EVENT_ATTR_RE = re.compile(r"^on[a-z]+$", re.IGNORECASE)

# Additional CSS for HTML features not present in the markdown pipeline.
HTML_EXTRA_CSS = """
blockquote { border-left: 3px solid #888; margin: 0.6em 0 0.6em 0.5em;
             padding: 0.2em 0.8em; color: #333; }
caption { caption-side: top; font-style: italic; padding-bottom: 0.3em; }
figure { margin: 0.8em 0; text-align: center; }
figcaption { font-style: italic; font-size: 0.9em; color: #444; }
img { max-width: 100%; height: auto; }
hr { border: 0; border-top: 1px solid #888; margin: 1em 0; }
dl { margin: 0.5em 0; }
dt { font-weight: bold; margin-top: 0.4em; }
dd { margin: 0 0 0.3em 1.2em; }
sup { font-size: 0.75em; vertical-align: super; }
sub { font-size: 0.75em; vertical-align: sub; }
"""

KINDLE_CSS = MARKDOWN_CSS + HTML_EXTRA_CSS

CHAPTER_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    '<head><title>{title}</title>'
    '<link rel="stylesheet" type="text/css" href="style/default.css"/></head>\n'
    '<body>{body}</body>\n</html>'
)


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _sanitize(soup: BeautifulSoup) -> None:
    """Drop dangerous tags and event-handler attributes (in-place)."""
    for tag in soup.find_all(list(STRIP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if EVENT_ATTR_RE.match(attr):
                del tag.attrs[attr]
        href = tag.attrs.get("href")
        if isinstance(href, str) and href.lower().lstrip().startswith("javascript:"):
            del tag.attrs["href"]


def _ensure_body(soup: BeautifulSoup) -> Tag:
    body = soup.body
    if body is None:
        body = soup.new_tag("body")
        # Wrap whatever the parser gave us into a body.
        for child in list(soup.contents):
            body.append(child.extract())
        soup.append(body)
    return body


def _extract_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        t = soup.title.string.strip()
        if t:
            return t
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
    return None


def _decode_data_uri(src: str) -> tuple[bytes, str] | None:
    """Decode a ``data:`` URI to (bytes, mime). Returns None on failure.

    Per RFC 2397 the syntax is ``data:[<mediatype>][;base64],<data>`` where
    ``<mediatype>`` may include parameters (``image/png;charset=utf-8``) and
    may be empty.
    """
    m = re.match(r"data:([^,]*),(.*)", src, re.DOTALL)
    if not m:
        return None
    meta = m.group(1)
    payload = m.group(2)
    tokens = [t.strip() for t in meta.split(";") if t.strip()]
    is_b64 = "base64" in (t.lower() for t in tokens)
    mime_token = tokens[0] if tokens and tokens[0].lower() != "base64" else ""
    mime = (mime_token or "text/plain").lower()
    try:
        if is_b64:
            content = base64.b64decode(payload)
        else:
            content = urllib.parse.unquote_to_bytes(payload)
    except Exception as e:
        logger.warning("Failed to decode data URI: %s", e)
        return None
    return content, mime


def _embed_images(soup: BeautifulSoup, book: epub.EpubBook, data_dir: Path) -> None:
    """Rewrite <img> tags so they point at embedded EPUB resources.

    Local filenames are resolved under ``data_dir`` (path-traversal-safe).
    ``data:`` URIs are decoded and embedded as standalone image items.
    External URLs are left untouched (so they remain broken in the EPUB —
    intentional: no outbound network calls).
    """
    embedded_files: dict[str, str] = {}  # resolved-path -> epub_path
    embedded_data: dict[str, str] = {}   # sha1(content) -> epub_path
    used_names: set[str] = set()

    def _unique(name: str) -> str:
        if name not in used_names:
            return name
        stem, dot, ext = name.rpartition(".")
        n = 1
        while True:
            candidate = f"{stem}_{n}.{ext}" if dot else f"{name}_{n}"
            if candidate not in used_names:
                return candidate
            n += 1

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            continue

        if src.startswith("data:"):
            got = _decode_data_uri(src)
            if got is None:
                continue
            content, mime = got
            if mime not in IMAGE_MEDIA_TYPES.values():
                # SVG-in-data-URI in particular can carry <script>; we don't
                # parse + sanitize its bytes, so reject it. Same for any
                # other non-raster type.
                logger.warning("Rejecting data: URI with unsupported mime %s", mime)
                continue
            ext = mimetypes.guess_extension(mime) or ".bin"
            digest = hashlib.sha1(content).hexdigest()[:10]
            epub_path = embedded_data.get(digest)
            if epub_path is None:
                name = _unique(f"data_{digest}{ext}")
                used_names.add(name)
                epub_path = f"images/{name}"
                book.add_item(epub.EpubImage(
                    uid=f"img_data_{digest}",
                    file_name=epub_path,
                    media_type=mime,
                    content=content,
                ))
                embedded_data[digest] = epub_path
            img["src"] = epub_path
            continue

        if src.startswith(("http://", "https://", "//")):
            # Some Kindle renderers fetch <img src=...> at render time,
            # which would leak the reader's IP to whichever URL the agent
            # embedded. Drop the src so the renderer can't fetch; the
            # alt text still shows.
            logger.info("Stripping external image src: %s", src[:80])
            del img["src"]
            continue

        resolved = _resolve_image(src, data_dir)
        if resolved is None:
            continue
        key = str(resolved)
        epub_path = embedded_files.get(key)
        if epub_path is None:
            ext = resolved.suffix.lower()
            if ext not in IMAGE_MEDIA_TYPES:
                logger.warning("Unsupported image extension %s for %s", ext, resolved)
                continue
            name = _unique(resolved.name)
            used_names.add(name)
            epub_path = f"images/{name}"
            book.add_item(epub.EpubImage(
                uid=f"img_{len(embedded_files)}_" + re.sub(r"\W+", "_", resolved.stem).strip("_"),
                file_name=epub_path,
                media_type=IMAGE_MEDIA_TYPES[ext],
                content=resolved.read_bytes(),
            ))
            embedded_files[key] = epub_path
        img["src"] = epub_path


SECTION_WRAPPERS = {"section", "article", "main"}


def _flatten_section_wrappers(body: Tag) -> None:
    """Unwrap <section>/<article>/<main> elements directly under body so that
    <h1>s buried inside them become top-level body children for splitting.

    Runs to fixed point so nested wrappers all dissolve.
    """
    while True:
        changed = False
        for child in list(body.children):
            if isinstance(child, Tag) and child.name in SECTION_WRAPPERS:
                child.unwrap()
                changed = True
        if not changed:
            break


def _split_chapters(body: Tag) -> list[tuple[str, list]]:
    """Split body children into ``(title, [nodes])`` groups at every <h1>.

    Content before the first <h1> (if any) becomes a "Preface" chapter.
    Common semantic wrappers (``<section>``, ``<article>``, ``<main>``) are
    flattened first so nested ``<h1>``s still produce chapter boundaries.
    """
    _flatten_section_wrappers(body)

    chapters: list[tuple[str, list]] = []
    current_title: str | None = None
    current: list = []

    def flush():
        nonlocal current, current_title
        if not current:
            return
        chapters.append((current_title or "Preface", current))
        current = []
        current_title = None

    for child in list(body.children):
        if isinstance(child, NavigableString):
            if child.strip():
                current.append(child)
            continue
        if not isinstance(child, Tag):
            continue
        if child.name == "h1":
            flush()
            current_title = child.get_text(" ", strip=True) or "Chapter"
            current.append(child)
        else:
            current.append(child)
    flush()

    if not chapters:
        chapters.append(("Document", list(body.children)))
    return chapters


def _detect_lang(text: str) -> str:
    if re.search(r"[぀-ヿ一-鿿]", text):
        return "ja"
    if re.search(r"[Ѐ-ӿ]", text):
        return "ru"
    return "en"


def build_epub_from_html(
    html_text: str,
    title: str,
    output_path: str,
    data_dir: Path | None = None,
) -> str:
    """Parse HTML, sanitise it, embed images, and write an EPUB.

    Returns the resolved book title (useful when it's auto-extracted).
    """
    if data_dir is None:
        data_dir = DATA_DIR

    # `lxml` parser tolerates fragments and broken HTML.
    soup = BeautifulSoup(html_text, "lxml")
    _sanitize(soup)

    resolved_title = title.strip() if title else (_extract_title(soup) or "Untitled")

    body = _ensure_body(soup)
    body_text = body.get_text(" ", strip=True)

    book = epub.EpubBook()
    book.set_identifier("html2epub-" + hashlib.sha1(html_text.encode("utf-8")).hexdigest()[:12])
    book.set_title(resolved_title)
    book.set_language(_detect_lang(body_text))

    css = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=KINDLE_CSS.encode("utf-8"),
    )
    book.add_item(css)

    _embed_images(soup, book, data_dir)

    chapters = []
    for i, (chap_title, nodes) in enumerate(_split_chapters(body)):
        ch = epub.EpubHtml(
            title=chap_title,
            file_name=f"chapter_{i:03d}.xhtml",
            lang=book.language,
        )
        body_html = "".join(str(n) for n in nodes)
        ch.content = CHAPTER_TEMPLATE.format(
            title=_escape(chap_title), body=body_html
        ).encode("utf-8")
        ch.add_item(css)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = chapters
    book.spine = ["nav"] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(output_path, book, {})
    return resolved_title


def convert_html_and_send(
    html_text: str,
    title: str,
    sender_email: str,
    sender_password: str,
    recipient_email: str,
) -> str:
    """Full pipeline: HTML -> EPUB -> email to Kindle."""
    if not html_text or not html_text.strip():
        return "Error: empty html_text"

    safe_title = re.sub(r"\W+", "_", title or "book")[:50]
    tmp = tempfile.NamedTemporaryFile(suffix=".epub", prefix=f"{safe_title}_", delete=False)
    epub_path = tmp.name
    tmp.close()

    try:
        resolved_title = build_epub_from_html(html_text, title, epub_path)
        send_epub_to_kindle(
            epub_path,
            sender_email,
            sender_password,
            recipient_email,
            resolved_title,
        )
        return f"Sent '{resolved_title}' (HTML) to {recipient_email}"
    finally:
        if os.path.exists(epub_path):
            os.unlink(epub_path)
