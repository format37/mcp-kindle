"""Gemini "Nano Banana" image generation for the Kindle book pipeline.

Models (Google Gemini image generation, codename "Nano Banana"):
    gemini-3-pro-image-preview     -> Nano Banana Pro (best quality/text)
    gemini-3.1-flash-image-preview -> Nano Banana 2   (fast, high volume)

Unlike the sibling ``imagine`` MCP server this module persists nothing and
returns no MCP content: it hands raw bytes back to the caller
(``books.store_asset()`` owns persistence + grayscale conversion). It also
generates a whole batch of images concurrently — a book needs a cover plus
several illustrations, and from a phone the model cannot afford one
round-trip per image.

Every image here ends up on a monochrome e-ink screen, so :func:`eink_prompt`
appends the style guidance that makes a Gemini image survive that screen.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

try:  # Optional at import time so the server still boots without the SDK.
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - exercised only on a broken image
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    logger.warning("google-genai not installed; image generation disabled")

# Friendly aliases -> concrete model ids (identity keys let callers pass either).
MODEL_ALIASES: dict[str, str] = {
    "pro": "gemini-3-pro-image-preview",
    "nano-banana-pro": "gemini-3-pro-image-preview",
    "gemini-3-pro-image-preview": "gemini-3-pro-image-preview",
    "flash": "gemini-3.1-flash-image-preview",
    "nano-banana": "gemini-3.1-flash-image-preview",
    "nano-banana-2": "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-image-preview": "gemini-3.1-flash-image-preview",
}
_ENV_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "").strip()
# Resolved through the alias map here too: GEMINI_IMAGE_MODEL="flash" must not
# reach the API verbatim as an unknown model id.
DEFAULT_MODEL: str = MODEL_ALIASES.get(_ENV_MODEL.lower(), _ENV_MODEL) or "gemini-3-pro-image-preview"

VALID_ASPECT: set[str] = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
VALID_SIZE: set[str] = {"1K", "2K", "4K"}

# Gemini SDK timeout in milliseconds (a 2K generation takes 15-40s; Pro more).
GENAI_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_MS", "300000"))
# Concurrent generations per batch — bounds Gemini quota burn and 429s.
MAX_CONCURRENCY = max(1, int(os.getenv("IMAGE_MAX_CONCURRENCY", "3")))
# Hard cap on one batch, so a confused caller can't fire 200 generations.
MAX_BATCH = max(1, int(os.getenv("IMAGE_MAX_BATCH", "12")))

# A single long-lived client is reused across requests and threads. It MUST be
# held in a stable module-level reference: a genai.Client created inline and
# garbage-collected mid-call runs its finalizer, which closes the underlying
# httpx transport, and the in-flight request dies with "Cannot send a request,
# as the client has been closed".
_client_lock = threading.Lock()
_cached_client = None

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

GRAYSCALE_SUFFIX = (
    " Render in pure black-and-white grayscale, no colour at all. Very high contrast: "
    "deep blacks against clean white, bold simple shapes, thick confident outlines, "
    "no subtle mid-grey gradients, no fine texture — it must stay readable on a "
    "low-contrast 6-inch e-ink screen."
)

_KIND_SUFFIX: dict[str, str] = {
    "illustration": (
        " Style: pen-and-ink drawing / woodcut engraving, hatching and cross-hatching "
        "for shading, generous white space."
    ),
    "cover": (
        " Compose it as a book cover: one bold central subject on a plain white "
        "background, strong silhouette, generous margins. Absolutely NO text, "
        "letters, words, numbers, logos or watermarks anywhere in the image — the "
        "book renders its own title."
    ),
    "diagram": (
        " Style: clean schematic line drawing, uniform stroke weight, flat shapes, "
        "no shading, no perspective, no decorative detail."
    ),
}

# Markers meaning "the caller already asked for monochrome" — don't repeat ourselves.
_MONO_MARKERS = ("black-and-white", "black and white", "grayscale", "greyscale")


def _sdk_problem() -> str | None:
    """Why generation cannot run right now, or None when it can.

    The ImageConfig probe is not paranoia: google-genai < 2.x imports fine and
    has HttpOptions, so every other check passes, and the call then dies deep
    inside generate_one with an AttributeError.
    """
    if genai is None:
        return (
            "the google-genai SDK is not installed in this image (add google-genai"
            "==2.8.0 to requirements.txt and rebuild)"
        )
    if not hasattr(types, "ImageConfig"):
        return (
            "the installed google-genai is too old — it has no types.ImageConfig "
            "(pin google-genai==2.8.0 and rebuild)"
        )
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return "GEMINI_API_KEY is not configured on the server"
    return None


def available() -> bool:
    """True when image generation can actually run (SDK usable + key configured)."""
    return _sdk_problem() is None


def _require_sdk() -> None:
    problem = _sdk_problem()
    if problem:
        raise RuntimeError(
            f"Image generation is unavailable: {problem}. Supply the image yourself "
            "instead of generating one."
        )


def eink_prompt(prompt: str, kind: str = "illustration") -> str:
    """Append e-ink style guidance to a caller's prompt.

    ``kind`` selects the extra art direction: ``"illustration"`` (ink/engraving,
    default), ``"cover"`` (bold central subject on white, no text), or
    ``"diagram"`` (flat schematic). An unknown kind falls back to
    ``"illustration"`` rather than failing — a bad label must not cost a batch.
    The grayscale block is skipped when the prompt already asks for monochrome;
    the kind-specific guidance is always appended, since it says more than
    "no colour".
    """
    base = (prompt or "").strip()
    if not base:
        raise ValueError("eink_prompt() got an empty prompt; describe the image to draw.")

    key = (kind or "illustration").strip().lower()
    if key not in _KIND_SUFFIX:
        logger.warning(
            "Unknown eink_prompt kind %r; using 'illustration'. Valid: %s",
            kind, sorted(_KIND_SUFFIX),
        )
        key = "illustration"

    lowered = base.lower()
    out = base
    if not any(m in lowered for m in _MONO_MARKERS):
        out += GRAYSCALE_SUFFIX
    return out + _KIND_SUFFIX[key]


def _resolve_model(model: str | None) -> str:
    if not model or not model.strip():
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(model.strip().lower(), model.strip())


def _client():
    global _cached_client
    if _cached_client is None:
        with _client_lock:
            if _cached_client is None:
                _require_sdk()
                _cached_client = genai.Client(
                    api_key=os.environ["GEMINI_API_KEY"].strip(),
                    http_options=types.HttpOptions(timeout=GENAI_TIMEOUT_MS),
                )
    return _cached_client


def _validate(aspect_ratio: str, image_size: str) -> None:
    if aspect_ratio not in VALID_ASPECT:
        raise ValueError(
            f"Invalid aspect_ratio {aspect_ratio!r}. Use one of {sorted(VALID_ASPECT)} "
            "(portrait '3:4' suits a Kindle page, '2:3' a cover)."
        )
    if image_size not in VALID_SIZE:
        raise ValueError(
            f"Invalid image_size {image_size!r}. Use one of {sorted(VALID_SIZE)} "
            "('1K' is plenty for e-ink; '2K'/'4K' only slow the book down)."
        )


def _sniff_mime(data: bytes, fallback: str = "image/png") -> str:
    """Best-effort mime from magic bytes; used when the model omits mime_type."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def generate_one(
    prompt: str,
    aspect_ratio: str = "3:4",
    image_size: str = "1K",
    model: str | None = None,
) -> tuple[bytes, str, str | None]:
    """Generate one image. Returns ``(image_bytes, mime, model_note)``.

    ``model_note`` is any text the model emitted alongside the image (often a
    caveat), or None. Raises ValueError for bad parameters and RuntimeError when
    no image comes back — the message carries the model's finish_reason and text,
    which is normally a safety-filter block and tells the caller how to reword.
    """
    text_prompt = (prompt or "").strip()
    if not text_prompt:
        raise ValueError("Empty prompt; describe the image to generate.")
    _validate(aspect_ratio, image_size)
    # Before touching types.*: a missing/old SDK must surface as an actionable
    # RuntimeError, not an AttributeError on the None placeholder.
    _require_sdk()
    model_id = _resolve_model(model)

    config = types.GenerateContentConfig(
        response_modalities=["IMAGE", "TEXT"],
        image_config=types.ImageConfig(aspect_ratio=aspect_ratio, image_size=image_size),
    )
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=text_prompt)])]

    logger.info(
        "Generating image: model=%s aspect=%s size=%s prompt=%r",
        model_id, aspect_ratio, image_size, text_prompt[:120],
    )
    client = _client()  # strong reference for the whole call (see _cached_client)
    response = client.models.generate_content(
        model=model_id, contents=contents, config=config
    )

    image_bytes: bytes | None = None
    mime: str | None = None
    note: str | None = None
    finish_reason = None

    candidates = response.candidates or []
    if candidates:
        cand = candidates[0]
        finish_reason = getattr(cand, "finish_reason", None)
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                image_bytes = inline.data
                mime = inline.mime_type
            elif getattr(part, "text", None):
                note = (note or "") + part.text

    if not image_bytes:
        msg = (
            f"The model returned no image (finish_reason={finish_reason}). This usually "
            "means safety filters blocked the prompt, or the prompt read as a request for "
            "text. Rephrase it as a concrete visual scene and retry."
        )
        if note and note.strip():
            msg += f" Model said: {note.strip()[:400]}"
        raise RuntimeError(msg)

    mime = (mime or "").split(";")[0].strip().lower() or _sniff_mime(image_bytes)
    note = note.strip() if note and note.strip() else None
    logger.info("Generated %d bytes (%s) with %s", len(image_bytes), mime, model_id)
    return image_bytes, mime, note


