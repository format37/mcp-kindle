# mcp-kindle

An MCP server that composes a book, shows you how it will actually render, and
emails it to a Kindle.

Give it HTML; it renders the diagram and math blocks to images, embeds the
pictures, builds an EPUB, rasterises that EPUB back into page images you can
look at, lints it for the things pixels hide, and sends it over Gmail SMTP to
your Send-to-Kindle address.

Everything that produces bytes — Gemini illustrations, PlantUML/Graphviz/D2/
Mermaid diagrams, LaTeX, the EPUB, the preview pages — happens inside the
server. The client only ever sends text.

---

## Two ways to drive it

| | **Local** (Claude Code) | **Remote** (claude.ai web, phone) |
|---|---|---|
| Transport | `http://localhost:8018/kindle/` | `https://<host>/kindle/<TOKEN>/` behind Caddy |
| Compose file | `docker-compose.yml` | `docker-compose.vps.yml` |
| Auth | `MCP_TOKENS` empty => disabled | `MCP_REQUIRE_AUTH=true` + tokens in the URL |
| Where images come from | files written into the bind-mounted `./data/` | `generate_images` / `add_images_from_urls`, server-side |
| Where the document lives | in the tool call | on the server, in a book workspace |
| Main tool | `send_html_to_kindle` (one shot) | `create_book` -> `set_document` -> `preview_book` -> `send_book` |

They differ because claude.ai has no filesystem and no shell. It cannot write a
PNG, cannot run a renderer, and cannot open an EPUB or a PDF. So content is
addressed by **id** instead of by path, and verification happens by handing the
model *images of the rendered pages* — the one artifact form it can actually
inspect.

The local flow is unchanged from the original server and still works exactly as
before; the two share the same build pipeline.

### The remote loop

```
create_book(title)                          -> book_id
generate_images(book_id, prompts=[...])        illustrations, and kind="cover"
add_images_from_urls(book_id, urls=[...])      images you already have
set_document(book_id, html)                    reference assets by BARE FILENAME
preview_book(book_id)                          LOOK at the pages + read the lint
patch_document(book_id, find, replace)         fix, preview again
send_book(book_id, cover="cover.jpg")
```

A book workspace is `data/books/<book_id>/` holding `meta.json`, `doc.html`,
`assets/` and `out/`. `book_id` is `<slug>-<12 hex>`. Workspaces older than
`BOOK_MAX_AGE_DAYS` (30) are pruned at startup.

---

## Tools

| Tool | Purpose |
|---|---|
| `create_book(title, notes="")` | Open a workspace; returns `book_id`. Keep the title 2-3 words, no date. |
| `list_books(limit=20)` | Recent workspaces, newest first, with sizes. |
| `book_status(book_id)` | Document size, notes, and every asset: filename, dimensions, bytes, public URL. |
| `set_document(book_id, html)` | Store the book's HTML server-side (replaces the previous version). |
| `patch_document(book_id, find, replace, count=1)` | Literal (non-regex) find/replace. Refuses a partial edit if the anchor is ambiguous. |
| `generate_images(book_id, prompts, names=[], kind="illustration", aspect_ratio="", image_size="1K", model="pro")` | Gemini generation, whole batch concurrently, straight into the book. `kind="cover"` asks for a bold central subject on white with no text and defaults to 2:3. |
| `add_images_from_urls(book_id, urls, names=[])` | Download public http(s) images into the book. SSRF-guarded: private, loopback and link-local addresses are refused, on every redirect hop. |
| `render_diagram(book_id, engine, source, name="")` | Render one diagram to a stored PNG. Optional — use it to check a tricky diagram compiles before committing it to the document. |
| `preview_book(book_id, pages="1-4", cover="", title="")` | Build the EPUB, return page **images** plus a lint report. See below. |
| `send_book(book_id, cover="", title="")` | Build and email the EPUB. |
| `get_job(job_id)` | Poll work that outran the tool timeout. |
| `send_html_to_kindle(html_text, title="", cover="")` | The original local one-shot: HTML in, EPUB mailed out, images read by bare filename from `data/`. |

