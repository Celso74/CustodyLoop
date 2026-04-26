"""CustodyLoop integration dry-run.

Drives run_loop end-to-end with a deterministic FakeRunner that returns
plausible LLM outputs for a tiny safe task: 'create greeting.txt with
Hello World'. Verifies all artifacts are produced, junctions fire
correctly, and the final report is clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop import auto_approve_callback, run_loop
from custodyloop.runner import Runner


class CannedRunner(Runner):
    def __init__(self, files: Dict[str, str]):
        self._files = files
        self.log = []

    def run(self, model, prompt, *, workdir=None, timeout=600):
        self.log.append((model, prompt[:60].replace("\n", " ")))
        # Side effect: codex actually creates the file (simulating real Codex behavior).
        if model == "codex" and workdir is not None:
            (workdir / "greeting.txt").write_text("Hello World\n")
        return self._files[model]


def _wrap(obj) -> str:
    return f"===JSON===\n{json.dumps(obj)}\n===END==="


def _canned_payloads() -> Dict[str, str]:
    plan = {
        "task": "create greeting.txt containing Hello World",
        "audience": "engineer",
        "artifact_type": "code_change",
        "steps": [
            {
                "id": "step_1",
                "objective": "create greeting.txt with the literal contents 'Hello World'",
                "allowed_files": ["greeting.txt"],
                "forbidden_files": ["*"],
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
        "notes": "ASSUMPTION: workdir is writable",
    }
    exec_report = {
        "step_id": "step_1",
        "files_changed": [{"path": "greeting.txt", "added": 1, "removed": 0, "action": "created"}],
        "diff_summary": "Created greeting.txt with one line.",
        "commands_executed": [
            {"cmd": "echo 'Hello World' > greeting.txt", "exit_code": 0, "stdout_summary": ""},
            {"cmd": "test -f greeting.txt", "exit_code": 0, "stdout_summary": ""},
            {"cmd": "grep -q 'Hello World' greeting.txt", "exit_code": 0, "stdout_summary": ""},
        ],
        "test_results": {"passed": 2, "failed": 0, "skipped": 0, "details": "all required tests passed"},
        "errors": [],
        "fixes_applied": [],
        "remaining_risks": "",
    }
    verdict = {
        "decision": "APPROVED",
        "reasons": ["file created", "both required tests ran with exit 0"],
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
    final = {
        "summary": "A new file named greeting.txt was created in the workdir, containing the line 'Hello World'.",
        "why": "The operator asked for a file with that exact content.",
        "changes": ["Added greeting.txt with content 'Hello World'."],
        "risks_remaining": [],
        "next_steps": [],
    }

    # Two distinct claude responses: planner first, reporter last.
    return {
        "plan": _wrap(plan),
        "exec": _wrap(exec_report),
        "verdict": _wrap(verdict),
        "final": _wrap(final),
    }


class StatefulCannedRunner(Runner):
    def __init__(self):
        self.payloads = _canned_payloads()
        self.claude_calls = 0
        self.calls = []

    def run(self, model, prompt, *, workdir=None, timeout=600):
        self.calls.append(model)
        if model == "claude":
            self.claude_calls += 1
            return self.payloads["plan"] if self.claude_calls == 1 else self.payloads["final"]
        if model == "codex":
            if workdir is not None:
                (workdir / "greeting.txt").write_text("Hello World\n")
            return self.payloads["exec"]
        if model == "chatgpt":
            return self.payloads["verdict"]
        raise AssertionError(f"unexpected model {model}")


def test_dry_run_full_pipeline(tmp_path):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    artifact_dir = tmp_path / "artifacts"
    runner = StatefulCannedRunner()

    result = run_loop(
        "create greeting.txt containing Hello World",
        runner=runner,
        workdir=workdir,
        approval_cb=auto_approve_callback,
        artifact_dir=artifact_dir,
    )

    # Pipeline succeeded
    assert not result.aborted
    assert result.final_report is not None

    # Artifacts produced
    assert (artifact_dir / "PLAN_PACKET.json").exists()
    assert (artifact_dir / "step_1.execution_report.json").exists()
    assert (artifact_dir / "step_1.verdict.json").exists()
    assert (artifact_dir / "FINAL_REPORT.json").exists()
    assert (artifact_dir / "FINAL_REPORT.md").exists()

    # Real file written by simulated Codex
    assert (workdir / "greeting.txt").read_text().strip() == "Hello World"

    # Plan packet shape
    plan = json.loads((artifact_dir / "PLAN_PACKET.json").read_text())
    assert plan["artifact_type"] == "code_change"
    assert plan["steps"][0]["allowed_files"] == ["greeting.txt"]

    # Verdict was APPROVED with all checks true
    verdict = json.loads((artifact_dir / "step_1.verdict.json").read_text())
    assert verdict["decision"] == "APPROVED"
    assert all(verdict["checks"].values())

    # Final report has no internal vocabulary
    md = (artifact_dir / "FINAL_REPORT.md").read_text().lower()
    for bad in ("claude", "codex", "chatgpt", "validator", "planner", "executor"):
        assert bad not in md

    # Call ordering: plan → execute → validate → final
    assert runner.calls == ["claude", "codex", "chatgpt", "claude"]


def test_dry_run_records_iterations_on_retry(tmp_path):
    """Same dry run but verdict rejects once, then approves."""

    workdir = tmp_path / "wd"
    workdir.mkdir()
    artifact_dir = tmp_path / "artifacts"

    payloads = _canned_payloads()
    rejected = json.dumps(
        {
            "decision": "REJECTED",
            "reasons": ["tests didn't actually run"],
            "fix_instructions": "actually invoke the test commands",
            "checks": {"tests_actually_ran": False},
        }
    )

    class Runner2(Runner):
        def __init__(self):
            self.calls = []
            self.claude_n = 0
            self.gpt_n = 0

        def run(self, model, prompt, *, workdir=None, timeout=600):
            self.calls.append(model)
            if model == "claude":
                self.claude_n += 1
                return payloads["plan"] if self.claude_n == 1 else payloads["final"]
            if model == "codex":
                if workdir is not None:
                    (workdir / "greeting.txt").write_text("Hello World\n")
                return payloads["exec"]
            if model == "chatgpt":
                self.gpt_n += 1
                return f"===JSON===\n{rejected}\n===END===" if self.gpt_n == 1 else payloads["verdict"]
            raise AssertionError(model)

    runner = Runner2()
    result = run_loop(
        "create greeting.txt",
        runner=runner,
        workdir=workdir,
        approval_cb=auto_approve_callback,
        artifact_dir=artifact_dir,
        max_retries=2,
    )
    assert not result.aborted
    assert result.outcomes[0].iterations == 2
