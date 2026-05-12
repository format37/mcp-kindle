import contextlib
import contextvars
import logging
import os
import re

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from kindle_tools import convert_and_send
from html_tools import convert_html_and_send

load_dotenv(".env")

logger = logging.getLogger(__name__)

# Config from environment
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")

MCP_NAME = os.getenv("MCP_NAME", "kindle")
_safe_name = re.sub(r"[^a-z0-9_-]", "-", MCP_NAME.lower()).strip("-") or "service"
BASE_PATH = f"/{_safe_name}"
STREAM_PATH = f"{BASE_PATH}/"

MCP_TOKEN_CTX = contextvars.ContextVar("mcp_token", default=None)

# Transport security: allow reverse proxy and Docker hostnames
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "localhost:*",
        "127.0.0.1:*",
        "mcp-kindle:*",
        "scriptlab.duckdns.org:*",
        "scriptlab.duckdns.org",
    ],
    allowed_origins=[
        "http://localhost:*",
        "https://localhost:*",
        "http://127.0.0.1:*",
        "http://mcp-kindle:*",
        "https://scriptlab.duckdns.org:*",
        "https://scriptlab.duckdns.org",
    ],
)

mcp = FastMCP(
    _safe_name,
    streamable_http_path=STREAM_PATH,
    json_response=True,
    transport_security=transport_security,
)