Slow tools (`generate_images`, `add_images_from_urls`, `preview_book`) answer
inline when they finish within `MCP_SOFT_TIMEOUT_S` (90s) and otherwise hand
back a `job_id`. Jobs live in the server process only: they do not survive a
restart, and finished results are dropped after `JOB_TTL_S` (1h).

The server also exposes an MCP resource, `kindle://documentation`, which reports
the workflow plus which engines are actually ready on that deployment.

---

## Authoring

### Chapters and HTML

The document may be a full HTML file or a fragment. **Chapters split at every
`<h1>`**; content before the first `<h1>` becomes a "Preface". `<section>`,
`<article>` and `<main>` wrappers are flattened first, so an `<h1>` nested
inside one still starts a chapter. The book title comes from the `title`
argument, else `<title>`, else the first `<h1>`.

Survives conversion: tables with `rowspan`/`colspan`, `<figure>`/`<figcaption>`,
`<dl>`, `<blockquote>`, `<sup>`/`<sub>`/`<mark>`, `<hr>`, inline `<svg>`, and
inline `style="..."` attributes.

Stripped for safety: `<script>`, `<iframe>`, `<embed>`, `<object>`,
`<noscript>`, `<form>`, `<input>`, `<button>`, `<meta>`, `<link>`, `<base>`,
`on*` handlers and `javascript:` hrefs. `<style>` elements are stripped too
(except inside an `<svg>`, where they style the diagram) — style with inline
`style="..."` instead.

The destination is a black-and-white e-ink screen. Never distinguish things by
colour alone; use line style, hatching, shape and labels.

### Diagrams

Put the source directly in the document. The server renders each block to PNG
at build time and swaps in an `<img>`.

| Block | Engine | Runs |
|---|---|---|
| `<pre class="plantuml">` (aliases `uml`, `puml`) | `java -jar plantuml.jar` | in the image |
| `<pre class="graphviz">` (alias `dot`) | `dot` | in the image |
| `<pre class="d2">` | `d2` -> SVG -> `rsvg-convert` -> PNG | in the image |
| `<pre class="mermaid">` | `yuzutech/kroki-mermaid` companion over HTTP | sidecar container |

`<pre><code class="language-mermaid">` is accepted as well. `@startuml` /
`@enduml` are optional in a PlantUML block. A block that fails to render raises
an error naming the block index, engine and the renderer's own stderr — a book
never ships with a silently missing diagram.

**Escape `<` and `>` as `&lt;` / `&gt;` inside diagram blocks.** PlantUML and
Mermaid arrow syntax (`-->`, `<|--`, `Map<K,V>`) is full of them and the HTML
parser eats them otherwise. The renderer decodes the entities back before
rendering.

Two engine notes worth knowing:

- **d2** is rasterised from its native SVG export rather than its own PNG
  export, because the latter drives a Playwright/Chromium that d2 downloads at
  run time — it fails in a slim image and would add ~400 MB.
- **Mermaid** is the only engine needing a sidecar. The mermaid *companion* is
  called directly (raw POST to `$MERMAID_URL/png`); the full kroki gateway is
  not used — 2.78 GB against 1.09 GB for the same answer, ~330 MB RSS idle.
  kroki-mermaid renders at CSS-pixel scale, so a default diagram lands ~750 px
  wide and looks thin on e-ink; the server therefore injects
  `%%{init: {"theme":"neutral","themeVariables":{"fontSize":"30px"}} }%%` when
  the author supplied no init directive. Your own `%%{init}%%` or YAML
  frontmatter wins.

### Math

LaTeX is rasterised to black-on-white PNG (Kindle drops MathML silently on
several device generations), which also means equations are *visible* in the
preview.

```html
<pre class="math">\frac{\partial u}{\partial t} = \alpha \nabla^2 u</pre>
<div class="math">E = mc^2</div>            <!-- display -->
<span class="math">\alpha_i</span>          <!-- inline -->
```

