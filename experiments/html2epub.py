#!/usr/bin/env python3
"""Convert a rich HTML document to EPUB.

HTML is a richer format than markdown — tables with rowspan/colspan, mixed
nested lists, definition lists, footnote anchors, figures, mathy sup/sub,
inline styles, etc. This experiment converts one HTML file into a Kindle-
friendly EPUB while embedding its images.

Usage:
    python experiments/html2epub.py [INPUT_HTML] [OUTPUT_EPUB]

Defaults to experiments/sample.html -> experiments/output/rich_sample.epub.

Image handling:
    - data: URIs are decoded and embedded
    - http(s) URLs are fetched (timeout 10s); failures are logged and skipped
    - relative paths are resolved against the HTML file's directory

Chapter splitting:
    The body is split at each <h1>. If no <h1> is found, the entire body
    becomes one chapter.

Stripped for safety / non-EPUB compatibility:
    <script>, <iframe>, <embed>, <object>, <noscript>, <form>, <input>,
    <button>, <link rel=...>, on* event attributes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import mimetypes
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import epub

try:
    import urllib.request
except ImportError:  # pragma: no cover
    urllib = None  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("html2epub")

EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
}

KINDLE_CSS = """
body { font-family: Georgia, serif; line-height: 1.55; color: #111; margin: 1em; }
h1 { font-size: 1.9em; margin: 0.3em 0 0.5em; border-bottom: 1px solid #888; padding-bottom: 0.2em; }
h2 { font-size: 1.45em; margin-top: 1.1em; }
h3 { font-size: 1.2em; margin-top: 1em; }
p  { margin: 0.5em 0; text-align: justify; }
blockquote { border-left: 3px solid #888; margin: 0.6em 0 0.6em 0.5em; padding: 0.2em 0.8em; color: #333; }
code { font-family: "DejaVu Sans Mono", monospace; background: #f0f0f0; padding: 0.05em 0.25em; }
pre  { font-family: "DejaVu Sans Mono", monospace; background: #f4f4f4; padding: 0.6em; overflow-x: auto;
       white-space: pre-wrap; word-wrap: break-word; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; }
caption { caption-side: top; font-style: italic; padding-bottom: 0.3em; }
th, td { border: 1px solid #888; padding: 0.35em 0.55em; vertical-align: top; }
th { background: #eee; }
ul, ol { margin: 0.4em 0 0.6em 1.3em; }
dl { margin: 0.5em 0; }
dt { font-weight: bold; margin-top: 0.4em; }
dd { margin: 0 0 0.3em 1.2em; }
figure { margin: 0.8em 0; text-align: center; }
figcaption { font-style: italic; font-size: 0.9em; color: #444; }
img { max-width: 100%; height: auto; }
hr { border: 0; border-top: 1px solid #888; margin: 1em 0; }
sup { font-size: 0.75em; vertical-align: super; }
sub { font-size: 0.75em; vertical-align: sub; }
.footnote { font-size: 0.85em; color: #333; }
"""

STRIP_TAGS = {"script", "iframe", "embed", "object", "noscript", "form",
              "input", "button", "meta", "link"}
EVENT_ATTRS = re.compile(r"^on[a-z]+$", re.IGNORECASE)


def _ext_from_data_uri(mime: str) -> str:
    return EXT_BY_MIME.get(mime.lower()) or mimetypes.guess_extension(mime) or ".bin"


def _hash_name(prefix: str, content: bytes, ext: str) -> str:
    h = hashlib.sha1(content).hexdigest()[:10]
    return f"{prefix}_{h}{ext}"


def _fetch_url(url: str, timeout: float = 10.0) -> tuple[bytes, str] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec - test fixture
            data = r.read()
            mime = r.headers.get_content_type() or "application/octet-stream"
            return data, mime
    except Exception as e:
        logger.warning("Could not fetch %s: %s", url, e)
        return None


def _resolve_image(src: str, base_dir: Path) -> tuple[bytes, str, str] | None:
    """Return (bytes, mime, suggested_filename) for an <img src=...>, or None."""
    src = src.strip()
    if not src:
        return None

    # data: URI
    if src.startswith("data:"):
        m = re.match(r"data:([^;,]+)(;base64)?,(.*)", src, re.DOTALL)
        if not m:
            logger.warning("Malformed data URI (%d chars)", len(src))
            return None
        mime = m.group(1) or "application/octet-stream"
        is_b64 = m.group(2) is not None
        payload = m.group(3)
        try:
            if is_b64:
                content = base64.b64decode(payload)
            else:
                content = urllib.parse.unquote_to_bytes(payload)
        except Exception as e:
            logger.warning("Failed to decode data URI: %s", e)
            return None
        return content, mime, _hash_name("data", content, _ext_from_data_uri(mime))

    # http(s)
    if src.startswith(("http://", "https://")):
        got = _fetch_url(src)
        if got is None:
            return None
        content, mime = got
        return content, mime, _hash_name("web", content, _ext_from_data_uri(mime))

    # local file
    p = (base_dir / src).resolve()
    if not p.is_file():
        logger.warning("Image file not found: %s", p)
        return None
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "application/octet-stream"
    return p.read_bytes(), mime, p.name


def _sanitize(soup: BeautifulSoup) -> None:
    """Drop scripts, iframes, event handlers, etc. In-place."""
    for tag in soup.find_all(list(STRIP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(True):
        # Strip on* event attributes and javascript: URLs.
        for attr in list(tag.attrs):
            if EVENT_ATTRS.match(attr):
                del tag.attrs[attr]
        href = tag.attrs.get("href")
        if isinstance(href, str) and href.lower().lstrip().startswith("javascript:"):
            del tag.attrs["href"]


def _ensure_body(soup: BeautifulSoup) -> Tag:
    body = soup.body
    if body is None:
        # Wrap top-level into a synthetic body
        body = soup.new_tag("body")
        for child in list(soup.contents):
            body.append(child.extract())
        soup.append(body)
    return body


def _split_chapters(body: Tag) -> list[tuple[str, list]]:
    """Split body children into (title, [tags]) groups at each <h1>.

    Content before the first h1 (if any) becomes a "Preface" chapter.
    """
    chapters: list[tuple[str, list]] = []
    current_title = None
    current: list = []

    def flush():
        nonlocal current, current_title
        if not current:
            return
        title = current_title or "Preface"
        chapters.append((title, current))
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


def _embed_images(soup: BeautifulSoup, book: epub.EpubBook, base_dir: Path) -> None:
    embedded: dict[str, str] = {}
    used_names: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        if src in embedded:
            img["src"] = embedded[src]
            continue
        got = _resolve_image(src, base_dir)
        if got is None:
            continue
        content, mime, suggested = got
        # Unique within EPUB
        name = suggested
        n = 1
        while name in used_names:
            stem, _, ext = suggested.rpartition(".")
            name = f"{stem}_{n}.{ext}" if stem else f"{suggested}_{n}"
            n += 1
        used_names.add(name)
        epub_path = f"images/{name}"
        uid = "img_" + re.sub(r"\W+", "_", name).strip("_")
        book.add_item(epub.EpubImage(uid=uid, file_name=epub_path,
                                     media_type=mime, content=content))
        embedded[src] = epub_path
        img["src"] = epub_path


def _children_to_html(children: Iterable) -> str:
    return "".join(str(c) for c in children)


def html_to_epub(input_html: Path, output_epub: Path, title: str | None = None,
                 author: str = "html2epub") -> Path:
    raw = input_html.read_text(encoding="utf-8")
    soup = BeautifulSoup(raw, "lxml")

    _sanitize(soup)

    head_title = None
    if soup.title and soup.title.string:
        head_title = soup.title.string.strip()
    title = title or head_title or input_html.stem

    body = _ensure_body(soup)

    book = epub.EpubBook()
    book.set_identifier(f"html2epub-{hashlib.sha1(raw.encode()).hexdigest()[:12]}")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)

    css = epub.EpubItem(uid="style", file_name="style/default.css",
                        media_type="text/css", content=KINDLE_CSS.encode("utf-8"))
    book.add_item(css)

    _embed_images(soup, book, base_dir=input_html.parent)

    chapter_template = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><title>{title}</title>'
        '<link rel="stylesheet" type="text/css" href="style/default.css"/></head>\n'
        '<body>{body}</body>\n</html>'
    )

    chapters = []
    for i, (chap_title, nodes) in enumerate(_split_chapters(body)):
        ch = epub.EpubHtml(title=chap_title, file_name=f"chapter_{i:02d}.xhtml", lang="en")
        body_html = _children_to_html(nodes)
        ch.content = chapter_template.format(
            title=_escape(chap_title), body=body_html
        ).encode("utf-8")
        ch.add_item(css)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = chapters
    book.spine = ["nav"] + chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    output_epub.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output_epub), book, {})
    return output_epub


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Convert HTML to EPUB.")
    parser.add_argument("input", nargs="?", default=str(repo_root / "sample.html"),
                        help="Input HTML file (default: experiments/sample.html)")
    parser.add_argument("output", nargs="?",
                        default=str(repo_root / "output" / "rich_sample.epub"),
                        help="Output EPUB path")
    parser.add_argument("--title", default=None)
    parser.add_argument("--author", default="html2epub")
    args = parser.parse_args(argv)

    out = html_to_epub(Path(args.input), Path(args.output),
                       title=args.title, author=args.author)
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
