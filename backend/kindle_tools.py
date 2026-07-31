"""Core logic: markdown -> EPUB -> email to Kindle."""

import logging
import os
import re
import smtplib
import tempfile
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import unquote

import markdown
from ebooklib import epub

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))

#: Per-operation SMTP timeout. Gmail's handshake is fast; anything near this
#: means the port is filtered rather than slow.
SMTP_TIMEOUT_S = int(os.getenv("SMTP_TIMEOUT_S", "30"))

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

CSS = """
body {
    font-family: Georgia, serif;
    font-size: 1em;
    line-height: 1.6;
    color: #1a1a1a;
    margin: 1em;
}
h1 { font-size: 1.8em; margin-bottom: 0.5em; }
h2 { font-size: 1.4em; margin-top: 1.2em; margin-bottom: 0.4em; }
h3 { font-size: 1.2em; margin-top: 1em; margin-bottom: 0.3em; }
ul, ol { margin-left: 1.5em; margin-bottom: 0.8em; }
li { margin-bottom: 0.3em; }
p { margin-bottom: 0.8em; }
strong { font-weight: bold; }
em { font-style: italic; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1em; }
th, td { border: 1px solid #ccc; padding: 0.4em 0.6em; text-align: left; }
th { background: #f0f0f0; font-weight: bold; }
code { font-family: monospace; background: #f5f5f5; padding: 0.1em 0.3em; }
pre { background: #f5f5f5; padding: 0.8em; overflow-x: auto; margin-bottom: 1em; }
"""

CHAPTER_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>{title}</title></head>
<body>
{body}
</body>
</html>"""


def extract_title(md_text: str) -> str:
    """Extract the first # heading as the book title."""
    m = re.search(r"^#\s+(.+)$", md_text, re.MULTILINE)
    return m.group(1).strip() if m else "Untitled"


def split_pages(md_text: str) -> list[str]:
    """Split markdown on {next page} markers."""
    pages = re.split(r"\{next page\}", md_text)
    return [p.strip() for p in pages if p.strip()]


def md_to_html(md_text: str) -> str:
    """Convert markdown text to HTML."""
    return markdown.markdown(md_text, extensions=["tables", "fenced_code"])


def _resolve_image(src: str, data_dir: Path) -> Path | None:
    """Resolve a markdown image src to a file inside data_dir.

    Returns the resolved Path if the file exists and stays within data_dir,
    otherwise None. Skips external URLs and data: URIs.
    """
    if not src or src.startswith(("http://", "https://", "data:", "//")):
        return None
    fname = unquote(src).strip()
    # Strip leading "./" and optional "data/" prefix so agents can reference
    # the image either as "name.png" or "data/name.png".
    while fname.startswith("./"):
        fname = fname[2:]
    if fname.startswith("data/"):
        fname = fname[len("data/"):]
    if fname.startswith("/"):
        # Absolute path inside the container; only allow under data_dir.
        candidate = Path(fname)
    else:
        candidate = data_dir / fname
    try:
        resolved = candidate.resolve()
        data_resolved = data_dir.resolve()
    except (OSError, ValueError):
        # ValueError covers e.g. embedded NUL bytes in the path.
        return None
    if data_resolved not in resolved.parents and resolved != data_resolved:
        logger.warning("Image %r resolves outside data dir, skipping", src)
        return None
    if not resolved.is_file():
        logger.warning("Image %r not found at %s", src, resolved)
        return None
    return resolved