`$...$`, `$$...$$`, `\[...\]` and `\(...\)` delimiters are optional and stripped.
Display equations are sized in **em**, so they scale with the reader's body text
instead of with the page's pixel width.

The typesetter is matplotlib's **mathtext**, a TeX *subset* — no LaTeX install
is involved. Supported: `\frac \sqrt \sum \int \lim \left \right \text \mathrm
\mathbb \boldsymbol \operatorname`, accents, Greek, the usual symbols. **Not**
supported: `\displaystyle`, `\begin{...}` environments (align, matrix, cases),
`\\` line breaks, `\label`/`\tag`. Split a multi-line derivation into one math
element per line. A rejected expression comes back with matplotlib's own parse
message.

### Images

Reference every image by **bare filename**: `<img src="fig_arch.png" alt="...">`.
Names resolve against the book's own `assets/` first, then the shared `data/`
folder, so a book can carry its own `cover.jpg` without colliding with anyone
else's.

Every image — generated, downloaded, or rendered from a diagram — funnels
through one normaliser: grayscale, long side capped at 1400 px, PNG for line art
and transparency, else JPEG q85. That is why a Gemini illustration, a fetched
URL and a PlantUML diagram all look the same in the finished book.

- Allowed extensions: `png`, `jpg`, `jpeg`, `gif`, `webp`.
- `data:` URIs work (raster mime types only; `image/svg+xml` is rejected because
  its bytes are not sanitised). Inline `<svg>` is the right way to ship vector art.
- **External `http(s)` `<img src>` is stripped from documents** so a renderer
  that fetches at view time cannot leak the reader's IP. Pull the image into the
  book with `add_images_from_urls` first — including results from the sibling
  `imagine` MCP, whose asset URLs expire.
- Locally, write the file into `./data/` and reference the filename; that is the
  only file channel `send_html_to_kindle` has.

Always ship a cover: it becomes the Kindle library thumbnail and the opening
page. Do not put a date in the title — the attachment is named
`YYMMDD-HHMMSS-<title>.epub` and the server adds the timestamp itself.

---

## Preview and lint

`preview_book` is the feature the remote flow hinges on. Claude on the web
cannot open an EPUB or a PDF, but it *can* receive images returned by a tool. So
the server builds the real EPUB, unzips it, walks `container.xml` -> the OPF ->
the spine in reading order, renders it with WeasyPrint at a 90x120mm page with
6mm margins, rasterises with `pdftoppm`, and returns the pages as inline MCP
image blocks alongside a structural summary and the lint report.

Defaults: `pages="1-4"`, grayscale JPEG, ~1100 px long side (~1.2k tokens per
page), hard cap `PREVIEW_MAX_PAGES=20`. Ask for a range around whatever you just
changed. When the range is clamped, the reply says so explicitly — four pages
back must not read as "the book is four pages long".

**Honest limits.** This is a *content* check, not Kindle's renderer. The page
box is only e-reader-shaped, so pagination and line breaks are approximate;
Kindle Previewer is Windows/Mac only. Read the pages for: did the equations
render, did the diagrams render, are images present and legible, is anything
mangled.

The lint pass covers what pixels hide. Findings are ranked and labelled
BLOCKER / WARNING / NOTE:

| Check | Why it matters |
|---|---|
| Unrendered `<pre class="mermaid\|plantuml\|d2\|graphviz">` blocks | The reader gets raw diagram source. |
| Literal `@startuml` / `@enduml` in the text | A PlantUML block never reached the renderer. |
| Unrendered `class="math"` blocks, and raw `$$…$$` / `\(…\)` / `\[…\]` in running text | The reader gets raw LaTeX. |
| `&amp;lt;` and friends | The HTML was escaped twice; the reader sees a literal `&lt;`. |
| Mojibake markers (`Ã©`, `â€™`, …) and non-UTF-8 chapters | Something was decoded with the wrong codec upstream. |
| `<img>` whose file is not in the EPUB, or with no `src` | Broken placeholder on the device. |
| `<img>` without `alt` | Nothing survives when the image fails to render. |
| Images over 1.5 MB, EPUB over 20 MB | Several of those breach the 25 MB mail cap. |
| Near-empty chapters (<20 chars, no image) | Usually a stray `<h1>` creating a phantom split. |
| Tables wider than 8 columns | Will not fit a 6" screen. |

