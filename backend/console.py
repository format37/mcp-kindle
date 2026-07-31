"""Server-rendered private console — browse, edit, preview, send and delete books.

Plain HTML out of the same process that owns the books. No build step, no second
container, no JSON API in between: the console cannot drift out of sync with the
data because it *is* the data. Client-side JavaScript is three small things —
the theme toggle, the select-all niceties and a copy-filename helper — all
progressive enhancement, so every page works with JS disabled.

It is the second driver of this server, beside the MCP tools. Everything an
agent can do to a book from claude.ai you can do here by hand: fix a typo in the
document, swap the cover, drop an illustration in, rebuild the preview and look
at the rendered pages, then mail it to the Kindle.

Visually it is a panel of the same instrument as the podcast console next door
and the blog and portfolio sites: their token block verbatim, their day/night
mechanism, and their shared ``portfolio-theme`` localStorage key.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

TITLE = "kindle"


def _asset_version() -> str:
    """Cache-buster derived from the newest mtime among the served assets.

    Assets are served with a week-long cache (requested on every page load,
    unchanged between deploys), which without this would mean a style or icon
    fix does not reach a browser for seven days — and, worse, that new HTML
    renders against stale CSS. Covering the whole directory rather than just the
    stylesheet means changing only the favicon still bumps the version. Fonts
    are excluded: they are content-stable and would only add churn.
    """
    static = Path(__file__).resolve().parent / "static"
    try:
        newest = max(
            (p.stat().st_mtime for p in static.iterdir() if p.is_file()), default=0.0
        )
        return str(int(newest))
    except OSError:
        return "0"


ASSET_V = _asset_version()

# The pre-paint script, verbatim in spirit from blog-site/src/layouts/Base.astro:
# it must be the first thing in <head>, before any stylesheet, or the wrong
# theme paints for one frame. Light is the boot default, matching the siblings.
_PREPAINT = """
try { document.documentElement.dataset.theme =
        localStorage.getItem('portfolio-theme') || 'light'; }
