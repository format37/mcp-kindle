"""Render inline diagram blocks to images — one abstraction, several engines.

The agent embeds diagram source directly in its HTML inside a
``<pre class="mermaid">`` (or plantuml / d2 / graphviz) element; this module
renders each block and swaps in an ``<img>``. Where those bytes end up is the
caller's business — :func:`process_diagram_blocks` takes a ``sink`` callable so
the same pass can produce a ``data:`` URI (local pipeline) or a stored book
asset (web pipeline).

Engines::

    plantuml  local   java -jar $PLANTUML_JAR -tpng -nometadata -pipe
    graphviz  local   dot -Tpng                          (alias class: dot)
    d2        local   d2 ... in.d2 out.svg | rsvg-convert -w N -o out.png
    mermaid   remote  POST source to $MERMAID_URL/png (kroki mermaid companion)
                      or $KROKI_URL/mermaid/png (full kroki gateway)

PNG is the default output because the destination is a black-and-white e-ink
Kindle whose converter handles inline SVG unreliably — and because Mermaid's
default ``htmlLabels`` put label text inside ``<foreignObject>``, which several
EPUB readers drop, leaving an empty diagram. Asking kroki for PNG sidesteps it.

Run order in the pipeline:
    sanitize -> process_diagram_blocks -> embed_images -> split_chapters

Requires (provided by the Docker image):
    java + plantuml.jar   path via ``$PLANTUML_JAR`` (default /opt/plantuml.jar)
    graphviz              ``dot`` on PATH
    d2 + rsvg-convert     both on PATH (d2's own PNG export needs a browser)
    mermaid sidecar       base URL via ``$MERMAID_URL`` / ``$KROKI_URL``
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup, Tag

try:  # only mermaid needs HTTP; local engines must work without it
    import requests
except ImportError:  # pragma: no cover - depends on the image
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class DiagramError(RuntimeError):
    """A diagram failed to render.

    Messages are written for the LLM that authored the diagram: they say what
    was wrong and what to do instead, and carry the renderer's own stderr.
    """


ENGINES: tuple[str, ...] = ("plantuml", "graphviz", "d2", "mermaid")

# css class on a <pre> -> engine name. Aliases included: agents reach for the
# language name they know ("dot", "uml") as often as the engine name.
BLOCK_CLASSES: dict[str, str] = {
    "plantuml": "plantuml",
    "uml": "plantuml",
    "puml": "plantuml",
    "graphviz": "graphviz",
    "dot": "graphviz",
    "d2": "d2",
    "mermaid": "mermaid",
}

ENGINE_LABELS: dict[str, str] = {
    "plantuml": "PlantUML",
    "graphviz": "Graphviz",
    "d2": "D2",
    "mermaid": "Mermaid",
}

FORMATS: tuple[str, ...] = ("png", "svg")

PLANTUML_JAR = os.environ.get("PLANTUML_JAR", "/opt/plantuml.jar")

# Guards against a single pathological block. Source far past this is a pasted
# document, not a diagram.
MAX_SOURCE_CHARS = 200_000
_STDERR_CHARS = 500

# Any @startXXX opener (uml, mindmap, gantt, json, salt, ...) — used to decide
# whether the block still needs wrapping.
_PLANTUML_START_RE = re.compile(r"^\s*@start\w+", re.MULTILINE)

# Magic bytes we accept back from a renderer, per format.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# `d2` picks the output format from the output filename; it has no PNG-to-stdout
# mode. Flags: fixed light theme, dark theme disabled, small pad — an e-ink
# page has no dark mode and no room for d2's default 100px border.
D2_FLAGS: tuple[str, ...] = ("--theme=0", "--dark-theme=-1", "--pad=20")

Sink = Callable[[bytes, str], str]


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


def _timeout_s() -> int:
    """Render timeout. ``PLANTUML_TIMEOUT`` still honoured for old deployments."""
    return _env_int("DIAGRAM_TIMEOUT_S", _env_int("PLANTUML_TIMEOUT", 60))


def _kroki_url() -> str:
    """Base URL of the self-hosted kroki sidecar; empty when not configured."""
    return (os.environ.get("KROKI_URL") or "").strip().rstrip("/")


def _mermaid_endpoint(fmt: str) -> str:
    """Full URL to POST mermaid source to; empty string when unconfigured.

    Two shapes are supported. MERMAID_URL points straight at kroki's mermaid
    *companion* (``POST /<fmt>``) — that is what we deploy, because the
    companion alone is a 1 GB image against 2.78 GB for the full kroki gateway
    it would otherwise sit behind. KROKI_URL points at a complete kroki server
    (``POST /mermaid/<fmt>``) for anyone who already runs one.
    """
    direct = (os.environ.get("MERMAID_URL") or "").strip().rstrip("/")
    if direct:
        return f"{direct}/{fmt}"
    base = _kroki_url()
    return f"{base}/mermaid/{fmt}" if base else ""


# kroki-mermaid renders at CSS-pixel scale, so a default-styled diagram lands
# around 750 px wide — thin and grey on a 6" e-ink page. Bumping mermaid's own
# font size scales the whole layout up (~900x1500 for a real diagram) at render
# time, which beats upscaling a small PNG afterwards.
MERMAID_INIT = os.environ.get(
    "MERMAID_INIT",
    '%%{init: {"theme":"neutral","themeVariables":{"fontSize":"30px"}} }%%',
)


def _with_mermaid_init(source: str) -> str:
    """Prepend the default init directive unless the author supplied their own."""
    if not MERMAID_INIT or "%%{init" in source or source.lstrip().startswith("---"):
        return source
    return f"{MERMAID_INIT}\n{source.lstrip()}"


# Renders are CPU/memory heavy (a JVM per PlantUML block). Cap how many run at
# once so several concurrent books can't OOM a small VPS.
_render_semaphore = threading.Semaphore(max(1, _env_int("DIAGRAM_MAX_CONCURRENCY", 2)))


def available_engines() -> dict[str, bool]:
    """Cheap probe of each engine's prerequisites — no diagram is rendered."""
    return {
        "plantuml": bool(shutil.which("java")) and Path(PLANTUML_JAR).is_file(),
        "graphviz": bool(shutil.which("dot")),
        "d2": bool(shutil.which("d2")) and bool(shutil.which("rsvg-convert")),
        "mermaid": bool(_mermaid_endpoint("png")),
    }


