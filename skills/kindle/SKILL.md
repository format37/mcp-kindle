---
name: kindle
description: Send rich documents to the user's Kindle via the kindle MCP server. Use whenever the user wants something delivered/sent to their Kindle, or to produce a long-form reading document (report, article, study notes, briefing, book) with tables, diagrams, UML or equations. The server renders inline PlantUML, Mermaid, D2, Graphviz, LaTeX math and SVG itself — do NOT fall back to ASCII diagrams or claim UML/Mermaid/math is unsupported. It can also BUILD the EPUB and hand back rendered page images plus a lint report (preview_book), so you SEE the equations, diagrams and stray symbols before sending instead of hoping. Quick path: send_html_to_kindle. Iterative path with verification: create_book / set_document / preview_book / send_book. ALWAYS attach a grayscale cover.
---

# kindle

The `kindle` MCP server converts an HTML document to EPUB and emails it to
the user's Kindle. It runs **locally** as a Docker container
(`http://localhost:8018/kindle/`), so images can be written straight into its
bind-mounted folder and referenced by filename (see §5).

Everything that used to need a shell — rendering a diagram, rendering an
equation, checking how the book came out — now happens **inside the server**.
Two ways to drive it (§1): a one-shot send, or a **book workspace** that lets
you build the EPUB and *look at the rendered pages* before it ships (§7).

## 0. Every book gets a generated cover (MANDATORY)

Before sending **any** document, attach a **grayscale book cover**. This is not
optional — every Kindle book gets a cover (it's the library thumbnail + opening
page).

Two ways, and **which one works depends on the server's config** — check first:

```bash
curl -s http://localhost:8018/health
# {"status":"healthy","service":"kindle","email_configured":true,
#  "image_generation":false,        <-- no GEMINI_API_KEY -> generate_images is dead
#  "diagram_engines":{"plantuml":true,"graphviz":true,"d2":true,"mermaid":true},
#  "preview":true, ...}
```

(The `curl` check is Claude Code / local only — claude.ai has no shell. On a
remote connector deployment image generation is normally on: use path B, and
if `generate_images` errors about `GEMINI_API_KEY`, fall back to
`add_images_from_urls` with a public image URL.)

**A. `image_generation: false` (a local container without a Gemini key) — use
an image-generation MCP (the examples here use `imagine`) and save the file
yourself.** This is the battle-tested local path:

```bash
DATA=/path/to/mcp-kindle/data
# 1. imagine — a grayscale, e-ink-friendly cover matching the book's topic.
#    Portrait book shape, NO text (Kindle shows `title` as the book name):
#    mcp__imagine__generate_image(
#      prompt="book cover illustration for <topic>, black-and-white ink / engraving
#              style, high contrast, bold central subject, white background, no text",
#      aspect_ratio="2:3", image_size="1K", requester="claude_code")
# 2. download → grayscale → downscale → save into the folder:
curl -s "<full-res URL from the imagine reply>" -o /tmp/cover.jpg
python3 - <<'PY'
from PIL import Image
im = Image.open("/tmp/cover.jpg").convert("L")   # grayscale for e-ink
im.thumbnail((1200, 1600))
im.save("/tmp/cover_k.jpg", "JPEG", quality=85, optimize=True)
PY
cp /tmp/cover_k.jpg "$DATA/cover_<slug>.jpg"
```

Then pass `cover="cover_<slug>.jpg"`. This filename works for **both** paths:
a book workspace resolves bare filenames against its own assets *and* the same
shared `data/` folder.

**B. `image_generation: true` — one call, no shell:**

```
generate_images(book_id, prompts=["a lighthouse in a storm, engraving"],
                names=["cover"], kind="cover")     # 2:3 portrait, no text
send_book(book_id, cover="cover.jpg")
```

`kind="cover"` asks Gemini for a bold central subject on white with no text and
defaults to 2:3. Do not use it as a fallback when health says false — it just
returns `Error: GEMINI_API_KEY is not configured…`.