catch (e) { document.documentElement.dataset.theme = 'light'; }
"""

_TOGGLE_JS = """
(function () {
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  var CANVAS = { dark: '#0a0a0c', light: '#eceae5' };
  function cur() { return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'; }
  function paint() {
    var dark = cur() === 'dark';
    btn.textContent = dark ? '\\u263e NIGHT' : '\\u2600 DAY';
    btn.setAttribute('aria-label', dark ? 'Switch to day theme' : 'Switch to night theme');
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute('content', CANVAS[cur()]);
  }
  btn.addEventListener('click', function () {
    var next = cur() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('portfolio-theme', next); } catch (e) {}
    paint();
  });
  paint();
})();
"""

# Selection is progressive enhancement. Without JS the checkboxes are still real
# form inputs and "delete selected" still submits — only the select-all box, the
# live count and the confirm step need scripting.
_SELECT_JS = """
(function () {
  var form = document.getElementById('bulk');
  if (!form) return;
  var all = document.getElementById('pick-all');
  var btn = document.getElementById('bulk-delete');
  var count = document.getElementById('bulk-count');
  function picks() {
    return Array.prototype.slice.call(form.querySelectorAll('.pick:not(:disabled)'));
  }
  function sync() {
    var boxes = picks();
    var on = boxes.filter(function (b) { return b.checked; });
    count.textContent = on.length ? on.length + ' SELECTED' : '';
    btn.disabled = on.length === 0;
    if (all) {
      all.checked = on.length > 0 && on.length === boxes.length;
      all.indeterminate = on.length > 0 && on.length < boxes.length;
    }
  }
  if (all) {
    all.addEventListener('change', function () {
      picks().forEach(function (b) { b.checked = all.checked; });
      sync();
    });
  }
  form.addEventListener('change', function (e) {
    if (e.target.classList.contains('pick')) sync();
  });
  form.addEventListener('submit', function (e) {
    var n = picks().filter(function (b) { return b.checked; }).length;
    if (!n || !confirm('Delete ' + n + ' book' + (n > 1 ? 's' : '') +
        '? The document, assets and builds are removed permanently.')) {
      e.preventDefault();
    }
  });
  sync();
})();
"""

# Confirmations live here, not in an onsubmit="" attribute, because a filename
# has to appear in the message. The browser decodes character references BEFORE
# compiling an inline handler, so html.escape's &#x27; becomes a real apostrophe
# inside the JS string: an ordinary name like alice's-cover.png then makes the
# handler a syntax error — no listener, no confirmation, the file is deleted on
# the first click — and a hostile name written straight into the bind-mounted
# data/ folder would execute in the console's own origin, where the URL is the
# credential. A data- attribute has no such second parse; the value is only ever
# a string.
_CONFIRM_JS = """
document.addEventListener('submit', function (e) {
  var f = e.target.closest ? e.target.closest('form[data-confirm]') : null;
  if (f && !confirm(f.dataset.confirm)) e.preventDefault();
}, true);
"""

# A filename is the thing you actually need off this page — it goes straight
# into <img src="...">. Clicking one copies it; without JS it stays selectable
# text, which is what you would have had anyway.
_COPY_JS = """
(function () {
  document.addEventListener('click', function (e) {
    var el = e.target.closest ? e.target.closest('.copyname') : null;
    if (!el) return;
    var name = el.dataset.name || el.textContent.trim();
    var done = function () {
      var was = el.textContent;
      el.textContent = 'COPIED';
      el.classList.add('copied');
      setTimeout(function () { el.textContent = was; el.classList.remove('copied'); }, 900);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(name).then(done, function () {});
    }
  });
})();
"""


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _fmt_date(iso: str | None, *, long: bool = False) -> str:
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return esc(str(iso)[:10])
    month = _MONTHS[stamp.month - 1]
    if long:
        return f"{month} {stamp.day}, {stamp.year} {stamp:%H:%M}"
    return f"{month} {stamp.day}"


def _group_key(iso: str | None) -> str:
    if not iso:
        return "UNDATED"
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "UNDATED"
    return f"{_MONTHS[stamp.month - 1]} {stamp.year}"


def _ago(iso: str | None) -> str:
    """Coarse "how long ago" — enough to tell fresh from stale at a glance."""
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - stamp).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def fmt_bytes(n: Any) -> str:
    try:
        size = float(n or 0)
    except (TypeError, ValueError):
        return ""
    if size >= 1_048_576:
        return f"{size / 1_048_576:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{int(size)} B"


def _shell(
    *,
    title: str,
    base: str,
    body: str,
    read: bool = False,
    select: bool = False,
    copy: bool = False,
    refresh: int = 0,
) -> str:
    """The page frame: head, masthead, content, footer.

    ``refresh`` sets a meta refresh, which is how a running preview or send is
    followed without a line of polling JavaScript.
    """
    wrap = "wrap wrap-read" if read else "wrap"
    scripts = (
        _TOGGLE_JS + _CONFIRM_JS
        + (_SELECT_JS if select else "")
        + (_COPY_JS if copy else "")
    )
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<script>{_PREPAINT}</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{meta_refresh}
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#eceae5">
<link rel="icon" type="image/svg+xml" href="{base}/assets/favicon.svg?v={ASSET_V}">
<link rel="alternate icon" type="image/png" href="{base}/assets/favicon-32.png?v={ASSET_V}">
<link rel="apple-touch-icon" href="{base}/assets/apple-touch-icon.png?v={ASSET_V}">
<link rel="preload" href="{base}/assets/fonts/jetbrains-mono-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{base}/assets/fonts/inter-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{base}/assets/console.css?v={ASSET_V}">
</head>
<body>
<div class="{wrap}">
<header class="mast">
  <a class="brand" href="{base}">{esc(TITLE)} <span class="dim">// console</span></a>
  <nav class="mast-right">
    <a class="plink" href="{base}/library">LIBRARY</a>
    <a class="plink" href="{base}/settings">SETTINGS</a>
    <button class="toggle" id="theme-toggle" aria-label="Switch theme">&#9728; DAY</button>
  </nav>
</header>
{body}
<footer class="foot">
  <span>KINDLE <span class="sep">//</span> CONSOLE</span>
  <span>PRIVATE &#183; NOT INDEXED</span>
</footer>
</div>
<script>{scripts}</script>
</body>
</html>"""


def _stat(label: str, value: str, unit: str = "", extra: str = "") -> str:
    unit_html = f' <span class="unit">{esc(unit)}</span>' if unit else ""
    return (
        f'<div class="stat"><span class="stat-k">{esc(label)}</span>'
        f'<span class="stat-v">{esc(value)}{unit_html}</span>{extra}</div>'
    )


def notice(message: str, *, bad: bool = False) -> str:
    if not message:
        return ""
    css = "note notice notice-bad" if bad else "note notice"
    return f'<p class="{css}">{esc(message)}</p>'


