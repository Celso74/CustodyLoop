"""Robust JSON extraction from LLM text output.

LLMs don't reliably emit clean JSON — they wrap in markdown fences,
add prose preambles, or sandwich JSON between ===MARKER=== blocks.
This module finds the first valid top-level JSON object in mixed text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_MARKER_RE = re.compile(r"===\s*JSON\s*===\s*(\{.*?\})\s*===\s*END\s*===", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Dict[str, Any]:
    """Pull the first top-level JSON object from text.

    Strategy (in order):
      1. ===JSON===…===END=== marker
      2. ```json fenced block
      3. ``` fenced block
      4. balanced-brace scan from the first '{' — accepts the largest valid object
      5. raw json.loads

    Raises ValueError if nothing parses.
    """
    if not isinstance(text, str):
        raise ValueError("extract_json: input must be str")

    m = _MARKER_RE.search(text)
    if m:
        try:
            return _coerce_object(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass

    for m in _FENCE_RE.finditer(text):
        try:
            return _coerce_object(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue

    obj = _scan_balanced(text)
    if obj is not None:
        return obj

    try:
        return _coerce_object(json.loads(text.strip()))
    except json.JSONDecodeError as exc:
        raise ValueError(f"No valid JSON object found in output: {exc}") from exc


def _coerce_object(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object, got {type(obj).__name__}")
    return obj


def _scan_balanced(text: str) -> Dict[str, Any] | None:
    """Scan from the first '{' looking for a balanced top-level object."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
