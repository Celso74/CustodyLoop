#!/usr/bin/env bash
# CustodyLoop — minimal ChatGPT/GPT-as-validator wrapper.
# Invokes a CLI that produces a single-shot completion for the validator role.
# Substitute with your preferred CLI if it differs.
# Usage: call_chatgpt.sh <input_file> <output_file> [timeout_seconds]
#
# Requires:
#   - a CLI that accepts a model id and reads a prompt from stdin
#   - the user's own auth
#   - CUSTODYLOOP_MODEL_ID env var set
set -euo pipefail

IN="${1:?usage: call_chatgpt.sh <input> <output> [timeout]}"
OUT="${2:?usage: call_chatgpt.sh <input> <output> [timeout]}"
TIMEOUT="${3:-600}"
MODEL="${CUSTODYLOOP_MODEL_ID:?CUSTODYLOOP_MODEL_ID must be set}"

# Default backend is `codex exec` (which supports non-coding GPT models too).
# Replace this command if you use a different CLI.
command -v codex >/dev/null || { echo "codex CLI not found" >&2; exit 127; }
[[ -f "$IN" ]] || { echo "input file not found: $IN" >&2; exit 1; }

timeout "$TIMEOUT" codex exec --model "$MODEL" - < "$IN" > "$OUT"