def _badge(label: str, kind: str = "") -> str:
    css = f"state state-{kind}" if kind else "state"
    return f'<span class="{css}">{esc(label)}</span>'


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------


def render_index(
    rows: list[dict[str, Any]],
    *,
    base: str,
    totals: dict[str, Any],
    delivery: tuple[bool, str],
    notices: str = "",
) -> str:
    """The whole catalogue on one page — no pagination, like the podcast console."""
    ready, reason = delivery
    stats = [
        _stat("Books", str(len(rows))),
        _stat("Assets", str(totals.get("assets", 0))),
        _stat("On disk", fmt_bytes(totals.get("bytes", 0))),
        _stat("Library", str(totals.get("library_count", 0)), "images"),
        _stat(
            "Delivery",
            "READY" if ready else "OFF",
            "",
            f'<p class="stat-note">{esc(totals.get("recipient") or reason)}</p>',
        ),
    ]

    if not rows:
        body = (
            f'<div class="strip">{"".join(stats)}</div>{notices}'
            '<div class="empty">No books yet.<br>'
            'Ask the agent for a book — or start one with <code>create_book</code> — '
            'and it will appear here.</div>'
        )
        return _shell(title=f"{TITLE} // console", base=base, body=body)

    items: list[str] = []
    group = None
    for row in rows:
        key = _group_key(row.get("updated"))
        if key != group:
            group = key
            items.append(f'<div class="grouprule">{esc(key)}</div>')

        book_id = esc(row.get("book_id"))
        title = esc(row.get("title") or book_id)

        bits = [_fmt_date(row.get("updated")), f"{int(row.get('doc_chars') or 0):,} chars"]
        if row.get("asset_count"):
            bits.append(f"{int(row['asset_count'])} assets")
        if row.get("bytes"):
            bits.append(fmt_bytes(row["bytes"]))
        if not row.get("doc_chars"):
            bits.append(_badge("NO DOCUMENT", "empty"))
        # "PREVIEWED" has to mean "previewed as it stands now" or it is worse
        # than no badge at all — a stale preview is exactly the thing you would
        # trust and should not.
        if row.get("sent_at") and not row.get("preview_stale"):
            bits.append(_badge("SENT", "done"))
        elif row.get("sent_at"):
            bits.append(_badge("SENT, THEN EDITED", "busy"))
        elif row.get("has_preview") and not row.get("preview_stale"):
            bits.append(_badge("PREVIEWED", "busy"))
        elif row.get("has_preview"):
            bits.append(_badge("PREVIEW STALE", "empty"))

        notes = row.get("notes") or ""
        desc = f'<p class="row-desc">{esc(notes)}</p>' if notes else ""

        items.append(
            f'<div class="row">'
            f'<input type="checkbox" class="pick" name="id" value="{book_id}"'
            f' aria-label="Select {title}">'
            f'<a class="row-main" href="{base}/{book_id}">'
            f'<div class="meta">{(" <span class=sep>&#183;</span> ").join(bits)}</div>'
            f'<div class="row-title">{title}</div>'
            f'{desc}<div class="row-id">{book_id}</div></a></div>'
        )

    toolbar = (
        '<div class="bulkbar">'
        '<label class="pickall">'
        '<input type="checkbox" class="pick-all" id="pick-all">'
        "<span>SELECT ALL</span></label>"
        '<span class="bulk-count" id="bulk-count"></span>'
        '<button class="btn btn-danger" id="bulk-delete" type="submit">'
        "DELETE SELECTED</button>"
        "</div>"
    )

    body = (
        f'<div class="strip">{"".join(stats)}</div>{notices}'
        f'<form id="bulk" method="post" action="{base}/delete-selected">'
        f'{toolbar}{"".join(items)}</form>'
    )
    return _shell(title=f"{TITLE} // console", base=base, body=body, select=True)


# --------------------------------------------------------------------------
# Book page
# --------------------------------------------------------------------------


