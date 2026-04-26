"""Standalone CustodyLoop dry-run script.

Drives the full pipeline with canned responses (no real LLMs) and a
small safe task: 'create greeting.txt with Hello World' in a tempdir.

Usage:
    python3 bin/r6/dry_run.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from custodyloop import auto_approve_callback, run_loop
from custodyloop.runner import Runner


def _wrap(obj) -> str:
    return f"===JSON===\n{json.dumps(obj)}\n===END==="


PLAN = _wrap(
    {
        "task": "create greeting.txt containing Hello World",
        "audience": "engineer",
        "artifact_type": "code_change",
        "steps": [
            {
                "id": "step_1",
                "objective": "create greeting.txt with the literal text 'Hello World'",
                "allowed_files": ["greeting.txt"],
                "forbidden_files": ["*.py", ".env"],
                "expected_output": "greeting.txt exists with 'Hello World'",
                "required_tests": ["test -f greeting.txt", "grep -q 'Hello World' greeting.txt"],
                "success_criteria": ["both test commands exit 0"],
                "rollback_plan": "rm -f greeting.txt",
            }
        ],
        "anti_failure": {
            "artifact_compliance": "must produce file, not describe it",
            "no_hallucinated_specifics": "operator gave literal content",
            "audience_alignment": "engineer",
        },
        "notes": "ASSUMPTION: workdir is writable and empty",
    }
)


EXEC = _wrap(
    {
        "step_id": "step_1",
        "files_changed": [{"path": "greeting.txt", "added": 1, "removed": 0, "action": "created"}],
        "diff_summary": "Created greeting.txt with one line 'Hello World'.",
        "commands_executed": [
            {"cmd": "echo 'Hello World' > greeting.txt", "exit_code": 0, "stdout_summary": ""},
            {"cmd": "test -f greeting.txt", "exit_code": 0, "stdout_summary": ""},
            {"cmd": "grep -q 'Hello World' greeting.txt", "exit_code": 0, "stdout_summary": ""},
        ],
        "test_results": {"passed": 2, "failed": 0, "skipped": 0, "details": "both required tests passed"},
        "errors": [],
        "fixes_applied": [],
        "remaining_risks": "",
    }
)


VERDICT = _wrap(
    {
        "decision": "APPROVED",
        "reasons": ["file created", "all required tests ran with exit 0", "no scope creep"],
        "fix_instructions": "",
        "checks": {
            "execution_matched_plan": True,
            "changes_correct_minimal": True,
            "tests_actually_ran": True,
            "no_hallucinations": True,
            "no_scope_creep": True,
            "no_unsafe_actions": True,
            "artifact_compliance": True,
            "no_hallucinated_specifics": True,
            "audience_alignment": True,
        },
    }
)


FINAL = _wrap(
    {
        "summary": "A new file named greeting.txt was created in the workdir, containing the line 'Hello World'.",
        "why": "The operator asked for a file with that exact content.",
        "changes": ["Added greeting.txt with content 'Hello World'."],
        "risks_remaining": [],
        "next_steps": ["Optionally version-control the file."],
    }
)


class CannedRunner(Runner):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._claude_n = 0

    def run(self, model, prompt, *, workdir=None, timeout=600):
        self.calls.append((model, prompt[:60].replace("\n", " ")))
        if model == "claude":
            self._claude_n += 1
            return PLAN if self._claude_n == 1 else FINAL
        if model == "codex":
            if workdir is not None:
                (workdir / "greeting.txt").write_text("Hello World\n")
            return EXEC
        if model == "chatgpt":
            return VERDICT
        raise AssertionError(f"unexpected model {model!r}")


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="r6_dry_wd_"))
    art = Path(tempfile.mkdtemp(prefix="r6_dry_art_"))
    print(f"Workdir:    {work}")
    print(f"Artifacts:  {art}")
    print()

    runner = CannedRunner()
    result = run_loop(
        "create greeting.txt containing Hello World",
        runner=runner,
        workdir=work,
        approval_cb=auto_approve_callback,
        artifact_dir=art,
        max_retries=1,
    )

    print("=== Stage trace ===")
    for i, (model, head) in enumerate(runner.calls, 1):
        print(f"  {i}. {model:8s}  '{head}…'")
    print()

    print("=== Outcome ===")
    print(f"aborted:   {result.aborted}")
    if result.aborted:
        print(f"reason:    {result.abort_reason}")
        return 1
    assert result.final_report is not None
    print(f"steps:     {len(result.outcomes)}")
    for o in result.outcomes:
        print(f"  - {o.step_id}: {o.verdict.decision} ({o.iterations} iter)")
    print()

    print("=== Workdir contents ===")
    for p in sorted(work.iterdir()):
        print(f"  {p.name}: {p.read_text().rstrip()!r}")
    print()

    print("=== Final report ===")
    print(result.final_report.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
