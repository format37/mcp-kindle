"""Core logic: markdown -> EPUB -> email to Kindle."""

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

import markdown
from ebooklib import epub

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


def build_epub(pages: list[str], title: str, output_path: str) -> None:
    """Assemble pages into an EPUB file."""
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

    chapters = []
    for i, page_md in enumerate(pages):
        html_body = md_to_html(page_md)
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
    """Build a unique attachment filename with datetime prefix."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    safe = re.sub(r"\s+", "_", safe)[:80]
    if not safe:
        safe = "book"
    return f"{ts}_{safe}.epub"


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

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
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
