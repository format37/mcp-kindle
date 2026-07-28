#!/usr/bin/env bash
# Build + (re)start the kindle MCP locally on 127.0.0.1:8018.
#
#   ./compose.sh              PlantUML, Graphviz and D2 (all render in-image)
#   ./compose.sh --mermaid    ...plus the Mermaid sidecar (~1 GB image, ~330 MB RSS)
#
# `docker compose up` has no --profile flag, so the sidecar is selected through
# COMPOSE_PROFILES. MERMAID_URL is set in the same breath: the server reports
# its available engines on /health, and it must not claim Mermaid when the
# sidecar is not running.
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--mermaid" ]; then
    shift
    export COMPOSE_PROFILES=mermaid
    export MERMAID_URL=http://kroki-mermaid:8002
fi

docker compose up -d --build "$@"
echo "kindle MCP -> http://localhost:8018/kindle/"
echo "health     -> curl -s http://localhost:8018/health"
