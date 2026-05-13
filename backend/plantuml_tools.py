"""Render inline <pre class="plantuml">...</pre> blocks to PNG images.

The agent embeds PlantUML source directly in their HTML; this module
extracts each block, shells out to ``plantuml.jar`` to render it to
PNG, and replaces the ``<pre>`` with an ``<img>`` carrying the PNG as
a ``data:`` URI. The existing image-embedding pass in
:func:`html_tools._embed_images` then turns the data URI into an EPUB
image item, so this module needs no integration beyond a single call.

Run order in the pipeline:
    sanitize -> process_plantuml_blocks -> embed_images -> split_chapters

Requires (provided by the Docker image):
    java                (default-jre-headless)
    graphviz            (``dot`` on PATH)
    plantuml.jar        - path via ``$PLANTUML_JAR`` (default /opt/plantuml.jar)
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PLANTUML_JAR = os.environ.get("PLANTUML_JAR", "/opt/plantuml.jar")
RENDER_TIMEOUT = int(os.environ.get("PLANTUML_TIMEOUT", "30"))


def render_plantuml(source: str, jar: str = PLANTUML_JAR) -> bytes:
    """Run ``plantuml.jar`` in pipe mode; return rendered PNG bytes.

    Raises :class:`RuntimeError` on missing jar, timeout, non-zero exit,
    or empty stdout (with the PlantUML stderr trimmed to 500 chars).
    """
    if not Path(jar).is_file():
        raise RuntimeError(f"plantuml.jar not found at {jar}")
    try:
        result = subprocess.run(
            ["java", "-jar", jar, "-tpng", "-nometadata", "-pipe"],
            input=source.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"PlantUML render timed out after {RENDER_TIMEOUT}s")
    if result.returncode != 0 or not result.stdout:
        err = result.stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"PlantUML render failed (rc={result.returncode}): {err or 'no stderr'}"
        )
    return result.stdout


def process_plantuml_blocks(soup: BeautifulSoup) -> int:
    """Replace every ``<pre class="plantuml">`` block with a rendered <img>.

    The replacement <img> uses a ``data:image/png;base64,...`` URI so the
    existing image-embedding pipeline picks it up without modification.

    Returns the number of blocks rendered.
    """
    rendered = 0
    for pre in soup.find_all("pre"):
        classes = pre.get("class") or []
        if "plantuml" not in classes:
            continue
        # get_text() decodes HTML entities (&lt; / &gt;) back to < / > so
        # the agent can safely escape PlantUML's arrow syntax in raw HTML.
        source = pre.get_text()
        if "@startuml" not in source:
            source = f"@startuml\n{source.strip()}\n@enduml\n"
        png = render_plantuml(source)
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        img = soup.new_tag("img", src=data_uri, alt="PlantUML diagram")
        pre.replace_with(img)
        rendered += 1
    if rendered:
        logger.info("Rendered %d PlantUML block(s)", rendered)
    return rendered
