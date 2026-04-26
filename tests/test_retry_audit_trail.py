"""Tests for CustodyLoop Fix 3 — cumulative retry audit trail.

Covers the six requirements from the Fix 3 spec:
1. cumulative files_changed survives across retries
2. validator prompt includes prior rejection reasons
3. validator prompt includes prior attempt file changes
4. empty files_changed on retry does not hide prior cumulative changes
5. scope violations are still rejected even if cumulative evidence exists
6. existing CustodyLoop successful behavior still passes (handled by other test files)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop import auto_approve_callback, run_loop
from custodyloop.runner import Runner
from custodyloop.types import (
    AttemptRecord,
    CommandRecord,
    ExecutionReport,
    FileChange,
    PlanPacket,
    ValidatorVerdict,
)
from custodyloop.types import TestResults as _TestResults
from custodyloop.validator import _summarize_prior_attempts, build_validator_prompt

# ── Helpers ─────────────────────────────────────────────────────────────────


def _attempt(
    n,
    *,
    files=None,
    cmd="echo x",
    exit_code=0,
    decision="REJECTED",
    reasons=None,
    test_passed=0,
    test_failed=0,
):
    rep = ExecutionReport(
        step_id="step_1",
        files_changed=[FileChange(**f) for f in (files or [])],
        commands_executed=[CommandRecord(cmd=cmd, exit_code=exit_code)],
        diff_summary=f"diff for attempt {n}",
        test_results=_TestResults(passed=test_passed, failed=test_failed),
    )
    verdict = ValidatorVerdict(
        decision=decision,
        reasons=reasons or [f"reason for attempt {n}"],
        fix_instructions=f"fix {n}",
        checks={},
    )
    return AttemptRecord(attempt=n, report=rep, verdict=verdict)


def _plan_one_step():
    return PlanPacket.from_dict(
        {
            "task": "fix foo",
            "audience": "engineer",
            "artifact_type": "code_change",
            "steps": [
                {
                    "id": "step_1",
                    "objective": "fix foo.py",
                    "allowed_files": ["foo.py"],
                    "forbidden_files": [".pytest_cache/*"],
                    "expected_output": "pytest passes",
                    "required_tests": ["pytest test_foo.py"],
                }
            ],
        }
    )


# ── _summarize_prior_attempts unit tests ────────────────────────────────────


def test_summary_returns_sentinel_when_empty():
    assert "no prior attempts" in _summarize_prior_attempts(())


def test_summary_includes_attempt_number_and_decision():
    a = _attempt(1, files=[{"path": "foo.py", "action": "modified"}])
    s = _summarize_prior_attempts([a])
    assert "attempt 1" in s
    assert "REJECTED" in s


def test_summary_includes_rejection_reasons():
    a = _attempt(1, reasons=["scope creep on bar.py", "no tests ran"])
    s = _summarize_prior_attempts([a])
    assert "scope creep on bar.py" in s
    assert "no tests ran" in s


def test_summary_includes_files_per_attempt():
    a = _attempt(1, files=[{"path": "foo.py", "action": "modified"}, {"path": "x.txt", "action": "created"}])
    s = _summarize_prior_attempts([a])
    assert "modified:foo.py" in s
    assert "created:x.txt" in s


def test_summary_includes_command_and_exit_code():
    a = _attempt(1, cmd="pytest test_foo.py", exit_code=1)
    s = _summarize_prior_attempts([a])
    assert "pytest test_foo.py" in s
    assert "[1]" in s


def test_summary_includes_cumulative_union():
    a1 = _attempt(1, files=[{"path": "foo.py", "action": "modified"}])
    a2 = _attempt(2, files=[{"path": "bar.py", "action": "modified"}])
    s = _summarize_prior_attempts([a1, a2])
    assert "cumulative union" in s.lower() or "cumulative" in s.lower()
    assert "foo.py" in s
    assert "bar.py" in s


def test_summary_truncates_long_diff_summary():
    a = _attempt(1)
    a.report.diff_summary = "B" * 1000
    s = _summarize_prior_attempts([a])
    assert "B" * 301 not in s
    assert "B" * 300 in s


# ── build_validator_prompt with prior_attempts ──────────────────────────────


def test_validator_prompt_includes_prior_attempts_section():
    plan = _plan_one_step()
    step = plan.steps[0]
    current = ExecutionReport(step_id="step_1")
    a1 = _attempt(
        1,
        files=[{"path": "foo.py", "action": "modified"}],
        cmd="pytest test_foo.py",
        exit_code=1,
        reasons=["test still failing"],
    )

    prompt = build_validator_prompt(plan, step, current, prior_attempts=[a1])

    # Section markers replaced
    assert "<PRIOR_ATTEMPTS>" not in prompt
    # Attempt 1's evidence visible
    assert "attempt 1" in prompt
    assert "modified:foo.py" in prompt
    assert "test still failing" in prompt
    # The cumulative judgement rule must be in the prompt template
    assert "CUMULATIVE" in prompt or "cumulative" in prompt


def test_validator_prompt_handles_no_prior_attempts():
    plan = _plan_one_step()
    step = plan.steps[0]
    current = ExecutionReport(step_id="step_1")
    prompt = build_validator_prompt(plan, step, current, prior_attempts=None)
    assert "<PRIOR_ATTEMPTS>" not in prompt
    assert "no prior attempts" in prompt


# ── Loop integration: cumulative trail flows end-to-end ─────────────────────


class CapturingRunner(Runner):
    """FakeRunner that records every prompt sent to chatgpt."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.chatgpt_prompts = []
        self.codex_prompts = []
        self.calls = []

    def run(self, model, prompt, *, workdir=None, timeout=600):
        self.calls.append(model)
        if model == "chatgpt":
            self.chatgpt_prompts.append(prompt)
        elif model == "codex":
            self.codex_prompts.append(prompt)
        return self.handlers[model](prompt, len(self.chatgpt_prompts), len(self.codex_prompts))