def _asset_tile(book_id: str, asset: dict[str, Any], *, base: str, is_cover: bool) -> str:
    name = esc(asset["filename"])
    dims = (
        f'{asset.get("width")}&#215;{asset.get("height")}'
        if asset.get("width") and asset.get("height")
        else "?"
    )
    mark = '<span class="tile-mark">COVER</span>' if is_cover else ""
    # Whether the document actually points at this image. Deleting an unused
    # asset is free; deleting a used one leaves an <img> resolving to nothing,
    # and the EPUB still *builds* — only the lint report notices.
    uses = int(asset.get("uses") or 0)
    if is_cover:
        usage = ""
    elif uses:
        usage = f' &#183; <span class="dim">&#215;{uses}</span>'
    else:
        usage = ' &#183; <span class="warn">UNUSED</span>'
    return (
        f'<figure class="tile">'
        f'<a class="tile-img" href="{base}/{esc(book_id)}/file/{name}" target="_blank" rel="noopener">'
        f'<img src="{base}/{esc(book_id)}/thumb/{name}" alt="{name}" loading="lazy">{mark}</a>'
        f'<figcaption>'
        f'<button type="button" class="copyname" data-name="{name}" '
        f'title="Copy the filename">{name}</button>'
        f'<span class="tile-meta">{dims} &#183; {esc(fmt_bytes(asset.get("bytes")))}{usage}</span>'
        f'</figcaption>'
        f'<form method="post" action="{base}/{esc(book_id)}/asset-delete" class="tile-del"'
        f' data-confirm="Delete {name}? Any &lt;img&gt; still pointing at it will '
        f'silently vanish from the EPUB.">'
        f'<input type="hidden" name="filename" value="{name}">'
        f'<button class="btn btn-mini btn-danger" type="submit">DELETE</button></form>'
        f"</figure>"
    )


def _render_preview(preview: dict[str, Any] | None, *, base: str, book_id: str, stale: bool) -> str:
    if not preview:
        return (
            '<h2>Preview</h2>'
            '<p class="note">No preview built yet. Building one renders the real EPUB, '
            'rasterises its pages and lints the result — the same check the agent runs '
            'before sending.</p>'
        )

    # A failure partway through still leaves useful ground: the EPUB may have
    # built and linted cleanly and only the rasteriser fell over (no WeasyPrint
    # in the image, say). Show the error *and* whatever did complete, rather
    # than throwing away a good lint report because the pictures failed.
    failure = (
        f'<p class="note notice notice-bad">Preview failed: {esc(preview["error"])}</p>'
        if preview.get("error")
        else ""
    )
    if failure and not preview.get("chapters"):
        return (
            "<h2>Preview</h2>" + failure
            + f'<p class="note">Attempted {esc(_ago(preview.get("built")))}.</p>'
        )

    chapters = preview.get("chapters") or []
    bits = [
        f'{esc(fmt_bytes(preview.get("epub_bytes")))} EPUB',
        f"{len(chapters)} chapter{'s' if len(chapters) != 1 else ''}",
        f"{int(preview.get('images') or 0)} image(s)",
        f"{int(preview.get('pages') or 0)} page(s)",
    ]
    head = (
        f'<div class="meta">{(" <span class=sep>&#183;</span> ").join(bits)}'
        f' <span class=sep>&#183;</span> {esc(_ago(preview.get("built")))}</div>'
    )

    warn = ""
    if stale:
        warn = (
            '<p class="note notice">The document has changed since this preview was '
            'built. Rebuild it before trusting the pages below.</p>'
        )

    findings = preview.get("findings") or []
    if findings:
        lint = (
            f'<p class="lint-head">LINT &#8212; {len(findings)} finding'
            f'{"s" if len(findings) != 1 else ""}, fix these before sending:</p>'
            '<ul class="lint">'
            + "".join(f"<li>{esc(f)}</li>" for f in findings)
            + "</ul>"
        )
    else:
        lint = '<p class="lint-head lint-ok">LINT &#8212; clean.</p>'

    chapter_rows = "".join(
        f'<tr><td>{esc(c.get("title"))}</td><td>{int(c.get("chars") or 0):,}</td>'
        f'<td>{int(c.get("images") or 0)}</td></tr>'
        for c in chapters
    )
    chapter_table = (
        '<div class="tblwrap"><table><thead><tr><th>Chapter</th><th>Chars</th>'
        f"<th>Images</th></tr></thead><tbody>{chapter_rows}</tbody></table></div>"
        if chapters
        else ""
    )

    images = preview.get("page_images") or []
    shown = "".join(
        f'<a class="page" href="{base}/{esc(book_id)}/out/{esc(name)}" target="_blank" rel="noopener">'
        f'<img src="{base}/{esc(book_id)}/out/{esc(name)}" alt="page" loading="lazy">'
        f'<span>{esc(name.replace("page_", "").replace(".jpg", "").lstrip("0") or "1")}</span></a>'
        for name in images
    )
    strip = f'<div class="pages">{shown}</div>' if shown else ""
    more = ""
    if preview.get("note"):
        more = f'<p class="note">{esc(preview["note"])}</p>'

    downloads = []
    if preview.get("epub"):
        downloads.append(
            f'<a class="btn" href="{base}/{esc(book_id)}/out/book.epub" download>EPUB</a>'
        )
    if preview.get("pdf"):
        downloads.append(
            f'<a class="btn" href="{base}/{esc(book_id)}/out/preview.pdf" download>PREVIEW.PDF</a>'
        )
    downloads.append(f'<a class="btn" href="{base}/{esc(book_id)}/doc.html" download>DOC.HTML</a>')

    return (
        f"<h2>Preview</h2>{head}{failure}{warn}{lint}{strip}{more}{chapter_table}"
        f'<div class="dl">{"".join(downloads)}</div>'
    )