Grayscale rules: §6.

## 1. Two ways to send — pick deliberately

**Quick path — `send_html_to_kindle`** (the original tool, unchanged):

```
send_html_to_kindle(html_text: str, title: str = "", cover: str = "") -> str
```

One call, no state, no preview. Right for a short/simple document you're
confident about.

**Iterative path — the book workspace** (also available locally, not just from
claude.ai). Content lives server-side and is addressed by `book_id`:

```
create_book(title, notes="") -> book_id      workspace; everything is scoped to it
list_books(limit=20)
book_status(book_id)                          document size + asset filenames
set_document(book_id, html)                   HTML lives server-side
patch_document(book_id, find, replace, count=1)   literal find/replace
generate_images(book_id, prompts, names=[], kind="illustration"|"cover", ...)
add_images_from_urls(book_id, urls, names=[])     public https only
render_diagram(book_id, engine, source, name="")  pre-flight: does it compile?
preview_book(book_id, pages="1-4", cover="", title="")   SEE the pages (§7)
send_book(book_id, cover="", title="")
get_job(job_id)                               poll work that outran the timeout
```

**Prefer the workspace whenever** the document is long, has diagrams, math or
generated images, or will need more than one attempt — because
**`preview_book` only works on a workspace book.** There is no preview for the
one-shot path. Iterating is also cheap there: `patch_document` edits the stored
HTML instead of you re-sending the whole book.

Rules for both:

- `send_to_kindle` (markdown) is **DISABLED**. Always send HTML.
- Pass a full document or a fragment; both work.
- `title` is optional — if omitted it's taken from `<title>`, then the
  first `<h1>`, else "Untitled". Prefer passing it explicitly and keep it
  short — **2-3 words is ideal**. The EPUB file is named
  `YYMMDD-HHMMSS-<title>.epub`, and the **MCP prepends the `YYMMDD-HHMMSS-`
  datetime itself** — so do NOT put a date or time in the title.
- **`cover` is MANDATORY** — the bare filename from §0; it becomes the real
  EPUB cover. (Technically optional in the API, but always supply one.)