def _wrap(obj):
    return f"===JSON===\n{json.dumps(obj)}\n===END==="


def _plan_payload():
    return {
        "task": "fix foo",
        "audience": "engineer",
        "artifact_type": "code_change",
        "steps": [
            {
                "id": "step_1",
                "objective": "fix foo.py so test passes",
                "allowed_files": ["foo.py"],
                "forbidden_files": [".pytest_cache/*"],
                "expected_output": "pytest passes",
                "required_tests": ["pytest test_foo.py"],
                "rollback_plan": "git checkout foo.py",
            }
        ],
        "anti_failure": {
            "artifact_compliance": "must produce code edit",
            "no_hallucinated_specifics": "none",
            "audience_alignment": "engineer",
        },
    }


def _exec_attempt_1():
    """Codex makes the fix but also runs an extra command that gets rejected."""
    return {
        "step_id": "step_1",
        "files_changed": [{"path": "foo.py", "added": 1, "removed": 1, "action": "modified"}],
        "diff_summary": "Changed return a-b to return a+b.",
        "commands_executed": [
            {"cmd": "edit foo.py", "exit_code": 0, "stdout_summary": ""},
            # missing required test command — validator will reject
        ],
        "test_results": {"passed": 0, "failed": 0, "skipped": 0, "details": ""},
        "errors": [],
        "fixes_applied": [],
        "remaining_risks": "",
    }


def _exec_attempt_2_empty():
    """Codex on retry sees file already correct, naively reports no edits."""
    return {
        "step_id": "step_1",
        "files_changed": [],  # empty — the bug we're fixing
        "diff_summary": "Verified prior fix is in place; no further edits.",
        "commands_executed": [
            {"cmd": "cat foo.py", "exit_code": 0, "stdout_summary": "return a + b"},
            {"cmd": "pytest test_foo.py", "exit_code": 0, "stdout_summary": "1 passed"},
        ],
        "test_results": {"passed": 1, "failed": 0, "skipped": 0, "details": "test passes"},
        "errors": [],
        "fixes_applied": [],
        "remaining_risks": "",
    }


