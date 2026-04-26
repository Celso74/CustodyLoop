#!/usr/bin/env bash
# CustodyLoop — minimal Claude wrapper.
# Invokes the `claude` CLI in non-interactive mode.
# Usage: call_claude.sh <input_file> <output_file> [timeout_seconds]
#
# Requires:
#   - `claude` CLI installed and on PATH
#   - the user's own Anthropic auth (via `claude` CLI login)
#   - CUSTODYLOOP_MODEL_ID env var set to a model identifier the CLI accepts
set -euo pipefail

IN="${1:?usage: call_claude.sh <input> <output> [timeout]}"
OUT="${2:?usage: call_claude.sh <input> <output> [timeout]}"
TIMEOUT="${3:-1200}"
MODEL="${CUSTODYLOOP_MODEL_ID:?CUSTODYLOOP_MODEL_ID must be set}"

command -v claude >/dev/null || { echo "claude CLI not found" >&2; exit 127; }
[[ -f "$IN" ]] || { echo "input file not found: $IN" >&2; exit 1; }

timeout "$TIMEOUT" claude --model "$MODEL" --print < "$IN" > "$OUT"