@mcp.tool()
def send_to_kindle(markdown_text: str, title: str = "") -> str:
    """Convert markdown text to EPUB and send it to a Kindle device via email.

    Use {next page} markers in the markdown to split content into separate pages/chapters.

    Images
    ------
    The markdown may reference images and they will be embedded into the EPUB.
    Before calling this tool, write each image to the host-mounted ``data/``
    folder (mapped to ``/app/data`` inside the container) using this filename
    convention:

        ``{YYYYMMDD_HHMMSS}_{8hex}.{ext}``

    where ``YYYYMMDD_HHMMSS`` is a UTC timestamp and ``8hex`` is 8 lowercase
    hex chars (e.g. the first 8 of ``uuid.uuid4().hex``) for uniqueness. The
    filename is treated as an opaque identifier; the server does not parse
    or validate it. Example: ``20260512_143022_a1b2c3d4.png``. Supported
    extensions: png, jpg, jpeg, gif, webp. Reference images from markdown
    with the bare filename (or a ``data/`` prefix), e.g.::

        ![chart](20260512_143022_a1b2c3d4.png)

    The destination Kindle is black-and-white (grayscale e-ink). When you
    generate images, render them in grayscale / monochrome and choose colors
    and contrast that read clearly on a BW e-ink display (avoid pure color
    coding; rely on shading, hatching, line style, or labels).

    Args:
        markdown_text: The markdown content to convert and send.
        title: Optional title for the book. If empty, extracted from the first # heading.
    """
    if not SENDER_EMAIL or not GMAIL_APP_PASS or not RECIPIENT_EMAIL:
        return "Error: missing email configuration (SENDER_EMAIL, GMAIL_APP_PASS, or RECIPIENT_EMAIL)"
    try:
        return convert_and_send(
            markdown_text=markdown_text,
            title=title,
            sender_email=SENDER_EMAIL,
            sender_password=GMAIL_APP_PASS,
            recipient_email=RECIPIENT_EMAIL,
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def send_html_to_kindle(html_text: str, title: str = "") -> str:
    """Convert an HTML document to EPUB and send it to the Kindle.

    HTML is a richer authoring format than markdown: native support for
    tables with ``rowspan`` / ``colspan`` and ``<caption>``, definition
    lists (``<dl>``/``<dt>``/``<dd>``), nested ``<blockquote>``,
    ``<figure>``/``<figcaption>``, inline ``<svg>``, and arbitrary
    inline markup (``<sup>``, ``<sub>``, ``<mark>``, ``<u>``, ``<s>``).
    Use this tool when the document is layout-rich; otherwise prefer the
    markdown tool.

    Chapters
    --------
    The body is split into chapters at every ``<h1>`` element. Anything
    before the first ``<h1>`` becomes a "Preface" chapter.

    Images
    ------
    Two image sources are accepted (same destination — embedded into the
    EPUB, no outbound network calls):

    1. ``data:`` URIs (``<img src="data:image/png;base64,...">``) — decoded
       and embedded inline. Useful for very small or programmatically
       generated images.
    2. Files placed in the host-mounted ``data/`` folder (``/app/data``
       inside the container). Filename convention:
       ``{YYYYMMDD_HHMMSS}_{8hex}.{ext}`` (UTC timestamp, 8 lowercase hex
       chars, e.g. ``20260512_143022_a1b2c3d4.png``). Reference from HTML
       with the bare filename or a ``data/`` prefix:
       ``<img src="20260512_143022_a1b2c3d4.png">``.
       Supported extensions: png, jpg, jpeg, gif, webp.

    External ``http(s)://`` image URLs are *not* fetched — embed them as a
    file in ``data/`` first.

    The destination Kindle is black-and-white (grayscale e-ink). Render
    images in grayscale / monochrome and rely on shading, hatching, line
    style, or labels rather than color coding.

    Sanitization
    ------------
    ``<script>``, ``<iframe>``, ``<embed>``, ``<object>``, ``<form>``,
    ``<style>``, ``on*`` event-handler attributes and ``javascript:``
    URLs are stripped before conversion.

    Args:
        html_text: The HTML body (or full document). Fragments are accepted.
        title: Optional book title. If empty, taken from ``<title>`` then
            the first ``<h1>``, falling back to "Untitled".
    """
    if not SENDER_EMAIL or not GMAIL_APP_PASS or not RECIPIENT_EMAIL:
        return "Error: missing email configuration (SENDER_EMAIL, GMAIL_APP_PASS, or RECIPIENT_EMAIL)"
    try:
        return convert_html_and_send(
            html_text=html_text,
            title=title,
            sender_email=SENDER_EMAIL,
            sender_password=GMAIL_APP_PASS,
            recipient_email=RECIPIENT_EMAIL,
        )
    except Exception as e:
        return f"Error: {e}"


# Build ASGI app
mcp_asgi = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield


async def health_check(request):
    return JSONResponse({"status": "healthy"})


app = Starlette(
    routes=[
        Route("/health", health_check, methods=["GET"]),
        Mount("/", app=mcp_asgi),
    ],
    lifespan=lifespan,
)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Token gate for requests under BASE_PATH.

    Accepts tokens via:
    - Authorization header: "Bearer <token>"
    - URL path: /<service>/<token>/...

    If MCP_TOKENS is unset, auth is disabled (allows all).
    """

    def __init__(self, app):
        super().__init__(app)
        raw = os.getenv("MCP_TOKENS", "")
        self.allowed_tokens = {t.strip() for t in raw.split(",") if t.strip()}
        self.allow_url_tokens = True
        self.require_auth = (
            os.getenv("MCP_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
        )
        if not self.allowed_tokens:
            if self.require_auth:
                logger.warning(
                    "MCP_TOKENS not set; MCP_REQUIRE_AUTH=true -> all %s requests rejected",
                    BASE_PATH,
                )
            else:
                logger.warning(
                    "MCP_TOKENS not set; token auth DISABLED for %s", BASE_PATH
                )

    async def dispatch(self, request, call_next):
        path = request.url.path or "/"
        if not path.startswith(BASE_PATH):
            return await call_next(request)

        def accept(token_value, source):
            request.state.mcp_token = token_value
            logger.info("Authenticated %s %s via %s", request.method, path, source)
            return MCP_TOKEN_CTX.set(token_value)

        async def proceed(token_value, source):
            token_scope = accept(token_value, source)
            try:
                return await call_next(request)
            finally:
                MCP_TOKEN_CTX.reset(token_scope)

        # If auth not required, strip token-like path segment and allow
        if not self.require_auth:
            segs = [s for s in path.split("/") if s != ""]
            if len(segs) >= 2 and segs[0] == _safe_name:
                remainder = "/".join([_safe_name] + segs[2:])
                new_path = "/" + (
                    remainder + "/"
                    if path.endswith("/") or not segs[2:]
                    else remainder
                )
                if new_path == BASE_PATH:
                    new_path = STREAM_PATH
                request.scope["path"] = new_path
                if "raw_path" in request.scope:
                    request.scope["raw_path"] = new_path.encode("utf-8")
                logger.info("Auth disabled, rewriting path %s -> %s", path, new_path)
            else:
                logger.info("Auth disabled, allowing request to %s", path)
            return await call_next(request)

        if not self.allowed_tokens:
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Authorization: Bearer <token>
        token = None
        auth = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

        if token and token in self.allowed_tokens:
            return await proceed(token, "header")

        if self.allow_url_tokens:
            # Query parameter ?token=...
            url_token = request.query_params.get("token")
            if url_token and url_token in self.allowed_tokens:
                return await proceed(url_token, "query")

            # Path segment /<service>/<token>/...
            segs = [s for s in path.split("/") if s != ""]
            if len(segs) >= 2 and segs[0] == _safe_name:
                candidate = segs[1]
                if candidate in self.allowed_tokens:
                    remainder = "/".join([_safe_name] + segs[2:])
                    new_path = "/" + (
                        remainder + "/"
                        if path.endswith("/") and not remainder.endswith("/")
                        else remainder
                    )
                    if new_path == BASE_PATH:
                        new_path = STREAM_PATH
                    request.scope["path"] = new_path
                    if "raw_path" in request.scope:
                        request.scope["raw_path"] = new_path.encode("utf-8")
                    return await proceed(candidate, "path")

        return JSONResponse(
            {"detail": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )


app.add_middleware(TokenAuthMiddleware)


def main():
    PORT = int(os.getenv("PORT", "8018"))
    logger.info(f"Starting {MCP_NAME} MCP server on port {PORT} at {STREAM_PATH}")
    uvicorn.run(
        app=app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=PORT,
        log_level=os.getenv("LOG_LEVEL", "info"),
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()