def _embed_images(html: str, book: epub.EpubBook, embedded: dict[str, str], data_dir: Path) -> str:
    """Rewrite <img src=...> tags so they reference files embedded in the EPUB.

    For each unique image referenced, the file is read from data_dir and added
    to the book as an EpubImage under images/. Returns the rewritten HTML.
    """
    img_tag = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])([^"\']+)\2([^>]*?>)', re.IGNORECASE)

    def replace(match: re.Match) -> str:
        prefix, quote, src, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
        resolved = _resolve_image(src, data_dir)
        if resolved is None:
            return match.group(0)
        ext = resolved.suffix.lower()
        if ext not in IMAGE_MEDIA_TYPES:
            logger.warning("Unsupported image extension %s for %s", ext, resolved)
            return match.group(0)

        epub_path = embedded.get(str(resolved))
        if epub_path is None:
            epub_path = f"images/{resolved.name}"
            # Ensure unique file_name if two source files share a basename.
            n = 1
            while epub_path in embedded.values():
                epub_path = f"images/{resolved.stem}_{n}{resolved.suffix}"
                n += 1
            with open(resolved, "rb") as f:
                content = f.read()
            uid = f"img_{len(embedded)}_" + (re.sub(r"\W+", "_", resolved.stem).strip("_") or "x")
            book.add_item(
                epub.EpubImage(
                    uid=uid,
                    file_name=epub_path,
                    media_type=IMAGE_MEDIA_TYPES[ext],
                    content=content,
                )
            )
            embedded[str(resolved)] = epub_path
        return f'{prefix}{quote}{epub_path}{quote}{suffix}'

    return img_tag.sub(replace, html)


def build_epub(
    pages: list[str],
    title: str,
    output_path: str,
    data_dir: Path | None = None,
) -> None:
    """Assemble pages into an EPUB file.

    Image references in the markdown (``![alt](filename.png)``) are resolved
    against ``data_dir`` and embedded into the EPUB. Defaults to ``DATA_DIR``.
    """
    if data_dir is None:
        data_dir = DATA_DIR

    book = epub.EpubBook()
    book.set_identifier("md2epub-" + re.sub(r"\W+", "-", title.lower()))
    book.set_title(title)
    text = " ".join(pages)
    if re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", text):
        lang = "ja"
    elif re.search(r"[\u0400-\u04FF]", text):
        lang = "ru"
    else:
        lang = "en"
    book.set_language(lang)

    style = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=CSS.encode("utf-8"),
    )
    book.add_item(style)

    embedded_images: dict[str, str] = {}
    chapters = []
    for i, page_md in enumerate(pages):
        html_body = md_to_html(page_md)
        html_body = _embed_images(html_body, book, embedded_images, data_dir)
        chapter_title = f"Page {i + 1}"
        heading = re.search(r"^#{1,3}\s+(.+)$", page_md, re.MULTILINE)
        if heading:
            chapter_title = heading.group(1).strip()

        ch = epub.EpubHtml(
            title=chapter_title,
            file_name=f"page_{i:03d}.xhtml",
            lang="en",
        )
        ch.content = CHAPTER_TEMPLATE.format(title=chapter_title, body=html_body).encode("utf-8")
        ch.add_item(style)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = chapters
    book.spine = ["nav"] + chapters

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    epub.write_epub(output_path, book, {})


def _make_attachment_filename(title: str) -> str:
    """Build the EPUB attachment filename: YYMMDD-HHMMSS-<name>.epub.

    The YYMMDD-HHMMSS datetime prefix is generated server-side (UTC), so callers
    must NOT add their own — just pass a short title as the <name>. Seconds are
    included so two books sent in the same minute don't collide.
    """
    ts = datetime.now(timezone.utc).strftime("%y%m%d-%H%M%S")
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    safe = re.sub(r"[\s_]+", "-", safe)[:80].strip("-")
    if not safe:
        safe = "book"
    return f"{ts}-{safe}.epub"


def send_epub_to_kindle(
    epub_path: str,
    sender_email: str,
    sender_password: str,
    recipient_email: str,
    title: str,
) -> None:
    """Send an EPUB file to a Kindle email address via Gmail SMTP."""
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = title

    msg.attach(MIMEText("", "plain"))

    filename = _make_attachment_filename(title)
    with open(epub_path, "rb") as f:
        part = MIMEBase("application", "epub+zip")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)

    # A timeout is not optional here: this runs on one of the four job worker
    # threads, and a black-holed port 587 (common on a locked-down host) would
    # otherwise pin that thread for the life of the process and hang shutdown.
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=SMTP_TIMEOUT_S) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())


def convert_and_send(
    markdown_text: str,
    title: str,
    sender_email: str,
    sender_password: str,
    recipient_email: str,
) -> str:
    """Full pipeline: markdown -> EPUB -> email to Kindle.

    Returns a success message or raises on failure.
    """
    if not title:
        title = extract_title(markdown_text)

    pages = split_pages(markdown_text)
    if not pages:
        return "Error: no content to convert (empty markdown or only whitespace)"

    safe_title = re.sub(r"\W+", "_", title)[:50]
    tmp = tempfile.NamedTemporaryFile(suffix=".epub", prefix=f"{safe_title}_", delete=False)
    epub_path = tmp.name
    tmp.close()

    try:
        build_epub(pages, title, epub_path)
        send_epub_to_kindle(epub_path, sender_email, sender_password, recipient_email, title)
        return f"Sent '{title}' ({len(pages)} page(s)) to {recipient_email}"
    finally:
        if os.path.exists(epub_path):
            os.unlink(epub_path)
