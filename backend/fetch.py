"""SSRF-guarded HTTP fetch for remote images.

The web-driven tools let the model hand the server an arbitrary URL, so every
download goes through :func:`assert_fetchable_url` first: the host is resolved
and any non-public address is refused. Without it a token holder could make the
server read cloud metadata (169.254.169.254), localhost, or a sibling container
on the shared Docker network.

Adapted from the imagine MCP server's ``mcp_image_utils`` — same guard, plus a
manual redirect loop so a 302 into a private address can't slip past the check.
"""

from __future__ import annotations

import io
import ipaddress
import logging
import os
import socket
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image as PILImage

logger = logging.getLogger(__name__)

# Cap decoded pixel count to defuse decompression bombs from downloaded images.
PILImage.MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", str(64_000_000)))

# Max bytes pulled for a single download.
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))
# Escape hatch for local testing against localhost / private asset URLs.
ALLOW_PRIVATE_IMAGE_URLS = os.getenv("ALLOW_PRIVATE_IMAGE_URLS", "").lower() in (
    "1",
    "true",
    "yes",
)
MAX_REDIRECTS = int(os.getenv("MAX_DOWNLOAD_REDIRECTS", "5"))

# Some CDNs 403 a bare python-requests UA.
USER_AGENT = os.getenv("FETCH_USER_AGENT", "mcp-kindle/1.0 (+epub-builder)")

# PIL format -> mime, used when sniffing downloaded bytes.
_PIL_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}


# RFC 6598 carrier-grade NAT space. ipaddress does not count it as private, but
# nothing publicly routable lives there — only the ISP's own infrastructure.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in _SHARED_ADDRESS_SPACE)
    )


def assert_fetchable_url(url: str) -> None:
    """Raise ValueError unless ``url`` is http(s) and resolves to public IPs.

    Set ALLOW_PRIVATE_IMAGE_URLS=true to disable the address check (local dev
    only). Note the usual DNS-rebinding caveat: the name is resolved here and
    again by the socket layer, so this is a strong deterrent, not a proof.
    """
    if not url or not url.strip():
        raise ValueError("Empty URL. Pass a full http(s) URL, e.g. https://example.com/x.png")
    if "\x00" in url:
        raise ValueError("URL contains a NUL byte; pass a plain http(s) URL")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Only http/https URLs can be fetched (got scheme {parsed.scheme or 'none'!r} "
            f"in {url[:120]!r}). For local content, store the bytes with the asset tools instead."
        )
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url[:120]!r}. Use the form https://host/path.")
    if ALLOW_PRIVATE_IMAGE_URLS:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(
            f"Could not resolve host {host!r} ({e}). Check the URL spelling, or use a "
            f"different source."
        ) from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(ip):
            raise ValueError(
                f"Refusing to fetch {host!r}: it resolves to the non-public address {ip}. "
                f"Only publicly routable http(s) URLs are allowed."
            )


def sniff_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    """Detect the mime type of raw image bytes via PIL, with a fallback.

    Header sniffing only — never trust a server's Content-Type, which is what
    decides whether we hand these bytes to a decoder.
    """
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            return _PIL_FORMAT_TO_MIME.get(im.format or "", fallback)
    except Exception:
        return fallback


def fetch_bytes(
    url: str,
    timeout: int = 30,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    """Download ``url`` and return ``(data, mime)``; mime is sniffed from bytes.

    Streamed and size-capped against both the declared Content-Length and the
    running total (a lying Content-Length must not buy an unbounded read).
    Redirects are followed manually, re-running the SSRF guard on every hop.
    """
    limit = max_bytes or MAX_DOWNLOAD_BYTES
    if limit <= 0:
        raise ValueError(f"max_bytes must be positive (got {limit})")

    current = url.strip()
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"}

    for _ in range(MAX_REDIRECTS + 1):
        assert_fetchable_url(current)
        with requests.get(
            current,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
            headers=headers,
        ) as response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError(
                        f"{current[:120]!r} returned HTTP {response.status_code} with no "
                        f"Location header; the URL is broken, try another source."
                    )
                current = urljoin(current, location)
                logger.info("fetch: following redirect -> %s", current[:120])
                continue

            response.raise_for_status()

            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ValueError(
                    f"Remote file is too large ({declared} bytes > {limit} limit). "
                    f"Pick a smaller image or a lower-resolution URL."
                )

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        f"Download exceeded the {limit} byte limit. Pick a smaller image "
                        f"or a lower-resolution URL."
                    )
                chunks.append(chunk)

        data = b"".join(chunks)
        if not data:
            raise ValueError(
                f"{current[:120]!r} returned an empty body. Check the URL points directly "
                f"at an image file, not at a web page."
            )
        mime = sniff_mime(data)
        logger.info("fetch: %d bytes, mime=%s from %s", len(data), mime, current[:120])
        return data, mime

    raise ValueError(
        f"Too many redirects (>{MAX_REDIRECTS}) starting at {url[:120]!r}. "
        f"Use the final image URL directly."
    )