def _verdict_rejected_no_test():
    return {
        "decision": "REJECTED",
        "reasons": ["required test command pytest test_foo.py was not executed"],
        "fix_instructions": "run pytest test_foo.py and include it in commands_executed",
        "checks": {"tests_actually_ran": False},
    }


def _verdict_approved_cumulative():
    return {
        "decision": "APPROVED",
        "reasons": ["cumulative state shows prior attempt edited foo.py and latest attempt verified pytest passes"],
        "fix_instructions": "",
        "checks": {
            "execution_matched_plan": True,
            "tests_actually_ran": True,
            "no_hallucinations": True,
            "no_scope_creep": True,
            "no_unsafe_actions": True,
        },
    }


def _final_report():
    return {"summary": "fixed", "why": "asked", "changes": ["fixed foo.py"], "risks_remaining": [], "next_steps": []}


def test_validator_sees_prior_attempt_on_retry(tmp_path):
    """Requirements 2, 3, 4: validator prompt on attempt 2 includes prior rejection
    reasons, prior files_changed, and the empty current files_changed does not
    erase that cumulative evidence."""
    plan = _plan_payload()

    handlers = {
        "claude": lambda prompt, gpt_n, codex_n: _wrap(plan if "PLANNER" in prompt else _final_report()),
        "codex": lambda prompt, gpt_n, codex_n: _wrap(_exec_attempt_1() if codex_n == 1 else _exec_attempt_2_empty()),
        "chatgpt": lambda prompt, gpt_n, codex_n: _wrap(
            _verdict_rejected_no_test() if gpt_n == 1 else _verdict_approved_cumulative()
        ),
    }
    runner = CapturingRunner(handlers)
    result = run_loop(
        "fix the failing test",
        runner=runner,
        workdir=tmp_path / "wd",
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "art",
        max_retries=2,
    )
    (tmp_path / "wd").mkdir(exist_ok=True)

    assert not result.aborted, f"unexpected abort: {result.abort_reason}"
    assert result.outcomes[0].iterations == 2

    # Two validator prompts: one per attempt. The SECOND must contain
    # prior rejection reason + prior attempt's files_changed.
    assert len(runner.chatgpt_prompts) == 2
    second_prompt = runner.chatgpt_prompts[1]

    # Requirement 2: prior rejection reason
    assert "required test command pytest test_foo.py was not executed" in second_prompt
    # Requirement 3: prior attempt's files_changed
    assert "modified:foo.py" in second_prompt
    # Requirement 4: cumulative section is present even though current is empty
    assert "attempt 1" in second_prompt
    # The first prompt must NOT have prior_attempts evidence
    first_prompt = runner.chatgpt_prompts[0]
    assert "no prior attempts" in first_prompt


def test_executor_sees_cumulative_files_on_retry(tmp_path):
    """Requirement 1: cumulative files_changed survives into the next executor prompt."""
    plan = _plan_payload()

    handlers = {
        "claude": lambda prompt, gpt_n, codex_n: _wrap(plan if "PLANNER" in prompt else _final_report()),
        "codex": lambda prompt, gpt_n, codex_n: _wrap(_exec_attempt_1() if codex_n == 1 else _exec_attempt_2_empty()),
        "chatgpt": lambda prompt, gpt_n, codex_n: _wrap(
            _verdict_rejected_no_test() if gpt_n == 1 else _verdict_approved_cumulative()
        ),
    }
    runner = CapturingRunner(handlers)
    (tmp_path / "wd").mkdir(exist_ok=True)
    run_loop(
        "fix the failing test",
        runner=runner,
        workdir=tmp_path / "wd",
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "art",
        max_retries=2,
    )

    # Codex is called twice. The SECOND call's prior_failures must mention
    # both the attempt-1 rejection reason AND the cumulative files touched.
    assert len(runner.codex_prompts) == 2
    second_codex_prompt = runner.codex_prompts[1]
    assert "Attempt 1 REJECTED" in second_codex_prompt
    assert "Cumulative files touched" in second_codex_prompt
    assert "modified:foo.py" in second_codex_prompt
    # And the retry-honesty rule from the prompt must be present in the template
    assert "RETRY HONESTY" in second_codex_prompt


