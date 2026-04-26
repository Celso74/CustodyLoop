"""Tests for custodyloop/json_extract.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop.json_extract import extract_json


def test_marker_block_priority():
    text = """preamble
===JSON===
{"decision": "APPROVED"}
===END===
trailing junk {"decision": "REJECTED"}"""
    obj = extract_json(text)
    assert obj["decision"] == "APPROVED"


def test_json_fence():
    text = 'Here you go:\n```json\n{"step_id": "s1", "errors": []}\n```\nthanks'
    assert extract_json(text)["step_id"] == "s1"


def test_plain_fence():
    text = '```\n{"a": 1}\n```'
    assert extract_json(text)["a"] == 1


def test_balanced_brace_scan():
    text = 'Some preamble {"task": "t", "steps": [{"id": "s1", "objective": "x"}]} trailing'
    obj = extract_json(text)
    assert obj["task"] == "t"
    assert obj["steps"][0]["id"] == "s1"


def test_strings_with_braces_dont_break_scan():
    text = '{"summary": "this { is } weird", "x": 1}'
    obj = extract_json(text)
    assert obj["x"] == 1


def test_raw_json():
    assert extract_json('{"k":"v"}')["k"] == "v"


def test_rejects_non_object():
    with pytest.raises(ValueError):
        extract_json("[1,2,3]")


def test_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("just some text, no json here")


def test_picks_first_valid_object_when_multiple_present():
    text = '{"a": 1} and later {"b": 2}'
    obj = extract_json(text)
    assert obj == {"a": 1}
