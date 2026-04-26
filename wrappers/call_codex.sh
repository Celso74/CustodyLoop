#!/usr/bin/env bash
# CustodyLoop — minimal Codex wrapper.
# Invokes the `codex` CLI in non-interactive exec mode for code execution.
# Usage: call_codex.sh <input_file> <output_file> [workdir] [timeout_seconds]
#
# Requires:
#   - `codex` CLI installed and on PATH
#   - the user's own ChatGPT/OpenAI auth configured for codex
#   - CUSTODYLOOP_MODEL_ID env var set
set -euo pipefail

IN="${1:?usage: call_codex.sh <input> <output> [workdir] [timeout]}"
OUT="${2:?usage: call_codex.sh <input> <output> [workdir] [timeout]}"
WORKDIR="${3:-$PWD}"
TIMEOUT="${4:-1200}"
MODEL="${CUSTODYLOOP_MODEL_ID:?CUSTODYLOOP_MODEL_ID must be set}"

command -v codex >/dev/null || { echo "codex CLI not found" >&2; exit 127; }
[[ -f "$IN" ]] || { echo "input file not found: $IN" >&2; exit 1; }
[[ -d "$WORKDIR" ]] || { echo "workdir not found: $WORKDIR" >&2; exit 1; }

cd "$WORKDIR"
timeout "$TIMEOUT" codex exec --model "$MODEL" - < "$IN" > "$OUT"