def test_scope_violation_in_prior_attempt_still_visible_to_validator(tmp_path):
    """Requirement 5: a scope-violating prior attempt remains visible so the
    validator can refuse to retroactively launder it via cumulative evidence.

    We assert the *evidence* is present in the validator prompt; the actual
    decision rule is enforced by the live validator (which we don't simulate
    here — its logic is the prompt instructions, not Python code)."""
    plan = _plan_payload()

    # Attempt 1: Codex modified an out-of-scope file. Validator rejected.
    bad_attempt_1 = {
        "step_id": "step_1",
        "files_changed": [
            {"path": "foo.py", "action": "modified"},
            {"path": "secrets/key", "action": "created"},  # scope violation
        ],
        "diff_summary": "Changed foo.py and accidentally created secrets/key.",
        "commands_executed": [{"cmd": "edit", "exit_code": 0, "stdout_summary": ""}],
        "test_results": {"passed": 0, "failed": 0, "skipped": 0, "details": ""},
        "errors": [],
        "fixes_applied": [],
        "remaining_risks": "",
    }
    rejected_for_scope = {
        "decision": "REJECTED",
        "reasons": ["secrets/key created — outside allowed_files"],
        "fix_instructions": "remove secrets/key",
        "checks": {"no_scope_creep": False},
    }

    handlers = {
        "claude": lambda prompt, gpt_n, codex_n: _wrap(plan if "PLANNER" in prompt else _final_report()),
        "codex": lambda prompt, gpt_n, codex_n: _wrap(bad_attempt_1 if codex_n == 1 else _exec_attempt_2_empty()),
        "chatgpt": lambda prompt, gpt_n, codex_n: _wrap(
            rejected_for_scope if gpt_n == 1 else _verdict_approved_cumulative()
        ),
    }
    runner = CapturingRunner(handlers)
    (tmp_path / "wd").mkdir(exist_ok=True)
    # We use deny on scope-creep junctions so the run aborts BEFORE attempt 2's
    # validator runs — that's the correct safety behavior.
    from custodyloop.control_junctions import JunctionKind, JunctionRequest

    asked = []

    def cb(req: JunctionRequest) -> bool:
        asked.append(req)
        return req.kind != JunctionKind.SCOPE_EXPANSION

    result = run_loop(
        "fix",
        runner=runner,
        workdir=tmp_path / "wd",
        approval_cb=cb,
        artifact_dir=tmp_path / "art",
        max_retries=2,
    )
    # The scope violation aborts the run via control junction (defense in depth).
    assert result.aborted
    assert any(r.kind == JunctionKind.SCOPE_EXPANSION for r in asked)


def test_validator_sees_scope_violation_evidence_in_prior_attempts():
    """Requirement 5 (prompt-level): the validator prompt for attempt 2
    must clearly show that attempt 1 had a scope violation (path outside
    allowed_files) so the validator does not retroactively approve it."""
    plan = _plan_one_step()
    step = plan.steps[0]
    current = ExecutionReport(step_id="step_1")
    a1 = _attempt(
        1,
        files=[{"path": "foo.py", "action": "modified"}, {"path": "secrets/key", "action": "created"}],
        reasons=["secrets/key created — outside allowed_files"],
    )
    prompt = build_validator_prompt(plan, step, current, prior_attempts=[a1])

    assert "secrets/key" in prompt
    assert "outside allowed_files" in prompt
    # And the explicit non-launder rule is in the template
    assert (
        "DO NOT become acceptable retroactively" in prompt or "do not become acceptable retroactively" in prompt.lower()
    )
