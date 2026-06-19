#!/usr/bin/env bash
# Build + (re)start the kindle MCP locally on 127.0.0.1:8018.
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d --build "$@"
echo "kindle MCP -> http://localhost:8018/kindle/"