def _render_delivery(meta: dict[str, Any], *, base: str, ready: bool, reason: str) -> str:
    sent = meta.get("last_sent") or {}
    rows = []
    if sent:
        ok = bool(sent.get("ok"))
        css = "ok" if ok else "bad"
        rows.append(
            f'<dt>status</dt><dd><span class="dstate d-{css}">'
            f'{"SENT" if ok else "FAILED"}</span>'
            + (f' &#183; {esc(sent.get("error"))}' if not ok and sent.get("error") else "")
            + "</dd>"
        )
        rows.append(
            f'<dt>at</dt><dd>{esc(_fmt_date(sent.get("at"), long=True))} '
            f'<span class="dim">({esc(_ago(sent.get("at")))})</span></dd>'
        )
        if sent.get("recipient"):
            rows.append(f'<dt>to</dt><dd>{esc(sent["recipient"])}</dd>')
        if sent.get("title"):
            rows.append(f'<dt>as</dt><dd>{esc(sent["title"])}</dd>')
        if sent.get("bytes"):
            rows.append(f'<dt>size</dt><dd>{esc(fmt_bytes(sent["bytes"]))}</dd>')
    else:
        rows.append('<dt>status</dt><dd class="prose">Never sent to the Kindle.</dd>')

    if not ready:
        tail = (
            f'<p class="note">Delivery is not configured &#8212; {esc(reason)}. '
            f'Set it up in <a href="{base}/settings">settings</a>.</p>'
        )
    else:
        tail = ""
    return f'<h2>Delivery</h2><dl class="dl-grid">{"".join(rows)}</dl>{tail}'


def _job_banner(job: dict[str, Any] | None) -> tuple[str, int]:
    """(html, refresh_seconds) for a running or just-finished console job."""
    if not job:
        return "", 0
    status = job.get("status")
    label = str(job.get("label") or "job")
    if status == "running":
        elapsed = float(job.get("elapsed_s") or 0)
        return (
            f'<p class="note notice running">{esc(label)} &#8212; running '
            f"({elapsed:.0f}s). This page refreshes itself.</p>",
            3,
        )
    if status == "error":
        return notice(f"{label} failed: {job.get('error')}", bad=True), 0
    if job.get("kind") == "preview":
        # A preview job "finishes" even when the build inside it failed — it
        # records the failure rather than raising, so the page can show it. A
        # "finished" banner over a "Preview failed" panel would be the console
        # contradicting itself; the panel below is the report.
        return "", 0
    return notice(f"{label} finished."), 0


