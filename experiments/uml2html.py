#!/usr/bin/env python3
"""Render inline <pre class="plantuml">...</pre> blocks to PNG images.

This is the preprocessing step we plan to bolt onto html_to_epub: the
agent writes ONE HTML document that already contains its diagram source
inline; this script (a) extracts each block, (b) renders it to PNG via
plantuml.jar, (c) substitutes an <img> tag, and (d) writes a processed
HTML you can open in a browser to verify the result.

Usage:
    python experiments/uml2html.py [INPUT_HTML] [OUTPUT_HTML]

Defaults:
    INPUT  experiments/sample_uml.html
    OUTPUT experiments/output/sample_uml.processed.html
    PNGs   experiments/output/uml/{sha1}.png  (alongside OUTPUT)

Requires:
    java        (default-jre)
    graphviz    (`dot` on PATH)
    plantuml.jar  - path via $PLANTUML_JAR, default ~/plantuml.jar
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from bs4 import BeautifulSoup

PLANTUML_JAR = os.environ.get("PLANTUML_JAR", str(Path.home() / "plantuml.jar"))


def render_plantuml(source: str, jar: str = PLANTUML_JAR) -> bytes:
    """Run plantuml.jar in pipe mode; return rendered PNG bytes."""
    result = subprocess.run(
        ["java", "-jar", jar, "-tpng", "-nometadata", "-pipe"],
        input=source.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(
            f"PlantUML render failed (rc={result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    return result.stdout


def process_html(in_path: Path, out_path: Path) -> tuple[int, Path]:
    """Render every plantuml block in in_path; write processed HTML to out_path.

    Returns (number_of_diagrams_rendered, png_directory).
    """
    soup = BeautifulSoup(in_path.read_text(encoding="utf-8"), "lxml")
    png_dir = out_path.parent / "uml"
    png_dir.mkdir(parents=True, exist_ok=True)

    rendered = 0
    for pre in soup.find_all("pre"):
        classes = pre.get("class") or []
        if "plantuml" not in classes:
            continue

        # get_text() decodes &lt; / &gt; back to < / > automatically.
        source = pre.get_text()
        if "@startuml" not in source:
            source = f"@startuml\n{source.strip()}\n@enduml\n"

        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        png_path = png_dir / f"{digest}.png"

        if not png_path.exists():
            png_path.write_bytes(render_plantuml(source))
            (png_dir / f"{digest}.puml").write_text(source)  # for debugging

        img = soup.new_tag(
            "img",
            src=f"uml/{digest}.png",
            alt=f"PlantUML diagram {digest}",
        )
        pre.replace_with(img)
        rendered += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(str(soup), encoding="utf-8")
    return rendered, png_dir


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", nargs="?", default=str(here / "sample_uml.html"),
        help="Input HTML file (default: experiments/sample_uml.html)",
    )
    parser.add_argument(
        "output", nargs="?",
        default=str(here / "output" / "sample_uml.processed.html"),
        help="Output HTML file (PNGs go in a sibling 'uml/' directory)",
    )
    args = parser.parse_args(argv)

    if not Path(PLANTUML_JAR).is_file():
        print(f"error: plantuml.jar not found at {PLANTUML_JAR} "
              f"(set $PLANTUML_JAR)", file=sys.stderr)
        return 2

    n, png_dir = process_html(Path(args.input), Path(args.output))
    print(f"Rendered {n} diagram(s) -> {png_dir}/")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
