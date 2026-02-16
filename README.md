# mcp-kindle

MCP server that converts markdown to EPUB and sends it to a Kindle device via Gmail SMTP.

## MCP Tool

**`send_to_kindle`** — Takes markdown text, converts it to EPUB, and emails it to your Kindle.

Parameters:
- `markdown_text` (required): Markdown content. Use `{next page}` markers to split into chapters.
- `title` (optional): Book title. If omitted, extracted from the first `# heading`.

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