- `send_book` refuses an EPUB over **24 MB** (Gmail's outbound cap). Lint warns
  well before that.
- Returns a confirmation string, or `Error: ...` on failure.
- `patch_document` refuses a partial edit: if the anchor occurs more times than
  `count`, it errors with the real count instead of silently changing the first
  one. Lengthen the anchor, or pass `count=0` for all.

**Do not** tell the user UML, Mermaid, diagrams or equations aren't supported,
and do not substitute ASCII-art diagrams "as the real capability." The server
renders real PlantUML, Mermaid, D2, Graphviz, LaTeX and SVG. That is the real
capability — use it.

## 2. Structure & rich content

- **Chapters**: the body splits into a new chapter at every `<h1>`.
  Content before the first `<h1>` becomes a "Preface" chapter.
  `<section>/<article>/<main>` wrappers are flattened first.
- Supported beyond plain markdown: tables with
  `rowspan`/`colspan`/`<caption>`/`<thead>`/`<tbody>`/`<tfoot>`,
  `<dl>/<dt>/<dd>`, `<figure>/<figcaption>`, nested `<blockquote>`,
  `<sup>`, `<sub>`, `<mark>`, `<u>`, `<s>`.
- **Stripped before conversion** (don't rely on them): `<script>`,
  `<style>`, `<link>`, `<meta>`, `<base>`, `<iframe>`, `<embed>`,
  `<object>`, `<form>`, `<input>`, `<button>`, `<noscript>`, every
  `on*` handler, and `javascript:` hrefs. (`<style>` *inside* an
  `<svg>` is preserved.) Inline CSS via `style="..."` attributes is
  kept — use that for styling.

## 3. Diagrams — four engines, all rendered server-side

Embed diagram **source** directly in the HTML inside a `<pre class="…">`. The
server renders each block to a PNG and substitutes an `<img>` at build time.
You write **one self-contained document** — no separate render/upload step, for
any of these:

| Block | Engine | Where it runs |
|---|---|---|
| `<pre class="plantuml">` (aliases `uml`, `puml`) | PlantUML — full UML | local (java + plantuml.jar + graphviz) |
| `<pre class="mermaid">` | Mermaid — flow / sequence / ER / state / gantt | **sidecar** — see §9 |
| `<pre class="d2">` | D2 — clean architecture diagrams, crispest output | local (d2 → SVG → rsvg-convert) |
| `<pre class="graphviz">` (alias `dot`) | Graphviz — raw DOT graphs | local (`dot`) |

`<pre><code class="language-mermaid">` is accepted too.

```html
<pre class="plantuml">
@startuml
skinparam dpi 180
skinparam monochrome true
class Oracle {
  +vectorize(item): Vec
}
Oracle --&gt; Cache : writes
@enduml
</pre>

<pre class="mermaid">
flowchart TD
  A[Ingest] --&gt; B{Valid?}
  B --&gt;|yes| C[Store]
  B --&gt;|no| D[Quarantine]
</pre>
```

Rules that apply to **every** engine:

- **Escape `<` and `>` as `&lt;` / `&gt;`** inside the block — PlantUML and
  Mermaid arrow syntax is full of them (`->`, `-->`, `<|--`, `<<interface>>`,
  `-->|label|`, generics like `Map<K,V>`). Unescaped, the HTML parser eats them
  and the diagram breaks. Other entities pass through fine.
- **One bad block fails the whole send.** The error names the block index and
  engine (`diagram block #3 (mermaid): …`) and carries the renderer's own
  message — read it, fix the source, resend. Use `render_diagram(book_id,
  engine, source)` to check a tricky diagram *before* committing it to the
  document (source there is raw — no HTML escaping).
- Source is capped at 200 000 chars; a render is killed after 60 s.

Engine-specific:

- **PlantUML** — `@startuml`/`@enduml` are optional (any `@startXXX` is
  detected), but include them for clarity. All families work: class, sequence,
  activity, state, use-case, component, deployment, ER, mindmap, gantt, JSON,
  archimate. **Sequence diagrams: never use a self-message `X -> X`** — it
  renders as an ambiguous hook that reads like an incoming arrow from the next
  participant. Use `note over X : …` for internal/self processing.
- **Mermaid** — the server injects
  `%%{init: {"theme":"neutral","themeVariables":{"fontSize":"30px"}} }%%` when
  you supply no init directive, because the renderer works at CSS-pixel scale
  and a default diagram comes out ~750 px wide — thin and grey on e-ink. Your
  own `%%{init}%%` or YAML frontmatter wins, so if you supply one, set a large
  `fontSize` yourself.
- **D2** — rendered at a fixed light theme, 20 px pad, rasterised 1200 px wide.
- **Graphviz** — rendered at 150 dpi; a `dpi=` inside the source still wins.

For hand-drawn vector graphics instead of a generated diagram, use inline
`<svg>…</svg>` directly in the body (sanitised with the rest of the HTML). This
is the **only** correct way to embed SVG — see §5.

### 3.1 draw.io / mxGraph files — render to PNG locally

Mermaid needs no detour: write it inline (above). This path is only for a
**real `.drawio` / mxGraph file** — one the user already has, or one they want
back as an editable diagram:

```bash
DATA=/path/to/mcp-kindle/data          # the MCP's bind-mounted image folder (§5)
drawio -x -f png -o "$DATA/fig_db.png" diagram.drawio   # or any .drawio → PNG renderer
```

Then `<img src="fig_db.png" alt="…">`. (Crisper alternative: render `-f svg` and
inline the resulting `<svg>…</svg>` via §5's vector channel.)

### 3.2 Generated illustrations — via the `imagine` MCP

For **original artwork / illustrations** (cover art, concept or scene images —
*not* diagrams; those go to §3), use an image-generation MCP — the examples
here use **imagine** (`mcp__imagine__generate_image` /
`mcp__imagine__edit_image`); any generator that returns a public HTTPS URL or
a local file works. (With `image_generation: true` on the server, the built-in
`generate_images` is simpler still.) Call it grayscale-friendly and modest, e.g.
`mcp__imagine__generate_image(prompt="black-and-white ink illustration of …, high contrast, clear subject", aspect_ratio="3:4", image_size="1K", requester="claude_code")`.

imagine returns a **public HTTPS asset URL** (`Full-resolution image:
…/imagine/assets/<uuid>.jpg` in the reply). Two ways to get it into the book:

- **Workspace:** `add_images_from_urls(book_id, urls=["<url>"], names=["fig_x"])`
  — the server downloads it, grayscales, resizes and stores it. Public http(s)
  only; private/loopback addresses are refused.
- **One-shot / local file:** curl it and normalise it yourself, exactly as in §0
  (`convert("L")`, `thumbnail((1400, 1400))`, save into `/path/to/mcp-kindle/data/`).

## 4. Math — LaTeX rendered to images

Equations are rasterised server-side (Kindle drops MathML silently), so they
also show up in the preview where you can *see* them:

```html
<pre class="math">\frac{\partial L}{\partial w} = -2X^{T}(y - Xw)</pre>
<div class="math">E = mc^2</div>                    <!-- display, centred -->
<p>the residual <span class="math">r_i = y_i - \hat{y}_i</span> stays small</p>
```

- Display: `<pre class="math">` or `<div class="math">`. Inline:
  `<span class="math">` or `<code class="math">`. An explicit `display` /
  `inline` class overrides the tag default.
- Delimiters are optional and stripped: `$…$`, `$$…$$`, `\[…\]`, `\(…\)`.
  A **bare unescaped `$` inside the element is an error** — one expression per
  element, prose stays outside, `\$` for a literal dollar.
- Display equations are sized in **em**, so they scale with the body text.
- **The typesetter is matplotlib mathtext — a TeX SUBSET.** Supported:
  `\frac \sqrt \sum \int \lim \left \right \text \mathrm \mathbb \boldsymbol
  \operatorname`, accents, Greek, the usual symbols. **Not** supported:
  `\displaystyle`, `\begin{…}` environments (align, matrix, cases), `\\` line
  breaks, `\label` / `\tag`. Split a multi-line derivation into one math element
  per line.
- Errors carry matplotlib's own parse message — read it and fix the source.
- Limits: 2000 chars per element, 500 elements per document.

Do **not** leave raw `$$…$$` / `\(…\)` in running prose — the lint pass (§7)
flags it as unrendered LaTeX, and the reader would see the markup.

## 5. Images — pick the right channel

The MCP runs **locally** and bind-mounts `/path/to/mcp-kindle/data/` into the
container, so you write image files there directly and reference them by **bare
filename**. A workspace book resolves a bare filename against its own assets
first, then that same shared folder. Channels, best first:

| Need | Use |
|---|---|
| UML / logical diagram | `<pre class="plantuml\|mermaid\|d2\|graphviz">` (§3) — self-contained, no file |
| Equation | `<pre class="math">` / `<span class="math">` (§4) — self-contained |
| Any raster (photo, illustration, pre-rendered PNG) | save to `/path/to/mcp-kindle/data/` → `<img src="name.png">` |
| Image that lives at a public https URL | workspace: `add_images_from_urls` (§3.2) |
| Generated illustration / artwork | `imagine` MCP → §3.2 |
| draw.io / mxGraph file the user has | render to PNG into the folder (§3.1) |
| Vector drawing you build by hand | inline `<svg>…</svg>` in the body |
| Tiny one-shot raster | `<img src="data:image/png;base64,…">` (inline) |

- Save to `/path/to/mcp-kindle/data/` with a collision-safe name
  (e.g. `fig_<short>.png`, or `{YYYYMMDD_HHMMSS}_{8hex}.png`); reference by the
  **bare filename only** — no path, no URL.
- Accepted raster types: `png`, `jpeg`, `gif`, `webp`. **SVG**: inline `<svg>`
  only — file-based SVG and `image/svg+xml` data URIs are **rejected**.
- External `http(s)://` `<img>` src is **not fetched** and is stripped (privacy:
  no view-time IP leak) — pull it in with `add_images_from_urls` or download it
  into the folder first.
- Everything the server stores itself (generated, fetched, rendered) is
  normalised for e-ink: grayscale, long side capped at 1400 px, PNG for line
  art / transparency, else JPEG q85. Give your own files the same treatment.
- **Always write `alt` text.** Lint flags missing alt, and it's what the reader
  gets when an image fails.

## 6. Kindle is black-and-white e-ink

The display has no colour. Make diagrams and images legible in
grayscale: use hatching, line style (dashed/dotted), shape, weight, and
explicit labels — **never colour alone** to distinguish things. For
PlantUML, `skinparam monochrome true` plus distinct line styles works
well. Everything is converted to grayscale on the way in anyway, so a
colour-coded legend simply stops working.

## 7. Preview — SEE the book before it ships (REQUIRED for anything non-trivial)

```
preview_book(book_id, pages="1-4", cover="cover_x.jpg", title="")
```

It builds the **real EPUB**, renders it (e-reader-shaped 90×120 mm page) and
returns the page images as **inline image blocks you can actually look at**,
plus a structural summary and a **lint report**. This is the difference between
hoping the equations and diagrams rendered and knowing they did.

- Look at the pages: rendered equations, rendered diagrams, stray symbols,
  broken images, tables that overflow, a diagram too dark or too small to read.
- **Read the lint report and act on it — do not skim it.** It catches what the
  pixels hide, and each finding is labelled `BLOCKER` / `WARNING` / `NOTE`:
  unrendered `<pre class="…">` blocks and literal `@startuml` in the text; raw
  LaTeX left in prose; double-escaped entities (`&amp;lt;`); mojibake; an
  `<img>` whose file never made it into the EPUB; missing alt; images over
  1.5 MB; EPUB over 20 MB (the send will fail); near-empty chapters from a
  stray `<h1>`; tables wider than 8 columns.
- Fix with `patch_document`, preview again, then `send_book`.
- `pages` accepts `"1-4"` (default), a single page `"3"`, or `"all"` (capped at
  20). Each page costs roughly 1.2k tokens of context — preview a range around
  whatever you just changed, not the whole book every time.
- **Honest limits:** this is a **content check, not Kindle's renderer**. The
  pagination is only e-reader-*shaped*; real page breaks and font metrics will
  differ. Kindle Previewer is Windows/Mac only, so this is as close as we get.
  Say that plainly to the user rather than promising pixel fidelity.
- Slow work (a big preview, a batch of generated images) may hand back a
  `job_id` after ~90 s instead of a result — poll `get_job(job_id)`, then call
  the tool again.

## 8. Workflow checklist

1. **Cover (always)** → §0. Check `/health` first to know which route works.
2. Decide the path (§1). Diagrams, math, images or more than one attempt →
   `create_book(title)` and work in the workspace. Trivial and short →
   `send_html_to_kindle`.
3. Build **one HTML document**. Split major sections with `<h1>` (each becomes
   a chapter). Store it with `set_document(book_id, html)`.
4. Diagrams → `<pre class="plantuml|mermaid|d2|graphviz">` (escape `&lt;`/`&gt;`,
   no PlantUML self-messages). Equations → `<pre class="math">` /
   `<span class="math">` (mathtext subset). Hand-drawn vectors → inline `<svg>`.
   A `.drawio` file the user already has → render to PNG (§3.1).
5. Other raster (photos, `imagine` illustrations §3.2) → into the book's assets
   or `/path/to/mcp-kindle/data/` → `<img src="name.png" alt="…">`.
   No external URLs.
6. Grayscale-safe styling throughout (§6).
7. **`preview_book(book_id, pages="1-4", cover="cover_x.jpg")` — look at the
   pages, read the lint report, fix with `patch_document`, preview again.**
   Do not skip this because the HTML "looks fine": diagrams and equations are
   exactly the things that fail silently.
8. `send_book(book_id, cover="cover_x.jpg")` — or
   `send_html_to_kindle(html_text=…, title="…", cover="cover_x.jpg")` on the
   quick path.
9. On `Error:` — read it. Diagram syntax, mathtext-unsupported LaTeX, a missing
   image file and rejected SVG data URIs are the common ones; fix and resend.
   Report the final confirmation (book title, size) to the user honestly — and
   if you skipped the preview, say so.

## 9. Running the MCP (local Docker)

The MCP is a local container bound to `127.0.0.1:8018`, repo at
`/path/to/mcp-kindle`. Token auth is off for localhost. Bring it up / rebuild:

```bash
cd /path/to/mcp-kindle
./compose.sh                                # build + start (127.0.0.1:8018)
curl -s http://localhost:8018/health        # engines, image gen, preview — read it
docker logs -f mcp-kindle                   # tail
```

The image (~815 MB, a ~minutes-long first build) bundles Java + Graphviz +
`plantuml.jar`, the `d2` binary + `rsvg-convert`, poppler (`pdftoppm`,
`pdfinfo`), WeasyPrint's pango/cairo/font stack and matplotlib — so the host
needs nothing but Docker. SMTP creds (a Gmail app password) and an optional
`GEMINI_API_KEY` live in `/path/to/mcp-kindle/.env` (gitignored — the repo is
public; never commit or echo it).

**Mermaid needs its sidecar**, which is behind a compose profile *and* an env
var — both, or `/health` will honestly report `"mermaid": false`:

```bash
./compose.sh --mermaid                # also starts kroki-mermaid (~330 MB RSS)
#   `docker compose up` has no --profile flag; the script sets COMPOSE_PROFILES
#   and MERMAID_URL together, so /health never claims an engine that isn't up.
```

Without it, use `plantuml`, `d2` or `graphviz` — those render locally in the app
image and are always available.

Troubleshooting:

- **`/health` returns only `{"status":"healthy"}`** with no `diagram_engines` /
  `preview` keys → you're talking to a **stale container** from before the
  rebuild. Run `./compose.sh`.
- **Only `send_html_to_kindle` is listed** among the MCP tools → the client is
  holding an old tool list. The other 11 tools appear only after the **MCP
  reconnects** (restart Claude Code / reconnect the server). Don't conclude the
  workspace doesn't exist.
- **Connection/transport error** (not an `Error: …` line from the tool) → the
  container is down; run `./compose.sh`.
- Agent thinks HTML/UML/math is unsupported → stale container, same fix.

## 10. The remote deployment (claude.ai)

The same codebase can also run on a VPS at `https://<host>/kindle/<token>/`
(token-authenticated, `docker-compose.vps.yml`) as a **claude.ai custom
connector**, so books can be written from a phone with no filesystem. That is
why the workspace tools exist at all. There, image generation and the mermaid
sidecar are normally always on, and assets/previews get public URLs.

**If this skill is running on claude.ai** (uploaded via `install.sh --zip`):
you have no shell and no filesystem. Skip every `curl` and local-folder
instruction; work entirely through the workspace tools — `generate_images`
for covers and illustrations, `add_images_from_urls` for anything at a public
URL, `render_diagram` / `preview_book` / `patch_document` exactly as
described above.

**If this skill is running in Claude Code**, talk to the local container —
`http://localhost:8018` — even when a remote deployment exists. Don't be
surprised when a book workspace shows up that you didn't create; the same
server may serve both.
