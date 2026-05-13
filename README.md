# mcp-kindle

MCP server that converts markdown or HTML to EPUB and sends it to a Kindle device via Gmail SMTP.

## MCP Tools

Two tools, same Kindle:

- **`send_to_kindle`** — markdown → EPUB → email. Use for prose-style content.
- **`send_html_to_kindle`** — HTML → EPUB → email. Use when you need
  tables with `rowspan`/`colspan`, `<dl>`, `<figure>`, inline `<svg>`,
  `<sup>`/`<sub>`/`<mark>`, or any other layout feature markdown can't
  express.

Both accept an optional `title` (extracted from `# heading` / `<title>` /
first `<h1>` if omitted).

### The `data/` folder — how images get in

Both tools embed images via a single shared host directory. Image bytes
never travel inside the MCP call (except the HTML tool's optional
`data:` URI channel — see below). The folder is the only file-channel.

**Host path (absolute):** `/home/alex/projects/mcp-kindle/data/`
**Container path:** `/app/data/` (bind-mounted from the host path above
via the `./data:/app/data` volume in [`docker-compose.yml`](docker-compose.yml))

The host path is **hardcoded in both tool docstrings** so an LLM agent
knows exactly where to write image files before calling the MCP. If you
clone this repo to a different location, update the path in both
`send_to_kindle` and `send_html_to_kindle` docstrings in
[`backend/main.py`](backend/main.py) (search for
`/home/alex/projects/mcp-kindle/data/`), then rebuild the container.

**Workflow** for every image:

1. Save the image file to `/home/alex/projects/mcp-kindle/data/`.
2. Use the filename convention `{YYYYMMDD_HHMMSS}_{8hex}.{ext}` — e.g.
   `20260512_143022_a1b2c3d4.png`. The filename is opaque to the server;
   it just needs to be unique to avoid collisions between agent runs.
3. Reference by **bare filename** from the document body (no path
   prefix, no absolute path, no URL):
   - Markdown: `![alt](20260512_143022_a1b2c3d4.png)`
   - HTML:     `<img src="20260512_143022_a1b2c3d4.png" alt="alt">`

Supported file extensions: `png`, `jpg`, `jpeg`, `gif`, `webp`. File-based
SVG is **not** supported — for vector graphics, use inline `<svg>` in the
HTML body (see below). The image resolver is path-traversal-guarded: `../`,
absolute paths outside the data folder, NUL bytes, `http(s)://` URLs,
and missing files all silently drop.

#### Additional image channels (only `send_html_to_kindle`)

- **Inline `data:` URI** — `<img src="data:image/png;base64,...">`.
  Supported MIME types: `image/png`, `image/jpeg`, `image/gif`,
  `image/webp`. `image/svg+xml` data URIs are **rejected** (we don't
  sanitise SVG bytes).
- **Inline `<svg>...</svg>`** in the document body — the right way to
  embed vector diagrams. The SVG is sanitised together with the rest of
  the HTML.
- **Inline PlantUML** — wrap diagram source in
  `<pre class="plantuml">@startuml ... @enduml</pre>`. The server
  renders each block to PNG via `plantuml.jar` and substitutes an
  `<img>` at conversion time, so the agent writes ONE self-contained
  document. Escape `<` / `>` inside the block as `&lt;` / `&gt;`
  (PlantUML uses `->`, `<|--`, generics like `Map<K,V>`). All PlantUML
  diagram families are supported (class, sequence, activity, state,
  use-case, component, deployment, ER, mindmap, gantt). Render failures
  raise an error from the tool — fix the source and resend. For
  sequence diagrams, prefer `note over X : ...` over self-message
  arrows `X -> X` (self-loops render ambiguously).

External `http(s)://` image URLs are not fetched; the `src` is stripped
so renderers that fetch at view time can't leak the reader's IP.

#### Kindle is BW

The destination is a black-and-white e-ink display. Render images in
grayscale and rely on shading, hatching, line style, or labels rather
than colour coding.

## Setup

### 1. Gmail App Password

1. Open https://myaccount.google.com/apppasswords
2. Sign in and generate a new app password
3. Use the generated 16-character password in `.env`

### 2. Environment

Create `.env` in the project root:

```
SENDER_EMAIL=your_email@gmail.com
GMAIL_APP_PASS="your 16char app password"
RECIPIENT_EMAIL=your_kindle_email@kindle.com
MCP_TOKENS=your_secure_token_here
MCP_PUBLIC_BASE_URL=https://your-domain.example.com
MCP_PUBLIC_LINK_TOKEN=your_secure_token_here
```

Note: quote `GMAIL_APP_PASS` if it contains spaces.

### 3. Docker

```bash
./compose.sh
```

Health check: `curl http://localhost:8018/health`

### 4. Caddy (reverse proxy)

Add to your Caddyfile:

```
handle /kindle* {
    reverse_proxy mcp-kindle:8018
}
```

MCP endpoint: `https://your-domain.example.com/kindle/<TOKEN>/`

## Examples

### Send a text file via email
```bash
python examples/send_email.py
```
Edit `examples/send_email.py` to set your sender email, app password, recipient, and attachment path.