def render_book(
    meta: dict[str, Any],
    *,
    base: str,
    document: str,
    assets: list[dict[str, Any]],
    preview: dict[str, Any] | None,
    stale: bool,
    delivery: tuple[bool, str],
    job: dict[str, Any] | None = None,
    notices: str = "",
    doc_sha: str = "",
) -> str:
    book_id = str(meta.get("book_id") or "")
    ready, reason = delivery
    banner, refresh = _job_banner(job)
    busy = bool(job and job.get("status") == "running")

    bits = [
        _fmt_date(meta.get("created"), long=True),
        f"{int(meta.get('doc_chars') or 0):,} chars",
        f"{len(assets)} asset{'s' if len(assets) != 1 else ''}",
    ]
    if meta.get("updated"):
        bits.append(f"updated {esc(_ago(meta['updated']))}")

    disabled = ' disabled title="A build is already running"' if busy else ""
    actions = (
        f'<form method="post" action="{base}/{esc(book_id)}/preview" class="act">'
        f'<button class="btn" type="submit"{disabled}>BUILD PREVIEW</button></form>'
    )
    if ready:
        actions += (
            f'<form method="post" action="{base}/{esc(book_id)}/send" class="act"'
            f' data-confirm="Build this book and email it to the Kindle?">'
            f'<button class="btn" type="submit"{disabled}>SEND TO KINDLE</button></form>'
        )
    else:
        actions += (
            f'<a class="btn btn-off" href="{base}/settings" '
            f'title="{esc(reason)}">SEND &#8212; NOT CONFIGURED</a>'
        )

    cover = str(meta.get("cover") or "")
    options = ['<option value="">(no cover)</option>']
    for asset in assets:
        name = esc(asset["filename"])
        picked = " selected" if asset["filename"] == cover else ""
        options.append(f'<option value="{name}"{picked}>{name}</option>')

    editor = f"""
<h2>Document</h2>
<form method="post" action="{base}/{esc(book_id)}/document" class="settings"
      accept-charset="utf-8">
  <input type="hidden" name="doc_sha" value="{esc(doc_sha)}">
  <div class="field">
    <label for="title">Title</label>
    <input type="text" id="title" name="title" value="{esc(meta.get("title") or "")}"
           spellcheck="false" autocomplete="off">
    <p class="hint">Two or three words read best in the Kindle library. The send
       step prefixes the date itself, so leave dates out.</p>
  </div>
  <div class="field">
    <label for="cover">Cover</label>
    <select id="cover" name="cover">{"".join(options)}</select>
    <p class="hint">Becomes the library thumbnail and the opening page. Upload one
       below, or ask the agent for <code>generate_images(kind="cover")</code>.</p>
  </div>
  <div class="field">
    <label for="notes">Notes</label>
    <input type="text" id="notes" name="notes" value="{esc(meta.get("notes") or "")}"
           autocomplete="off">
    <p class="hint">Your own note about the book&#8217;s plan. Shown in the list and
       to the agent by <code>book_status</code>.</p>
  </div>
  <div class="field">
    <label for="html">HTML</label>
    <textarea id="html" name="html" class="editor" spellcheck="false"
              rows="26">{esc(document)}</textarea>
    <p class="hint">The stored document, exactly as <code>set_document</code> left it.
       Chapters split at every <code>&lt;h1&gt;</code>; images are referenced by bare
       filename. <code>&lt;pre class="mermaid|plantuml|d2|graphviz|math"&gt;</code>
       blocks are rendered to images at build time.</p>
  </div>
  <div class="actions">
    <button class="btn" type="submit">SAVE DOCUMENT</button>
  </div>
</form>"""

    tiles = "".join(
        _asset_tile(book_id, asset, base=base, is_cover=asset["filename"] == cover)
        for asset in assets
    )
    gallery = (
        f'<div class="grid">{tiles}</div>'
        if tiles
        else '<p class="note">No assets yet. Upload one below, or let the agent generate '
             'illustrations into this book.</p>'
    )
    upload = f"""
<form method="post" action="{base}/{esc(book_id)}/upload" enctype="multipart/form-data"
      class="upload">
  <input type="file" name="file" accept="image/*" multiple required>
  <button class="btn" type="submit">UPLOAD</button>
</form>
<p class="note">Uploads are converted to grayscale and downscaled for e-ink, exactly
   like a generated illustration &#8212; that normalisation is what keeps every image
   in a book looking the same.</p>"""

    parts = [
        f'<p style="margin:22px 0 0"><a class="back" href="{base}">&#8592; ALL BOOKS</a></p>',
        f'<h1 class="title">{esc(meta.get("title") or book_id)}</h1>',
        f'<div class="meta">{(" <span class=sep>&#183;</span> ").join(bits)}</div>',
        f'<div class="row-id">{esc(book_id)}</div>',
        banner,
        notices,
        f'<div class="actbar">{actions}</div>',
        _render_preview(preview, base=base, book_id=book_id, stale=stale),
        _render_delivery(meta, base=base, ready=ready, reason=reason),
        editor,
        "<h2>Assets</h2>",
        gallery,
        upload,
        '<div class="danger"><form method="post" '
        f'action="{base}/{esc(book_id)}/delete" '
        'data-confirm="Delete this book? The document, every asset and every build '
        'are removed permanently.">'
        '<button class="btn btn-danger" type="submit">DELETE BOOK</button>'
        '</form><p class="note">Removes the whole workspace from disk. '
        "There is no undo.</p></div>",
    ]

    return _shell(
        title=f"{meta.get('title') or book_id} // {TITLE}",
        base=base,
        body="".join(parts),
        copy=True,
        refresh=refresh,
    )


