# mcp-kindle

MCP server that converts markdown to EPUB and sends it to a Kindle device via Gmail SMTP.

## MCP Tools

Two tools, same Kindle:

- **`send_to_kindle`** — markdown → EPUB → email. Use for prose-style content.
- **`send_html_to_kindle`** — HTML → EPUB → email. Use when you need
  tables with `rowspan`/`colspan`, `<dl>`, `<figure>`, inline `<svg>`,
  `<sup>`/`<sub>`/`<mark>`, or any other layout feature markdown can't
  express.

Both accept an optional `title` (extracted from `# heading` / `<title>` /
first `<h1>` if omitted).

### Images

Both tools embed images via the same shared folder. Image bytes never
travel inside the MCP call (except the HTML tool's `data:` URI channel).

1. Save the image to the absolute host path:
   `/home/alex/projects/mcp-kindle/data/` (bind-mounted to `/app/data` in
   the container).
2. Use the filename convention `{YYYYMMDD_HHMMSS}_{8hex}.{ext}` — e.g.
   `20260512_143022_a1b2c3d4.png`.
3. Reference by bare filename from the document:
   - Markdown: `![alt](20260512_143022_a1b2c3d4.png)`
   - HTML:     `<img src="20260512_143022_a1b2c3d4.png" alt="alt">`

File-based extensions: png, jpg, jpeg, gif, webp.

Additional channels available **only** through `send_html_to_kindle`:

- Inline `data:` URIs (raster only; SVG data URIs are rejected).
- Inline `<svg>...</svg>` in the document body — the right way to embed
  vector diagrams.

Kindles are black-and-white e-ink, so render images in grayscale and
rely on shading, hatching, line style, or labels rather than colour.

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
