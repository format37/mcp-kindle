"""PlantUML entry points — a thin back-compat layer over :mod:`diagrams`.

The diagram pipeline grew from "PlantUML only" to several engines (PlantUML,
Graphviz, D2, Mermaid); the implementation now lives in :mod:`diagrams` so
there is exactly one of it. This module stays because ``html_tools`` imports
:func:`process_plantuml_blocks`, and to keep the ``<pre>`` -> inline ``data:``
URI behaviour available in one call.

New code should call :func:`diagrams.process_diagram_blocks` directly with its
own sink (e.g. one that stores the PNG as a book asset instead of inlining
base64, which inflates the EPUB by ~33% per diagram).
"""

from __future__ import annotations

import base64
import logging

from bs4 import BeautifulSoup

import diagrams
from diagrams import PLANTUML_JAR, DiagramError, process_diagram_blocks

logger = logging.getLogger(__name__)

__all__ = [
    "PLANTUML_JAR",
    "DiagramError",
    "render_plantuml",
    "process_plantuml_blocks",
]


def render_plantuml(source: str, jar: str = PLANTUML_JAR) -> bytes:
    """Render PlantUML source to PNG bytes.

    Raises :class:`diagrams.DiagramError` (a ``RuntimeError``) on missing jar,
    timeout, non-zero exit or non-PNG output, with PlantUML's own diagnostics
    trimmed into the message.
    """
    return diagrams.render_plantuml(source, "png", jar=jar)


def _data_uri_sink(png: bytes, engine: str) -> str:
    """Inline the PNG so ``html_tools._embed_images`` picks it up unchanged."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def process_plantuml_blocks(soup: BeautifulSoup) -> int:
    """Replace every diagram ``<pre>`` block with an inline ``data:`` URI <img>.

    Despite the name this now covers every engine in
    :data:`diagrams.BLOCK_CLASSES` (plantuml, mermaid, d2, graphviz + aliases),
    because the block scan itself is shared. Returns the number rendered.
    """
    return process_diagram_blocks(soup, _data_uri_sink)