`send_book` refuses anything over `MAX_SEND_BYTES` (24 MiB) — Gmail hard-caps
the outbound attachment at 25 MB and base64 adds ~33%.

---

## Setup

### 1. Gmail app password

1. Enable 2-Step Verification on the Google account.
2. Go to https://myaccount.google.com/apppasswords and generate one.
3. Put the 16-character value in `.env` as `GMAIL_APP_PASS` (quote it if you
   keep the spaces). It is not your login password, and it is the only Gmail
   credential the server needs — delivery is plain SMTP over STARTTLS to
   `smtp.gmail.com:587`.

### 2. Amazon: approved sender + Send-to-Kindle address

On Amazon -> *Manage Your Content and Devices* -> *Preferences*:

- **Personal Document Settings** -> *Approved Personal Document E-mail List*:
  add the Gmail address you send **from**. Amazon silently drops mail from any
  other sender.
- Copy your device's **Send-to-Kindle e-mail** (`something@kindle.com`) into
  `RECIPIENT_EMAIL`.

Delivery is asynchronous on Amazon's side: the tool returns as soon as SMTP
accepts the message, and the book appears on the device a minute or two later.

### 3. Gemini API key (optional)

Needed only for `generate_images`. Get one at https://aistudio.google.com/apikey
and set `GEMINI_API_KEY`. Without it the server starts fine, `/health` reports
`image_generation: false`, and the tool tells the caller to supply images
another way. Default model is `gemini-3-pro-image-preview` (`"pro"`);
`"flash"` maps to `gemini-3.1-flash-image-preview`.

### 4. `.env`

```bash
cp .env.example .env && chmod 600 .env
```

`.env` is gitignored and **must never be committed** — this is a public
repository and the file holds a Gmail app password, a Gemini key and the bearer
tokens guarding the public endpoint.

---

## Run locally

```bash
./compose.sh                 # docker compose up -d --build
curl -s http://localhost:8018/health | jq
./logs.sh                    # docker compose logs -f -t
```

The MCP endpoint is `http://localhost:8018/kindle/`, published on `127.0.0.1`
only. With `MCP_TOKENS` empty, token auth is disabled — acceptable because
nothing off the host can reach the port. Register it with Claude Code:

```bash
claude mcp add --transport http kindle http://localhost:8018/kindle/
```

`./data` is bind-mounted at `/app/data`, which is what lets the local flow drop
image files in and reference them by filename.

Mermaid is behind a compose profile, since it costs an extra container:

```bash
./compose.sh --mermaid        # starts the sidecar AND sets MERMAID_URL
```