def generate_many(
    prompts: list[str],
    aspect_ratio: str = "3:4",
    image_size: str = "1K",
    model: str | None = None,
    max_workers: int | None = None,
) -> list[dict]:
    """Generate several images concurrently. Order-preserving, failure-tolerant.

    One bad prompt must never sink the batch, so every entry is reported
    independently::

        {"index": int, "prompt": str, "ok": bool, "data": bytes | None,
         "mime": str | None, "error": str | None, "note": str | None}

    Concurrency defaults to ``IMAGE_MAX_CONCURRENCY`` (3) and is clamped to the
    batch length. Parameter errors that apply to the whole call (bad
    aspect_ratio/image_size, oversized batch) raise instead of being reported
    N times.
    """
    if prompts is None or isinstance(prompts, (str, bytes)):
        raise ValueError("prompts must be a list of prompt strings, not a single string.")
    items = list(prompts)
    if not items:
        return []
    if len(items) > MAX_BATCH:
        raise ValueError(
            f"Too many prompts ({len(items)}); max {MAX_BATCH} per call. Split the batch "
            "or generate fewer illustrations."
        )
    _validate(aspect_ratio, image_size)
    # Fail once rather than reporting the same server misconfiguration N times.
    _require_sdk()

    workers = MAX_CONCURRENCY if max_workers is None else max_workers
    workers = max(1, min(int(workers), len(items), MAX_CONCURRENCY * 4))

    def _one(index: int, raw: object) -> dict:
        text = raw.strip() if isinstance(raw, str) else ""
        result: dict = {
            "index": index, "prompt": text, "ok": False,
            "data": None, "mime": None, "error": None, "note": None,
        }
        if not text:
            result["error"] = (
                f"prompts[{index}] is empty or not a string; every entry must describe "
                "one image."
            )
            return result
        try:
            data, mime, note = generate_one(text, aspect_ratio, image_size, model)
        except Exception as e:
            logger.warning("Image %d failed: %s", index, e)
            result["error"] = f"{type(e).__name__}: {e}"
            return result
        result.update(ok=True, data=data, mime=mime, note=note)
        return result

    logger.info("Generating %d image(s) with %d worker(s)", len(items), workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imagegen") as pool:
        # Submit order is preserved by list(map(...)); each future is independent.
        futures = [pool.submit(_one, i, p) for i, p in enumerate(items)]
        results = [f.result() for f in futures]

    ok = sum(1 for r in results if r["ok"])
    logger.info("Batch done: %d/%d generated", ok, len(results))
    return results