def _available_summary() -> str:
    ready = [name for name, ok in available_engines().items() if ok]
    return ", ".join(ready) if ready else "none"


def _trim(raw: bytes | str | None) -> str:
    """Decode + squeeze a renderer's diagnostics down to something loggable."""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return text.strip()[:_STDERR_CHARS]


def _stderr_detail(result: subprocess.CompletedProcess) -> str:
    """PlantUML reports some parse errors on stdout, so fall back to it when
    stderr is empty and stdout is clearly not image bytes."""
    detail = _trim(result.stderr)
    if detail:
        return detail
    out = result.stdout or b""
    if out and not out.startswith((_PNG_MAGIC, b"GIF8", b"\xff\xd8")):
        return _trim(out) or "no output"
    return "no stderr"


def _resolve_engine(engine: str) -> str:
    key = (engine or "").strip().lower()
    name = BLOCK_CLASSES.get(key, key)
    if name not in ENGINES:
        aliases = ", ".join(
            f"{a} -> {e}" for a, e in sorted(BLOCK_CLASSES.items()) if a != e
        )
        raise DiagramError(
            f"Unknown diagram engine {engine!r}. Valid engines: {', '.join(ENGINES)} "
            f"(aliases: {aliases}). Ready on this server: {_available_summary()}."
        )
    return name


def _clean_source(engine: str, source: str) -> str:
    label = ENGINE_LABELS[engine]
    if not source or not source.strip():
        raise DiagramError(
            f"Empty {label} source — put the diagram source inside the block."
        )
    if "\x00" in source:
        raise DiagramError(f"{label} source contains a NUL byte; remove it.")
    if len(source) > MAX_SOURCE_CHARS:
        raise DiagramError(
            f"{label} source is {len(source)} chars, over the {MAX_SOURCE_CHARS} "
            "limit. Split it into several smaller diagrams."
        )
    # A <pre> carries the author's HTML indentation verbatim; strip the common
    # prefix so engines that care about column 0 see clean source.
    return textwrap.dedent(source).strip("\n")


def _assert_image(engine: str, fmt: str, data: bytes) -> None:
    """Honest check that we got the format we asked for, not an error page."""
    label = ENGINE_LABELS[engine]
    if not data:
        raise DiagramError(f"{label} produced no output; check the diagram source.")
    if fmt == "png" and not data.startswith(_PNG_MAGIC):
        raise DiagramError(
            f"{label} returned data that is not a PNG: {_trim(data[:_STDERR_CHARS])}"
        )
    if fmt == "svg" and b"<svg" not in data[:4096]:
        raise DiagramError(
            f"{label} returned data that is not an SVG: {_trim(data[:_STDERR_CHARS])}"
        )


