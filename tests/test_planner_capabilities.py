"""Tests for CustodyLoop Fix 4 — planner workdir capability detection.

Six requirements from the Fix 4 spec:
1. planner prompt includes workdir capability block
2. non-git workdir surfaces forbidden git commands in planner prompt
3. git repo workdir does not forbid git commands
4. python3 preference is surfaced
5. produce_plan threads workdir through to the prompt
6. existing CustodyLoop tests still pass (covered by other suites)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop.planner import (
    build_planner_prompt,
    capabilities_to_json,
    detect_workdir_capabilities,
    produce_plan,
)
from custodyloop.runner import Runner

# ── detect_workdir_capabilities ─────────────────────────────────────────────


def test_no_workdir_returns_safe_defaults():
    caps = detect_workdir_capabilities(None)
    assert caps["workdir_path"] is None
    assert caps["workdir_exists"] is False
    assert caps["is_git_repo"] is False
    # The host is expected to have python3 in CI/dev. If not, this assertion is
    # surfacing a real environment issue, not a bug in detection.
    assert isinstance(caps["has_python3"], bool)


def test_non_git_workdir_is_git_repo_false(tmp_path):
    caps = detect_workdir_capabilities(tmp_path)
    assert caps["workdir_exists"] is True
    assert caps["is_git_repo"] is False


def test_git_repo_dir_detected(tmp_path):
    (tmp_path / ".git").mkdir()
    caps = detect_workdir_capabilities(tmp_path)
    assert caps["is_git_repo"] is True


def test_git_worktree_pointer_file_detected(tmp_path):
    """git worktrees use a `.git` file (not directory) pointing to gitdir."""
    (tmp_path / ".git").write_text("gitdir: /some/where\n")
    caps = detect_workdir_capabilities(tmp_path)
    assert caps["is_git_repo"] is True


def test_workdir_inside_parent_git_repo_detected(tmp_path):
    """If workdir is nested inside a git tree, capability check walks up."""
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)
    caps = detect_workdir_capabilities(nested)
    assert caps["is_git_repo"] is True


def test_python_cmd_prefers_python3():
    """python_cmd must prefer python3 when both exist; pick python only as fallback."""
    with patch("custodyloop.planner.shutil.which") as which:
        which.side_effect = lambda cmd: "/usr/bin/python3" if cmd == "python3" else None
        caps = detect_workdir_capabilities(None)
        assert caps["python_cmd"] == "python3"

    with patch("custodyloop.planner.shutil.which") as which:
        which.side_effect = lambda cmd: "/usr/bin/python" if cmd == "python" else None
        caps = detect_workdir_capabilities(None)
        assert caps["python_cmd"] == "python"

    with patch("custodyloop.planner.shutil.which") as which:
        which.return_value = None
        caps = detect_workdir_capabilities(None)
        assert caps["python_cmd"] == ""


def test_capabilities_to_json_roundtrip():
    caps = detect_workdir_capabilities(None)
    s = capabilities_to_json(caps)
    obj = json.loads(s)
    assert obj["workdir_path"] is None
    assert "is_git_repo" in obj
    assert "has_python3" in obj


# ── build_planner_prompt ─────────────────────────────────────────────────────


def test_planner_prompt_contains_capability_block(tmp_path):
    """Requirement 1: planner prompt includes the WORKDIR_CAPABILITIES block."""
    caps = detect_workdir_capabilities(tmp_path)
    prompt = build_planner_prompt("do something", capabilities=caps)
    # The placeholder must be replaced
    assert "<WORKDIR_CAPABILITIES>" not in prompt
    # The actual block must be present
    assert "Workdir runtime capabilities" in prompt
    assert "workdir_path:" in prompt
    assert "is_git_repo: False" in prompt


def test_planner_prompt_forbids_git_in_non_git_workdir(tmp_path):
    """Requirement 2: a non-git workdir produces a FORBIDDEN constraint
    listing git commands so Claude does not select them."""
    caps = detect_workdir_capabilities(tmp_path)
    prompt = build_planner_prompt("task", capabilities=caps)
    assert "FORBIDDEN" in prompt
    assert "git diff" in prompt
    assert "git status" in prompt
    assert "exit 129" in prompt


def test_planner_prompt_does_not_forbid_git_in_git_workdir(tmp_path):
    """Requirement 3: a real git repo workdir does NOT emit the
    git-forbidden constraint."""
    (tmp_path / ".git").mkdir()
    caps = detect_workdir_capabilities(tmp_path)
    prompt = build_planner_prompt("task", capabilities=caps)
    assert "is_git_repo: True" in prompt
    # The "FORBIDDEN ... git diff" derived constraint must NOT appear.
    # (The static template still warns about other tools and the
    # generic "do not use git subcommands when is_git_repo False" rule
    # exists, but the dynamic FORBIDDEN-derived line is gated.)
    forbidden_block_pos = prompt.find("FORBIDDEN in this workdir")
    assert forbidden_block_pos == -1, "Git workdir should not emit dynamic FORBIDDEN-git constraint"


def test_planner_prompt_recommends_python3_when_python_missing():
    """Requirement 4: if python is not available but python3 is, surface
    a derived constraint forbidding bare `python`."""
    with patch("custodyloop.planner.shutil.which") as which:
        # python3 exists, python does not
        which.side_effect = lambda cmd: "/usr/bin/" + cmd if cmd in ("python3",) else None
        caps = detect_workdir_capabilities(None)
        prompt = build_planner_prompt("task", capabilities=caps)
        assert "FORBIDDEN" in prompt
        assert "bare `python`" in prompt or "use `python3`" in prompt


def test_planner_prompt_recommends_python_m_pytest_when_pytest_missing():
    with patch("custodyloop.planner.shutil.which") as which:
        # python3 exists, pytest does not
        which.side_effect = lambda cmd: "/usr/bin/python3" if cmd == "python3" else None
        caps = detect_workdir_capabilities(None)
        prompt = build_planner_prompt("task", capabilities=caps)
        assert "python3 -m pytest" in prompt


def test_planner_prompt_default_capabilities_when_none():
    """build_planner_prompt with capabilities=None must still produce a
    valid prompt (no placeholder leakage, no crash)."""
    prompt = build_planner_prompt("task", capabilities=None)
    assert "<WORKDIR_CAPABILITIES>" not in prompt
    assert "<TASK>" not in prompt
    assert "task" in prompt


# ── produce_plan threads workdir through ────────────────────────────────────


class _StubRunner(Runner):
    """Captures the prompt sent to claude and returns a canned plan."""

    def __init__(self):
        self.prompt: str | None = None

    def run(self, model, prompt, *, workdir=None, timeout=600):
        assert model == "claude"
        self.prompt = prompt
        canned_plan = {
            "task": "t",
            "audience": "engineer",
            "artifact_type": "code_change",
            "steps": [{"id": "step_1", "objective": "x", "allowed_files": ["foo.py"]}],
            "anti_failure": {
                "artifact_compliance": "y",
                "no_hallucinated_specifics": "n",
                "audience_alignment": "engineer",
            },
        }
        return f"===JSON===\n{json.dumps(canned_plan)}\n===END==="


def test_produce_plan_threads_workdir_into_prompt(tmp_path):
    """Requirement 5: the workdir argument to produce_plan reaches the
    capability block in the rendered prompt."""
    (tmp_path / ".git").mkdir()  # mark as git repo
    runner = _StubRunner()
    plan = produce_plan("task", runner, workdir=tmp_path)
    assert plan.steps[0].id == "step_1"
    assert runner.prompt is not None
    assert "is_git_repo: True" in runner.prompt
    assert str(tmp_path.resolve()) in runner.prompt or str(tmp_path) in runner.prompt


def test_produce_plan_with_no_workdir_uses_advisory_caps():
    runner = _StubRunner()
    produce_plan("task", runner, workdir=None)
    assert runner.prompt is not None
    assert "is_git_repo: False" in runner.prompt
    # Advisory marker
    assert "advisory step only" in runner.prompt or "(none" in runner.prompt