# --------------------------------------------------------------------------
# Library (the flat data/ folder)
# --------------------------------------------------------------------------


def render_library(
    rows: list[dict[str, Any]], *, base: str, notices: str = "", data_dir: str = ""
) -> str:
    total = sum(r["bytes"] for r in rows)
    stats = [
        _stat("Images", str(len(rows))),
        _stat("On disk", fmt_bytes(total)),
        _stat("Oversize", str(sum(1 for r in rows if r["big"]))),
    ]

    tiles = []
    for row in rows:
        name = esc(row["filename"])
        dims = (
            f'{row.get("width")}&#215;{row.get("height")}'
            if row.get("width") and row.get("height")
            else "?"
        )
        big = ' <span class="warn">BIG</span>' if row["big"] else ""
        tiles.append(
            f'<figure class="tile">'
            f'<a class="tile-img" href="{base}/library/file/{name}" target="_blank" rel="noopener">'
            f'<img src="{base}/library/thumb/{name}" alt="{name}" loading="lazy"></a>'
            f"<figcaption>"
            f'<button type="button" class="copyname" data-name="{name}" '
            f'title="Copy the filename">{name}</button>'
            f'<span class="tile-meta">{dims} &#183; {esc(fmt_bytes(row["bytes"]))}{big}</span>'
            f"</figcaption>"
            f'<form method="post" action="{base}/library/delete" class="tile-del"'
            f' data-confirm="Delete {name}? Any document referencing it by bare '
            f'filename will silently lose the image.">'
            f'<input type="hidden" name="filename" value="{name}">'
            f'<button class="btn btn-mini btn-danger" type="submit">DELETE</button></form>'
            f"</figure>"
        )

    grid = (
        f'<div class="grid">{"".join(tiles)}</div>'
        if tiles
        else '<div class="empty">No images in the data folder yet.</div>'
    )

    body = f"""
<p style="margin:22px 0 0"><a class="back" href="{base}">&#8592; ALL BOOKS</a></p>
<h1 class="title">Library</h1>
<div class="meta">SHARED DATA FOLDER{f" &#183; {esc(data_dir)}" if data_dir else ""}</div>
<p class="note">Images here are what <code>send_html_to_kindle</code> resolves a bare
   <code>&lt;img src="cover.jpg"&gt;</code> against, and what Claude Code drops in over
   the bind mount. Click a filename to copy it. Bytes are stored and embedded
   unchanged &#8212; nothing here is re-encoded, so a picture you prepared reaches the
   EPUB exactly as you made it. A book&#8217;s own assets shadow this folder.</p>
<div class="strip">{"".join(stats)}</div>
{notices}
<form method="post" action="{base}/library/upload" enctype="multipart/form-data" class="upload">
  <input type="file" name="file" accept="image/*" multiple required>
  <button class="btn" type="submit">UPLOAD</button>
</form>
{grid}
"""
    return _shell(title=f"Library // {TITLE}", base=base, body=body, copy=True)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


_SOURCE_NOTE = {
    "console": "Set here, in the console.",
    "env": "Coming from the environment (<code>.env</code>). Type a value to override it.",
    "": "Not set.",
}


def _field(
    key: str,
    label: str,
    view: dict[str, Any],
    *,
    hint: str,
    secret: bool = False,
    placeholder: str = "",
) -> str:
    """One settings row.

    A secret input is rendered **empty**, never pre-filled with its own mask.
    Round-tripping a mask through a form and recognising it on the way back is
    one encoding mishap away from writing the mask itself into the credential —
    a failure whose first symptom is an SMTP 535 days later. Empty means
    "leave it alone", and clearing is a deliberate, separate checkbox.
    """
    info = view[key]
    source = _SOURCE_NOTE[info["source"]]
    if info["overridden"]:
        source = (
            "Set here, in the console &#8212; this <strong>overrides</strong> a value that "
            "is also in the environment. Tick <em>clear</em> to fall back to it."
        )

    if not secret:
        return f"""
  <div class="field">
    <label for="{key}">{esc(label)}</label>
    <input type="text" id="{key}" name="{key}" value="{esc(info["value"])}"
           spellcheck="false" autocomplete="off" placeholder="{esc(placeholder)}">
    <p class="hint">{hint} <span class="dim">{source}</span></p>
  </div>"""

    if info["set"]:
        state = (
            f' Currently <code>{esc(info["display"])}</code> &#8212; '
            "leave blank to keep it, type to replace it."
        )
        clear = f"""
    <label class="pickall clear-box" for="clear_{key}">
      <input type="checkbox" class="pick-all" id="clear_{key}" name="clear_{key}">
      <span>CLEAR IT</span></label>"""
    else:
        state = ""
        clear = ""
    # autocomplete="new-password", not "off": browsers ignore "off" on password
    # inputs, and a manager autofilling this one would otherwise post a stale
    # credential over a good one.
    return f"""
  <div class="field">
    <label for="{key}">{esc(label)}</label>
    <input type="password" id="{key}" name="{key}" value=""
           spellcheck="false" autocomplete="new-password"
           placeholder="{esc(placeholder)}">{clear}
    <p class="hint">{hint}{state} <span class="dim">{source}</span></p>
  </div>"""


