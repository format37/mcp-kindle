#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — install the kindle skill for Claude Code, with this clone's
# path filled in.
#
#   ./skills/install.sh
#   ./skills/install.sh --zip ~/kindle-skill.zip
#
# A symlink would be tidier, but the skill references the clone's bind-mounted
# data/ folder by absolute path, and that path differs per machine. So this
# copies and substitutes, and re-running it after a `git pull` re-applies the
# path on top of the new version.
#
# --zip additionally emits an archive for claude.ai (Customize -> Skills ->
# "+" -> Create skill). Same single source, two targets: the terminal reads
# the installed copy, the web and phone read the uploaded one, and neither
# drifts from the other. The skill's §10 tells the claude.ai side to ignore
# the local-path instructions, so the substituted path is harmless there.
# ---------------------------------------------------------------------------
set -euo pipefail

ZIP=""
if [ "${1:-}" = "--zip" ]; then
    ZIP="${2:?--zip needs an output path}"
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/skills/kindle/SKILL.md"
DEST="${SKILLS_DIR:-$HOME/.claude/skills}/kindle"
[ -f "$SRC" ] || { echo "error: $SRC not found" >&2; exit 1; }

mkdir -p "$DEST"
sed "s|/path/to/mcp-kindle|${REPO}|g" "$SRC" > "$DEST/SKILL.md"

if [ -n "$ZIP" ]; then
    # claude.ai wants a zip whose top level is the skill FOLDER, not a bare
    # SKILL.md, and the folder name must match the `name:` in the frontmatter.
    command -v zip >/dev/null || { echo "error: zip is not installed" >&2; exit 1; }
    STAGE=$(mktemp -d)
    mkdir -p "$STAGE/kindle"
    cp "$DEST/SKILL.md" "$STAGE/kindle/SKILL.md"
    rm -f "$ZIP"
    ( cd "$STAGE" && zip -q -r "$ZIP" kindle )
    rm -rf "$STAGE"
    echo "packaged: $ZIP  (claude.ai -> Customize -> Skills -> + -> Create skill)"
fi

echo "installed: $DEST/SKILL.md"
echo "  repo:     $REPO"
echo
echo "Re-run this after a git pull to re-apply the path."
