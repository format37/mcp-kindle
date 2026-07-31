"""Console tests — the parts that are cheap to get wrong and expensive to notice.

Run them inside the image, where every dependency already exists::

    docker compose run --rm --entrypoint sh mcp-kindle \\
        -c "pip install -q pytest && python -m pytest tests -q"

They cover four things that all fail *silently* in production: a credential
overwritten by its own mask, a page proof left behind from a previous build, a
path that escapes the data folder, and an upload that clobbers the asset the
document points at. Rendering is checked only for the invariants that matter
(escaping, and that a token-prefixed console keeps its prefix on every link) —
not for markup, which should be free to change.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """A DATA_DIR the modules pick up at import, reimported per test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("books", "library", "thumbs", "settings"):
        sys.modules.pop(name, None)
    return tmp_path


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def test_stored_value_wins_over_env_and_clearing_reverts(data_dir, monkeypatch):
    monkeypatch.setenv("RECIPIENT_EMAIL", "from-env@kindle.com")
    import settings

    assert settings.load()["recipient_email"] == "from-env@kindle.com"
    assert settings.effective()["recipient_email"]["source"] == "env"

    settings.save(recipient_email="from-console@kindle.com")
    assert settings.load()["recipient_email"] == "from-console@kindle.com"
    info = settings.effective()["recipient_email"]
    assert info["source"] == "console" and info["overridden"] is True

    settings.save(recipient_email="")  # cleared in the console
    assert settings.load()["recipient_email"] == "from-env@kindle.com"


def test_a_field_not_passed_is_left_alone(data_dir):
    """How the console says "the operator did not retype this secret"."""
    import settings

    settings.save(gmail_app_pass="abcdefghijklmnop", sender_email="me@gmail.com")
    settings.save(sender_email="me2@gmail.com")  # app pass absent entirely
    assert settings.load()["gmail_app_pass"] == "abcdefghijklmnop"


def test_a_mask_is_never_written_as_a_credential(data_dir):
    import settings

    settings.save(gmail_app_pass="abcdefghijklmnop")
    settings.save(gmail_app_pass=settings.MASK_SENTINEL)
    settings.save(gmail_app_pass="•" * 24)
    assert settings.load()["gmail_app_pass"] == "abcdefghijklmnop"


def test_secrets_are_never_rendered_in_the_clear(data_dir):
    import settings

    settings.save(gmail_app_pass="abcdefghijklmnop", gemini_api_key="AIzaSyABCDEFGH12345678")
    view = settings.public_view()
    assert "abcdefghijklmnop" not in json.dumps(view["gmail_app_pass"]["display"])
    # An API key keeps four either side — enough to tell two keys apart, not to use one.
    assert view["gemini_api_key"]["display"] == "AIza••••••5678"


def test_settings_file_is_not_world_readable(data_dir):
    import settings

    settings.save(gmail_app_pass="abcdefghijklmnop")
    assert oct(settings.PATH.stat().st_mode)[-3:] == "600"


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------

def _png(width=40, height=30, mode="RGB"):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_library_never_serves_outside_the_data_folder(data_dir):
    import library

    (data_dir / "settings.json").write_text("{}")
    (data_dir / "books").mkdir()
    (data_dir / ".hidden.png").write_bytes(_png())
    outside = data_dir.parent / "secret.png"
    outside.write_bytes(_png())
    (data_dir / "link.png").symlink_to(outside)

    listed = {row["filename"] for row in library.list_images()}
    assert listed == set(), listed  # symlink excluded, dotfile excluded, no images yet

    for bad in ("../secret.png", "settings.json", "books", ".hidden.png", "link.png", "a/b.png"):
        with pytest.raises(library.LibraryError):
            library.image_path(bad)


def test_upload_takes_its_extension_from_the_bytes_not_the_name(data_dir):
    import library

    stored = library.store_upload("photo.jpg", _png())  # says jpg, is a PNG
    assert stored["filename"] == "photo.png"


def test_upload_never_overwrites(data_dir):
    import library

    first = library.store_upload("cover.png", _png())
    second = library.store_upload("cover.png", _png(50, 50))
    assert (first["filename"], second["filename"]) == ("cover.png", "cover-2.png")


def test_upload_rejects_non_images_and_leaves_nothing_behind(data_dir):
    import library

    with pytest.raises(library.LibraryError):
        library.store_upload("evil.png", b"<html>not an image</html>")
    assert list(data_dir.glob("*.png")) == []
    assert list(data_dir.glob(".*.part")) == []


# --------------------------------------------------------------------------
# books: the upload path the console uses
# --------------------------------------------------------------------------

def test_reserving_a_name_first_is_what_stops_an_upload_clobbering_the_cover(data_dir):
    """store_asset overwrites by *stem*, across extensions — hence the reserve."""
    import books

    book_id = books.create_book("Test")["book_id"]
    books.store_asset(book_id, "cover.jpg", _png())
    fresh = books.unique_asset_name(book_id, "cover.png", ".png")
    books.store_asset(book_id, fresh, _png(60, 60))
    assert {a["filename"] for a in books.list_assets(book_id)} == {"cover.png", "cover-2.png"}