def render_settings(
    view: dict[str, Any],
    *,
    base: str,
    notices: str = "",
    capabilities: dict[str, Any] | None = None,
) -> str:
    fields = (
        _field(
            "sender_email", "Sender Gmail", view,
            hint="The Gmail account the EPUB is mailed from. It must be listed as an "
                 "<strong>Approved Personal Document E-mail</strong> on Amazon, or the "
                 "Kindle silently drops every book.",
            placeholder="you@gmail.com",
        )
        + _field(
            "gmail_app_pass", "Gmail app password", view, secret=True,
            hint="Google account &#8594; Security &#8594; App passwords. Not your login "
                 "password, and not reversible from this page.",
            placeholder="16 characters",
        )
        + _field(
            "recipient_email", "Send-to-Kindle address", view,
            hint="Amazon &#8594; Manage Your Content and Devices &#8594; Preferences. "
                 "Usually <code>something@kindle.com</code>.",
            placeholder="you@kindle.com",
        )
        + _field(
            "gemini_api_key", "Gemini API key", view, secret=True,
            hint="Enables <code>generate_images</code> for covers and illustrations. "
                 "Saving a new key takes effect immediately &#8212; no restart.",
            placeholder="AIza…",
        )
    )

    caps = ""
    if capabilities:
        yes = '<span class="dstate d-ok">YES</span>'
        no = '<span class="dstate d-bad">NO</span>'
        rows = "".join(
            f"<dt>{esc(name)}</dt><dd>{yes if ok else no}</dd>"
            for name, ok in capabilities.items()
        )
        caps = (
            "<h2>Server capabilities</h2>"
            f'<dl class="dl-grid">{rows}</dl>'
            '<p class="note">Read from the running container, not from configuration. '
            "A renderer listed as NO is missing from the image or its sidecar is down.</p>"
        )

    body = f"""
<p style="margin:22px 0 0"><a class="back" href="{base}">&#8592; ALL BOOKS</a></p>
<h1 class="title">Settings</h1>
<div class="meta">KINDLE DELIVERY &#183; IMAGE GENERATION</div>
{notices}
<form method="post" action="{base}/settings" class="settings" accept-charset="utf-8">
{fields}
  <div class="actions">
    <button class="btn" type="submit" name="action" value="save">SAVE</button>
    <button class="btn" type="submit" name="action" value="test">SAVE &amp; SEND TEST</button>
  </div>
</form>
<p class="note"><strong>SAVE &amp; SEND TEST</strong> mails a one-page EPUB to the
   Send-to-Kindle address. That is the only test worth running: an SMTP login can
   succeed while Amazon still discards the mail because the sender is not an
   approved one, and the failure is silent. If the test book does not appear on the
   device within a minute or two, the address is wrong or the sender is not
   approved.</p>
<p class="note">Values are stored in <code>data/settings.json</code> (chmod 600) and
   secrets are never rendered back to this page, only described. Setting them in the
   environment instead keeps them off disk entirely. Emptying an address field, or
   ticking <em>clear it</em> on a secret, drops the console&#8217;s override and falls
   back to the environment value.</p>
{caps}
"""
    return _shell(title=f"Settings // {TITLE}", base=base, body=body, read=True)


def render_error(message: str, *, base: str, status: int = 404) -> str:
    return _shell(
        title=f"{status} // {TITLE}",
        base=base,
        body=(
            f'<div class="empty">{esc(message)}<br>'
            f'<a class="back" href="{base}">&#8592; ALL BOOKS</a></div>'
        ),
    )
