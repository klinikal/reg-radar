#!/usr/bin/env bash
# Regenerate the hashed lockfiles each image/CI job installs from.
# Inputs are */requirements.txt (direct pins); outputs are */requirements.lock.
# playwright is collector-image-only, so the mcp/webui locks drop it.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -r "$tmp"' EXIT
grep -v '^playwright' collector/requirements.txt > "$tmp/collector-lib.txt"
lock() { uv pip compile --universal --python-version 3.11 --generate-hashes --no-header --no-annotate -q "${@:2}" -o "$1"; }
lock collector/requirements.lock collector/requirements.txt
lock mcpserver/requirements.lock mcpserver/requirements.txt "$tmp/collector-lib.txt"
lock webui/requirements.lock webui/requirements.txt "$tmp/collector-lib.txt"
