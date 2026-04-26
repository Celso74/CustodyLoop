"""Stage 1 — Claude planner.

Turns operator task → strict PlanPacket. Claude does not approve its
own work; the executor will work against this contract and the
validator will judge the result.

Fix 4 (2026-04-25): Workdir capability detection. The planner now
inspects the workdir before composing the prompt and tells Claude what
tools and conditions are actually available. This prevents the planner
from putting `git diff` / `git status` into `required_tests` when the
workdir is not a git repository (which would deterministically exit 129
and cause the validator to reject every retry).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .json_extract import extract_json
from .runner import Runner
from .types import PlanPacket

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text()


def detect_workdir_capabilities(workdir: Optional[Path]) -> Dict[str, Any]:
    """Inspect workdir + PATH for tools the planner may select for required_tests.

    Returns a dict with stable keys:
      workdir_path:  str|None — absolute path or None if no workdir
      workdir_exists: bool
      is_git_repo:   bool — True if `.git` directory or worktree pointer file exists
      has_python3:   bool
      has_python:    bool
      python_cmd:    str — recommended python invocation ('python3' preferred)
      has_pytest:    bool — pytest executable on PATH
      has_pytest_module: bool — best-effort: True if has_python3 is True (cannot
                              be confirmed without import; planner is told to
                              use `python3 -m pytest` when has_pytest is False)
      has_diff:      bool
      has_cmp:       bool
      has_grep:      bool

    The check is conservative and stat-only — never executes a subprocess to
    verify, since the planner runs before any executor work.
    """
    caps: Dict[str, Any] = {
        "workdir_path": None,
        "workdir_exists": False,
        "is_git_repo": False,
        "has_python3": shutil.which("python3") is not None,
        "has_python": shutil.which("python") is not None,
        "has_pytest": shutil.which("pytest") is not None,
        "has_diff": shutil.which("diff") is not None,
        "has_cmp": shutil.which("cmp") is not None,
        "has_grep": shutil.which("grep") is not None,
    }

    caps["python_cmd"] = "python3" if caps["has_python3"] else ("python" if caps["has_python"] else "")
    # If the system has python3 we presume `python3 -m pytest` is the safe fallback.
    caps["has_pytest_module"] = caps["has_python3"]

    if workdir is None:
        return caps

    workdir = Path(workdir)
    caps["workdir_path"] = str(workdir.resolve()) if workdir.exists() else str(workdir)
    caps["workdir_exists"] = workdir.exists()

    if caps["workdir_exists"]:
        # `.git` is a directory in standard repos and a file in worktrees.
        git = workdir / ".git"
        if git.exists():
            caps["is_git_repo"] = True
        else:
            # Walk up to repo root if workdir is inside a parent git tree.
            for parent in workdir.resolve().parents:
                if (parent / ".git").exists():
                    caps["is_git_repo"] = True
                    break

    return caps


def _format_capabilities(caps: Dict[str, Any]) -> str:
    """Render capabilities as a compact, human-readable block for the prompt."""
    lines = ["Workdir runtime capabilities (use ONLY these for required_tests):"]
    lines.append(f"  workdir_path: {caps['workdir_path'] or '(none — advisory step only)'}")
    lines.append(f"  workdir_exists: {caps['workdir_exists']}")
    lines.append(f"  is_git_repo: {caps['is_git_repo']}")
    lines.append(f"  has_python3: {caps['has_python3']}")
    lines.append(f"  has_python: {caps['has_python']}")
    lines.append(f"  recommended_python_invocation: {caps['python_cmd'] or '(none)'}")
    lines.append(f"  has_pytest_executable: {caps['has_pytest']}")
    lines.append(f"  has_pytest_via_python3_m: {caps['has_pytest_module']}")
    lines.append(f"  has_diff: {caps['has_diff']}")
    lines.append(f"  has_cmp: {caps['has_cmp']}")
    lines.append(f"  has_grep: {caps['has_grep']}")

    derived: list[str] = []
    if not caps["is_git_repo"]:
        derived.append(
            "FORBIDDEN in this workdir (not a git repo): `git diff`, `git diff --name-only`, "
            "`git status --porcelain`, `git log`, `git show`, `git rev-parse`, `git ls-files`. "
            "These will exit 129 and cause the validator to reject every attempt."
        )
    if not caps["has_python"] and caps["has_python3"]:
        derived.append(
            "FORBIDDEN in this environment: bare `python` (use `python3` instead). "
            "`python -c ...` and `python -m ...` will exit 127."
        )
    if not caps["has_pytest"] and caps["has_pytest_module"]:
        derived.append("Use `python3 -m pytest` (not bare `pytest`) — the pytest executable is not on PATH.")

    if derived:
        lines.append("")
        lines.append("Derived constraints:")
        for d in derived:
            lines.append(f"  - {d}")

    return "\n".join(lines)


def build_planner_prompt(task: str, capabilities: Optional[Dict[str, Any]] = None) -> str:
    """Build planner prompt. If capabilities is None, detects with no workdir."""
    if capabilities is None:
        capabilities = detect_workdir_capabilities(None)
    template = _load("planner")
    return template.replace("<TASK>", task.strip()).replace(
        "<WORKDIR_CAPABILITIES>", _format_capabilities(capabilities)
    )


def produce_plan(
    task: str,
    runner: Runner,
    *,
    workdir: Optional[Path] = None,
    timeout: int = 600,
) -> PlanPacket:
    """Invoke Claude planner. Returns a validated PlanPacket.

    `workdir` is passed in so the planner can be told what tools are
    actually available before it composes `required_tests`. Without it,
    capability detection assumes no workdir (advisory plan).

    Raises ValueError on malformed output. The caller may retry once.
    """
    if not task or not task.strip():
        raise ValueError("produce_plan: task must be non-empty")
    capabilities = detect_workdir_capabilities(workdir)
    prompt = build_planner_prompt(task, capabilities=capabilities)
    raw = runner.run("claude", prompt, timeout=timeout)
    obj = extract_json(raw)
    return PlanPacket.from_dict(obj)


def capabilities_to_json(caps: Dict[str, Any]) -> str:
    """Serialize capabilities for artifact persistence."""
    return json.dumps(caps, indent=2, sort_keys=True)