The env var is left commented out on purpose, so `/health` never claims an
engine whose sidecar is not running. PlantUML, Graphviz and D2 need nothing but
the image (~815 MB; Java, Graphviz, plantuml.jar, d2, librsvg, poppler and
WeasyPrint's pango/font stack are all frozen into it).

## Run on a VPS

The VPS deployment publishes no host port: Caddy terminates TLS and reaches the
container over an external Docker network.

```bash
docker network create mcp-shared          # once, shared with the other MCPs
docker compose -f docker-compose.vps.yml up -d --build
```

It sets `MCP_REQUIRE_AUTH=true`, `MCP_ALLOW_URL_TOKENS=true`,
`MCP_ALLOWED_HOSTS` (your public hostname) and `MCP_PUBLIC_BASE_URL`, and runs
the mermaid sidecar unprofiled — the web flow cannot pre-render diagrams
locally, so Mermaid must always be there. `MCP_TOKENS` comes from `.env`:
generate them with `openssl rand -hex 32`.

### Caddy

Inside your site block:

```caddyfile
handle /kindle* {
    reverse_proxy mcp-kindle:8018
}

# Everything else on this host must 404. Caddy's default for a path that
# matches no handler in a site block is an EMPTY 200 — and a claude.ai custom
# connector reads a 200 on /.well-known/oauth-protected-resource as "this
# server has a sign-in service", POSTs a client registration to /register,
# gets an empty body where a JSON client record belongs, and fails with
# "Couldn't register with kindle's sign-in service". A 404 tells it there is
# no OAuth here, so it uses the token already in the connector URL.
handle {
    respond "Not Found" 404
}
```

`handle`, not `handle_path` — the app serves from `/kindle` and expects the
prefix. The upstream `Host` header must appear in `MCP_ALLOWED_HOSTS` or the
MCP's DNS-rebinding protection answers **421 Invalid Host header**; adding the
hostname to that env var is the fix, not a Caddy header rewrite.

If the site block already serves other apps, scope the fallback rather than
adding a bare `handle` — a catch-all 404 changes what every unmatched path on
that hostname returns.

### claude.ai connector

Add a custom connector pointing at:

```
https://<your-host>/kindle/<TOKEN>/
```

The token in the URL path is the point of `MCP_ALLOW_URL_TOKENS`: a claude.ai
custom connector can only carry a secret in the URL. `Authorization: Bearer
<token>` and `?token=<token>` are accepted too. Tokens are redacted from the
access log.

Two route families stay unauthenticated:

| Route | Contents |
|---|---|
| `/health`, `/kindle/health` | Engine readiness, email config, image-gen key, preview availability |
| `/kindle/assets/<book_id>/<file>` | Book images |
| `/kindle/out/<book_id>/<file>` | Preview PDF and page images |

`book_id` carries 12 random hex characters, so the URL *is* the capability.
Nothing else under the book directory is served — `meta.json` and `doc.html` are
not reachable over HTTP.

---

## Configuration

Full annotated list, with defaults: [`.env.example`](.env.example). The ones you
actually touch:

| Variable | Meaning |
|---|---|
| `SENDER_EMAIL`, `GMAIL_APP_PASS`, `RECIPIENT_EMAIL` | Delivery. Required; without them the send tools refuse. |
| `GEMINI_API_KEY` | Enables `generate_images`. |
| `MCP_TOKENS` | Comma-separated bearer tokens. Empty => auth disabled (localhost only). |
| `MCP_REQUIRE_AUTH` | Reject every unauthenticated request. Set `true` on anything public. |
| `MCP_ALLOW_URL_TOKENS` | Accept `?token=` and `/kindle/<token>/`. Needed for claude.ai. |
| `MCP_ALLOWED_HOSTS` | Extra `Host` values accepted by DNS-rebinding protection. |
| `MCP_PUBLIC_BASE_URL` | Public origin used to build asset and preview links. |
| `MERMAID_URL` | Mermaid companion base URL, e.g. `http://kroki-mermaid:8002`. Unset => Mermaid unavailable (`KROKI_URL` works too, for a full kroki gateway). |
| `MCP_NAME` | Service name; drives the URL prefix (`/kindle`) and the MCP server name. |
| `DATA_DIR`, `BOOK_MAX_AGE_DAYS` | Storage root and workspace retention. |
| `MAX_SEND_BYTES`, `MCP_SOFT_TIMEOUT_S`, `PREVIEW_MAX_PAGES` | Mail cap, inline-vs-job threshold, preview page cap. |

Renderer tuning (`DIAGRAM_TIMEOUT_S`, `DIAGRAM_MAX_CONCURRENCY`, `GRAPHVIZ_DPI`,
`D2_PNG_WIDTH`, `MATH_DPI`, `MATH_FONTSIZE`, `MERMAID_INIT`), fetch limits and
preview thresholds all have working defaults and are read straight from the
environment; the ones worth tuning are annotated in `.env.example`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Tools missing in the client; `curl localhost:8018/health` refuses the connection | Container is down or never started | `docker compose ps`, then `./compose.sh`; read `./logs.sh` for the real error (a bad `.env` line is the usual one) |
| Code change has no effect; an old tool signature is still advertised | Stale container — the source is **baked into the image**, not bind-mounted | `docker compose up -d --build` (a plain `restart` reuses the old image) |
| `421 Invalid Host header` on every MCP call, while `/health` stays green | The proxy's upstream `Host` is not in the allowlist; `/health` bypasses the check, so it is not proof | Add the hostname to `MCP_ALLOWED_HOSTS` and recreate the container. Verify with a real `initialize` POST, not `/health` |
| `401 Unauthorized` from claude.ai | Token missing or `MCP_ALLOW_URL_TOKENS=false` | Use `https://<host>/kindle/<TOKEN>/` with the trailing slash; confirm the token is listed in `MCP_TOKENS` |
| claude.ai: "Couldn't register with `<name>`'s sign-in service… add an OAuth Client ID" | Nothing to do with your token. The **host** answers `200` (empty) instead of `404` on `/.well-known/oauth-protected-resource` and `/register`, so the connector believes OAuth exists here and its client registration fails. Caddy returns an empty 200 for any path matching no handler in a site block | Make unmatched paths 404 (see the Caddy block above), reload, and confirm: `curl -o /dev/null -w '%{http_code}' https://<host>/.well-known/oauth-protected-resource` must print `404`. The 404 must not carry a `WWW-Authenticate` header, or discovery restarts |
| "mermaid needs its sidecar; neither MERMAID_URL nor KROKI_URL is set" | Sidecar not running, or MERMAID_URL is empty | Locally: `./compose.sh --mermaid` (it sets the compose profile and `MERMAID_URL` together). Or use `plantuml` / `d2` / `graphviz`, which render in-image |
| "PlantUML render failed (exit N)" / "Mermaid render failed (HTTP 400)" | Diagram syntax error — the message carries the renderer's own stderr | Fix the source. If arrows vanished or the parse error looks nonsensical, you forgot to escape `<`/`>` as `&lt;`/`&gt;` |
| "LaTeX rejected by mathtext" | An unsupported construct (`\displaystyle`, `\begin{align}`, `\\`) | Rewrite with the mathtext subset; one equation per math element |
| `send_book` refuses: "EPUB is N MB but the outbound mail limit is 24 MB" | Too many or too large images | `preview_book` lists the biggest ones; drop some, or lower `image_size` when generating |
| Lint reports unrendered blocks even though preview pages look fine | The block never reached a renderer (wrong class, or nested in a stripped element) | Fix the class, re-`set_document`, preview again |
| Book never arrives, no error from the tool | Sender not on Amazon's approved list, or wrong `@kindle.com` address | Check *Manage Your Content and Devices* -> Preferences -> Personal Document Settings |
| Preview unavailable but sending works | WeasyPrint or poppler-utils missing/broken in the image | `/health` reports `preview: false`; the tool's own error names the missing piece (libpango/libcairo, a font family, `pdftoppm`). Rebuild the image |

---

## Repository layout

```
backend/
  main.py          MCP tool surface, routes, token auth, transport security
  books.py         book workspaces + the one asset normaliser
  html_tools.py    HTML -> sanitise -> render blocks -> embed images -> EPUB
  kindle_tools.py  EPUB -> Gmail SMTP
  diagrams.py      plantuml / graphviz / d2 / mermaid
  mathrender.py    LaTeX -> PNG (matplotlib mathtext)
  imagegen.py      Gemini batch generation
  preview.py       EPUB -> PDF -> page images, plus the lint pass
  fetch.py         SSRF-guarded image download
  jobs.py          soft-timeout background jobs
docker-compose.yml       local, 127.0.0.1:8018
docker-compose.vps.yml   VPS, behind Caddy on the mcp-shared network
.env.example             every environment variable, annotated
```

Build order inside `build_epub_from_html`: sanitize -> diagram blocks -> math
blocks -> embed images -> cover -> split chapters at `<h1>` -> write EPUB.
