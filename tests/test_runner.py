"""Tests for ShellRunner model-ID resolution.

Covers the resolution priority (instance dict > env > error) and
per-role isolation. Uses fake POSIX shell wrappers that echo
$CUSTODYLOOP_MODEL_ID. No API access.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop.runner import RunnerError, ShellRunner


def _make_fake_bin(tmp_path: Path) -> Path:
    """Create a bin/ dir with three fake wrappers that write
    $CUSTODYLOOP_MODEL_ID to argv[2] (the output file)."""
    script = '#!/bin/sh\nprintf "%s" "$CUSTODYLOOP_MODEL_ID" > "$2"\n'
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("call_claude.sh", "call_codex.sh", "call_chatgpt.sh"):
        p = bin_dir / name
        p.write_text(script)
        p.chmod(0o755)
    return bin_dir


def test_instance_dict_wins_over_env(tmp_path, monkeypatch):
    bin_dir = _make_fake_bin(tmp_path)
    monkeypatch.setenv("CUSTODYLOOP_MODEL_ID", "from-env")
    r = ShellRunner(bin_dir=bin_dir, model_ids={"claude": "from-dict"})
    assert r.run("claude", "ignored") == "from-dict"


def test_env_fallback_when_instance_missing_role(tmp_path, monkeypatch):
    bin_dir = _make_fake_bin(tmp_path)
    monkeypatch.setenv("CUSTODYLOOP_MODEL_ID", "from-env")
    r = ShellRunner(bin_dir=bin_dir)
    assert r.run("claude", "ignored") == "from-env"


def test_no_id_raises_with_helpful_message(tmp_path, monkeypatch):
    bin_dir = _make_fake_bin(tmp_path)
    monkeypatch.delenv("CUSTODYLOOP_MODEL_ID", raising=False)
    r = ShellRunner(bin_dir=bin_dir)
    with pytest.raises(RunnerError, match="--planner-model"):
        r.run("claude", "ignored")


def test_per_role_isolation(tmp_path, monkeypatch):
    """Three roles, three different IDs, no env var — each call gets its own."""
    bin_dir = _make_fake_bin(tmp_path)
    monkeypatch.delenv("CUSTODYLOOP_MODEL_ID", raising=False)
    r = ShellRunner(
        bin_dir=bin_dir,
        model_ids={"claude": "A", "codex": "B", "chatgpt": "C"},
    )
    assert r.run("claude", "x") == "A"
    assert r.run("codex", "x") == "B"
    assert r.run("chatgpt", "x") == "C"


def test_unknown_model_raises(tmp_path):
    bin_dir = _make_fake_bin(tmp_path)
    r = ShellRunner(bin_dir=bin_dir, model_ids={"claude": "id"})
    with pytest.raises(RunnerError, match="Unknown model"):
        r.run("not-a-model", "x")