def _run(
    cmd: list[str],
    engine: str,
    stdin: bytes | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a renderer, converting every failure mode into a DiagramError."""
    label = ENGINE_LABELS[engine]
    timeout = _timeout_s()
    try:
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError as e:
        raise DiagramError(
            f"{label} renderer {cmd[0]!r} is not installed in the server image. "
            f"Engines ready here: {_available_summary()} — use one of those instead."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise DiagramError(
            f"{label} render timed out after {timeout}s. Simplify the diagram or "
            "split it into several blocks."
        ) from e
    if result.returncode != 0:
        raise DiagramError(
            f"{label} render failed (exit {result.returncode}): {_stderr_detail(result)}"
        )
    return result


def _wrap_plantuml(source: str) -> str:
    """``@startuml``/``@enduml`` are optional in a block.

    Detect ANY ``@startXXX`` opener, not just ``@startuml``: wrapping a
    ``@startmindmap`` diagram into ``@startuml`` would break it.
    """
    if _PLANTUML_START_RE.search(source):
        return source
    return f"@startuml\n{source.strip()}\n@enduml\n"


def render_plantuml(source: str, fmt: str = "png", jar: str | None = None) -> bytes:
    """Run ``plantuml.jar`` in pipe mode; return the rendered image bytes.

    On a syntax error ``-pipe`` still writes an "error" image to stdout but
    exits 200, so the non-zero check in :func:`_run` is what stops that image
    from silently shipping inside the book.
    """
    jar_path = jar or PLANTUML_JAR
    if not Path(jar_path).is_file():
        raise DiagramError(
            f"plantuml.jar not found at {jar_path} (server misconfigured: set "
            f"PLANTUML_JAR). Engines ready here: {_available_summary()}."
        )
    result = _run(
        ["java", "-jar", jar_path, f"-t{fmt}", "-nometadata", "-pipe"],
        "plantuml",
        stdin=_wrap_plantuml(source).encode("utf-8"),
    )
    # Checked here, not only in render(), because plantuml_tools calls straight
    # into this: an exit-0 run with empty stdout must not return b"".
    _assert_image("plantuml", fmt, result.stdout)
    return result.stdout


def _render_graphviz(source: str, fmt: str) -> bytes:
    cmd = ["dot", f"-T{fmt}"]
    if fmt == "png":
        # -G sets a DEFAULT graph attribute, so `dpi=` inside the source still
        # wins. Graphviz's 96dpi default is mushy on a 300dpi e-ink screen.
        # Via _env_int because dot silently ignores a bad dpi= and ships a
        # blurry image instead of failing.
        cmd.append(f"-Gdpi={_env_int('GRAPHVIZ_DPI', 150)}")
    return _run(cmd, "graphviz", stdin=source.encode("utf-8")).stdout


def _render_d2(source: str, fmt: str) -> bytes:
    """Render d2 via its native SVG export, rasterising with librsvg when asked.

    d2's own PNG export drives a headless Chromium it downloads at run time —
    which fails in a slim container and would add ~400 MB to the image. Its SVG
    export is native and emits real <text> elements (no <foreignObject>), so
    rsvg-convert rasterises it faithfully at whatever width we want.
    """
    with tempfile.TemporaryDirectory(prefix="d2-") as tmp:
        src = Path(tmp) / "diagram.d2"
        svg = Path(tmp) / "diagram.svg"
        src.write_text(source, encoding="utf-8")
        result = _run(["d2", *D2_FLAGS, str(src), str(svg)], "d2", cwd=tmp)
        if not svg.is_file():
            raise DiagramError(f"D2 wrote no SVG file: {_stderr_detail(result)}")
        if fmt == "svg":
            return svg.read_bytes()

        out = Path(tmp) / "diagram.png"
        width = _env_int("D2_PNG_WIDTH", 1200)
        # _run already turns a missing binary into an actionable DiagramError
        # naming rsvg-convert and the engines that ARE ready.
        rast = _run(
            ["rsvg-convert", "-w", str(width), str(svg), "-o", str(out)], "d2", cwd=tmp
        )
        if not out.is_file():
            raise DiagramError(
                f"rsvg-convert wrote no PNG for the d2 diagram: {_stderr_detail(rast)}"
            )
        return out.read_bytes()


def _render_mermaid(source: str, fmt: str) -> bytes:
    url = _mermaid_endpoint(fmt)
    if not url:
        raise DiagramError(
            "mermaid needs its sidecar; neither MERMAID_URL nor KROKI_URL is set on "
            "this server. Use plantuml, d2 or graphviz instead — they render locally."
        )
    if not url.startswith(("http://", "https://")):
        raise DiagramError(
            f"The configured mermaid endpoint {url!r} is not an http(s) URL (server "
            "misconfigured). Use plantuml, d2 or graphviz instead."
        )
    if requests is None:
        raise DiagramError(
            "mermaid needs the 'requests' package, which is missing from the server "
            "image. Use plantuml, d2 or graphviz instead."
        )
    timeout = _timeout_s()
    try:
        # The endpoint is server config pointing at a sidecar on the private
        # Docker network — deliberately NOT routed through the public-IP SSRF
        # guard that covers caller-supplied URLs. Nothing here comes from the caller.
        resp = requests.post(
            url,
            data=_with_mermaid_init(source).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=timeout,
        )
    except Exception as e:  # requests.RequestException + socket/TLS surprises
        raise DiagramError(
            f"Mermaid render failed: cannot reach the mermaid sidecar at {url} ({e}). "
            "Use plantuml, d2 or graphviz instead."
        ) from e
    if resp.status_code != 200:
        # kroki puts the mermaid parse error in the response body.
        raise DiagramError(
            f"Mermaid render failed (HTTP {resp.status_code}): {_trim(resp.content)}"
        )
    return resp.content


def render(engine: str, source: str, fmt: str = "png") -> bytes:
    """Render ``source`` with ``engine``; return the image bytes.

    ``engine`` accepts the names in :data:`ENGINES` and the aliases in
    :data:`BLOCK_CLASSES`. ``fmt`` is ``"png"`` (default — the safe choice for
    Kindle) or ``"svg"``. Raises :class:`DiagramError`, carrying the renderer's
    stderr, on unknown engine/format, bad source, timeout or render failure.
    """
    name = _resolve_engine(engine)
    f = (fmt or "png").strip().lower()
    if f not in FORMATS:
        raise DiagramError(
            f"Unknown diagram format {fmt!r}. Valid formats: {', '.join(FORMATS)}. "
            "Use 'png' for Kindle — its converter handles inline SVG unreliably."
        )
    text = _clean_source(name, source)

    with _render_semaphore:
        if name == "plantuml":
            data = render_plantuml(text, f)
        elif name == "graphviz":
            data = _render_graphviz(text, f)
        elif name == "d2":
            data = _render_d2(text, f)
        else:
            data = _render_mermaid(text, f)

    _assert_image(name, f, data)
    logger.debug("Rendered %s diagram: %d bytes (%s)", name, len(data), f)
    return data


def _block_engine(pre: Tag) -> str | None:
    """Engine for a ``<pre>``, from its own class or that of a child ``<code>``.

    Markdown-minded agents write ``<pre><code class="language-mermaid">``; both
    spellings are accepted so a valid diagram never silently ships as raw text.
    """

    def pick(raw) -> str | None:
        if isinstance(raw, str):
            raw = raw.split()
        for cls in raw or []:
            key = str(cls).strip().lower()
            if key.startswith("language-"):
                key = key[len("language-"):]
            engine = BLOCK_CLASSES.get(key)
            if engine:
                return engine
        return None

    found = pick(pre.get("class"))
    if found:
        return found
    code = pre.find("code", recursive=False)
    return pick(code.get("class")) if isinstance(code, Tag) else None


def process_diagram_blocks(soup: BeautifulSoup, sink: Sink) -> int:
    """Replace every diagram ``<pre>`` in ``soup`` with a rendered ``<img>``.

    Blocks are recognised by css class (:data:`BLOCK_CLASSES`). Each is rendered
    to PNG and handed to ``sink(png_bytes, engine) -> str``, whose return value
    becomes the ``src`` — a ``data:`` URI, a stored asset path, whatever the
    caller wants. Returns the number of blocks rendered.

    A failing block raises :class:`DiagramError` naming the block index and
    engine: shipping a book with a silently missing diagram is worse than an
    error the author can act on.
    """
    rendered = 0
    for pre in soup.find_all("pre"):
        engine = _block_engine(pre)
        if engine is None:
            continue
        index = rendered + 1
        # get_text() decodes HTML entities (&lt; / &gt;) back to < / > so the
        # agent can safely escape arrow syntax (-->, <|--, Map<K,V>) in raw HTML.
        source = pre.get_text()
        try:
            png = render(engine, source, "png")
            src = sink(png, engine)
        except DiagramError as e:
            raise DiagramError(f"diagram block #{index} ({engine}): {e}") from e
        except Exception as e:
            raise DiagramError(
                f"diagram block #{index} ({engine}) could not be stored: {e}"
            ) from e
        img = soup.new_tag("img", src=src, alt=f"{ENGINE_LABELS[engine]} diagram")
        pre.replace_with(img)
        rendered += 1
    if rendered:
        logger.info("Rendered %d diagram block(s)", rendered)
    return rendered