def test_a_reserved_name_is_given_back_when_the_store_fails(data_dir):
    import books

    book_id = books.create_book("Test")["book_id"]
    fresh = books.unique_asset_name(book_id, "fig", ".png")
    with pytest.raises(books.BookError):
        books.store_asset(book_id, fresh, b"not an image")
    books.release_stem(book_id, fresh)
    assert books.unique_asset_name(book_id, "fig", ".png") == fresh


# --------------------------------------------------------------------------
# thumbs
# --------------------------------------------------------------------------

def test_a_replaced_image_never_serves_its_old_thumbnail(data_dir):
    import os as _os

    import thumbs

    source = data_dir / "pic.png"
    source.write_bytes(_png(40, 30))
    first = thumbs.thumb_for(source)
    assert first.is_file()

    source.write_bytes(_png(80, 60))
    _os.utime(source, (0, 0))  # a different mtime is the whole cache key
    second = thumbs.thumb_for(source)
    assert second != first
    assert not first.exists(), "the superseded thumbnail must be dropped"


# --------------------------------------------------------------------------
# console rendering
# --------------------------------------------------------------------------

def test_everything_user_supplied_is_escaped():
    import console

    html = console.render_index(
        [{
            "book_id": "x-0123456789ab",
            "title": '<script>alert(1)</script>',
            "notes": '"><img onerror=alert(2) src=x>',
            "updated": "2026-07-31T10:00:00Z",
            "doc_chars": 10,
        }],
        base="/kindle/console",
        totals={"assets": 0, "bytes": 0, "library_count": 0, "recipient": ""},
        delivery=(False, "not configured"),
    )
    # What matters is that no tag from the input survives as markup — the text
    # of the payload is allowed through, escaped, because that is the title.
    # (The page has its own <script> for the theme pre-paint, so the assertion
    # has to name the payload rather than the tag.)
    assert "<script>alert(1)" not in html
    assert "<img onerror" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img onerror=alert(2) src=x&gt;" in html
    assert "&quot;&gt;&lt;img" in html  # the quote that would break out of an attribute


def test_a_token_prefixed_console_keeps_the_prefix_on_every_link():
    """One un-prefixed link is a dead end that looks like a broken console."""
    import re

    import console

    base = "/kindle/SECRET/console"
    pages = [
        console.render_index(
            [], base=base,
            totals={"assets": 0, "bytes": 0, "library_count": 0, "recipient": ""},
            delivery=(True, ""),
        ),
        console.render_library([], base=base),
        console.render_error("nope", base=base),
    ]
    for page in pages:
        for url in re.findall(r'(?:href|action|src)="(/[^"]*)"', page):
            assert url.startswith(base), url


def test_no_user_text_is_ever_compiled_as_javascript():
    """A filename in an onsubmit="" is a second parse html.escape cannot protect.

    The browser decodes &#x27; before compiling an inline handler, so an ordinary
    ``alice's-cover.png`` breaks the handler (no confirmation, the file is
    deleted on the first click) and a hostile one runs in the console's origin,
    where the URL *is* the credential.
    """
    import re

    import console

    hostile = "x'+alert(1)+'.png"
    pages = [
        console.render_library(
            [{"filename": hostile, "bytes": 10, "mtime": 0, "width": 1, "height": 1, "big": False}],
            base="/kindle/console",
        ),
        console.render_book(
            {"book_id": "x-0123456789ab", "title": "T", "cover": hostile},
            base="/kindle/console", document="<h1>T</h1>",
            assets=[{"filename": hostile, "bytes": 10, "width": 1, "height": 1, "uses": 0}],
            preview=None, stale=False, delivery=(True, ""),
        ),
    ]
    for page in pages:
        assert "onsubmit=" not in page, "confirmations must not be inline handlers"
        assert 'data-confirm="' in page
        # No attribute value may contain a raw quote character of either kind.
        for value in re.findall(r'="([^"]*)"', page):
            assert "'" not in value and '"' not in value


def test_the_editor_carries_the_version_it_was_rendered_from():
    """Without it, a stale tab silently overwrites everything the agent wrote."""
    import console

    page = console.render_book(
        {"book_id": "x-0123456789ab", "title": "T"},
        base="/kindle/console", document="<h1>T</h1>", assets=[],
        preview=None, stale=False, delivery=(True, ""), doc_sha="deadbeefdeadbeef",
    )
    assert '<input type="hidden" name="doc_sha" value="deadbeefdeadbeef">' in page


def test_a_partly_failed_preview_still_shows_what_did_complete():
    import console

    html = console.render_book(
        {"book_id": "x-0123456789ab", "title": "T"},
        base="/kindle/console", document="<h1>T</h1>", assets=[],
        preview={
            "built": "2026-07-31T10:00:00Z",
            "error": "PreviewError: no rasteriser",
            "chapters": [{"title": "One", "chars": 100, "images": 0}],
            "findings": ["BLOCKER — something"],
            "epub_bytes": 1234, "images": 0, "pages": 0, "epub": True,
        },
        stale=False, delivery=(True, ""),
    )
    assert "no rasteriser" in html
    assert "BLOCKER" in html          # the lint survived the rasteriser failing
    assert "book.epub" in html
