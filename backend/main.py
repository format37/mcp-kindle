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


# Markdown endpoint disabled — use send_html_to_kindle instead.
# @mcp.tool()
# def send_to_kindle(markdown_text: str, title: str = "") -> str:
#     """Convert markdown text to EPUB and email it to the user's Kindle.
#
#     This server exposes TWO tools — pick the one that matches your content:
#
#       * ``send_to_kindle``      (this tool)  — markdown input, prose-style.
#       * ``send_html_to_kindle`` (sibling)    — HTML input. Use it instead
#         whenever you need: tables with rowspan/colspan, <figure>, inline
#         <svg>, definition lists, <sup>/<sub>/<mark>, or any other layout
#         feature markdown can't express. If you find yourself thinking
#         "I'll pass raw HTML through this markdown tool" — STOP and call
#         ``send_html_to_kindle`` instead. Both tools embed images the same way.
#
#     Pages
#     -----
#     Use ``{next page}`` markers in the markdown to split content into
#     separate pages / chapters.
#
#     Embedding images — REQUIRED WORKFLOW
#     ------------------------------------
#     This tool does NOT accept image bytes inside the markdown text. You must
#     write the image file to disk first, then reference it by filename.
#
#     Step 1. Save each image file to this EXACT absolute path on the host
#             machine (the same host the agent is running on):
#
#                 /home/alex/projects/mcp-kindle/data/
#
#             (Inside the MCP container this directory is bind-mounted at
#             /app/data — you don't need to use the container path; write to
#             the host path with your normal filesystem tools, e.g. Write or
#             Bash.)
#
#     Step 2. Use this filename convention so files don't collide between
#             agent runs:
#
#                 {YYYYMMDD_HHMMSS}_{8hex}.{ext}
#
#             - YYYYMMDD_HHMMSS — UTC timestamp
#             - 8hex            — 8 lowercase hex chars (e.g. the first 8 of
#                                 uuid.uuid4().hex)
#             - ext             — one of: png, jpg, jpeg, gif, webp
#                                 (SVG is NOT supported here — rasterize to
#                                 PNG before saving)
#
#             Example filename: 20260512_143022_a1b2c3d4.png
#
#     Step 3. Reference the image in markdown by the BARE FILENAME (no path
#             prefix, no absolute path, no URL):
#
#                 ![alt text](20260512_143022_a1b2c3d4.png)
#
#     External http(s):// URLs and arbitrary file paths are NOT fetched and
#     will silently drop. The file MUST exist in the directory above before
#     you call this tool.
#
#     BW rendering
#     ------------
#     The destination Kindle is black-and-white e-ink. Generate images in
#     grayscale; rely on shading, hatching, line style, or labels rather
#     than colour coding.
#
#     Args:
#         markdown_text: The markdown content to convert and send.
#         title: Optional book title. If empty, extracted from the first
#             ``# heading`` in the markdown.
#     """
#     if not SENDER_EMAIL or not GMAIL_APP_PASS or not RECIPIENT_EMAIL:
#         return "Error: missing email configuration (SENDER_EMAIL, GMAIL_APP_PASS, or RECIPIENT_EMAIL)"
#     try:
#         return convert_and_send(
#             markdown_text=markdown_text,
#             title=title,
#             sender_email=SENDER_EMAIL,
#             sender_password=GMAIL_APP_PASS,
#             recipient_email=RECIPIENT_EMAIL,
#         )
#     except Exception as e:
#         return f"Error: {e}"


@mcp.tool()
def send_html_to_kindle(html_text: str, title: str = "") -> str:
    """Convert an HTML document to EPUB and email it to the user's Kindle.

    This server exposes TWO tools — pick the one that matches your content:

      * ``send_html_to_kindle`` (this tool) — HTML input. Use it whenever
        the document needs features markdown can't express:
          - tables with rowspan/colspan/caption/thead/tbody/tfoot
          - definition lists (<dl>/<dt>/<dd>)
          - <figure>/<figcaption>
          - inline <svg> for diagrams (PREFERRED for vector graphics —
            do NOT pass SVG as a data: URI, that's rejected, see below)
          - nested <blockquote>, <sup>, <sub>, <mark>, <u>, <s>
      * ``send_to_kindle`` (sibling) — markdown input, prose-style.

    Chapters
    --------
    The body is split into chapters at every ``<h1>`` element. Anything
    before the first ``<h1>`` becomes a "Preface" chapter. Common semantic
    wrappers (<section>/<article>/<main>) are flattened first, so nested
    <h1>s still produce chapter boundaries.

    Embedding images — TWO CHANNELS
    -------------------------------
    Pick ONE per image:

    CHANNEL A — image file in the shared folder (preferred for reuse /
    larger images):

        Step 1. Save the image to this EXACT absolute path on the host
                machine (the same host the agent is running on):

                    /home/alex/projects/mcp-kindle/data/

                (Inside the MCP container this directory is bind-mounted
                at /app/data — write to the HOST path with your normal
                filesystem tools, e.g. Write or Bash.)

        Step 2. Filename convention so files don't collide between runs:

                    {YYYYMMDD_HHMMSS}_{8hex}.{ext}

                e.g. 20260512_143022_a1b2c3d4.png
                ext one of: png, jpg, jpeg, gif, webp
                (file-based SVG is NOT supported — use Channel C below
                 for vector graphics)

        Step 3. Reference by BARE FILENAME from HTML:

                    <img src="20260512_143022_a1b2c3d4.png" alt="...">

    CHANNEL B — data: URI inline in the HTML (good for one-shot small
    raster images):

        <img src="data:image/png;base64,iVBOR..." alt="...">

        Supported MIME types for data: URIs:
            image/png, image/jpeg, image/gif, image/webp
        ``image/svg+xml`` data URIs are REJECTED. Use Channel C for
        vector graphics.

    CHANNEL C — inline <svg> in the document body (the right way to
    include diagrams / vector graphics):

        <svg xmlns="http://www.w3.org/2000/svg" width="..." height="..."
             viewBox="...">
          ...shapes...
        </svg>

        Inline SVG is sanitised together with the rest of the HTML.

    External http(s):// image URLs are NOT fetched. If an <img> has an
    external src, the src attribute is stripped (so renderers that fetch
    at view time can't leak the reader's IP); the alt text still renders.
    If you have a remote image, download it first and embed via Channel A
    or B.

    BW rendering
    ------------
    The destination Kindle is black-and-white e-ink. Generate images in
    grayscale; rely on shading, hatching, line style, or labels rather
    than colour coding.

    Sanitisation
    ------------
    Removed before conversion: <script>, <iframe>, <embed>, <object>,
    <form>, <input>, <button>, <style>, <link>, <meta>, <base>,
    <noscript>; all on* event-handler attributes; ``javascript:`` hrefs.

    Args:
        html_text: The HTML body (or full document). Fragments are accepted.
        title: Optional book title. If empty, taken from <title>, then the
            first <h1>, falling back to "Untitled".
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
