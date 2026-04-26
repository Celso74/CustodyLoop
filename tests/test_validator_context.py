"""Tests for validator prior-outcome context (Fix 2 from CustodyLoop live validation).

Two assertions:
1. _summarize_prior_outcomes packs prior steps into a compact, parseable string.
2. validate_step's prompt actually contains prior-step commands and exit codes
   so the validator can verify a fix step addressed earlier evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop.types import (
    CommandRecord,
    ExecutionReport,
    FileChange,
    PlanPacket,
    StageOutcome,
    ValidatorVerdict,
)
from custodyloop.types import TestResults as _TestResults
from custodyloop.validator import _summarize_prior_outcomes, build_validator_prompt


def _outcome(step_id, *, cmd, exit_code, files=None) -> StageOutcome:
    rep = ExecutionReport(
        step_id=step_id,
        files_changed=[FileChange(**f) for f in (files or [])],
        commands_executed=[CommandRecord(cmd=cmd, exit_code=exit_code)],
        diff_summary=f"diff for {step_id}",
        test_results=_TestResults(passed=0, failed=1 if exit_code else 0),
    )
    verdict = ValidatorVerdict(decision="APPROVED", reasons=[], fix_instructions="", checks={})
    return StageOutcome(step_id=step_id, report=rep, verdict=verdict, iterations=1)


def _plan_with_steps(*ids):
    return PlanPacket.from_dict(
        {
            "task": "t",
            "audience": "engineer",
            "artifact_type": "code_change",
            "steps": [{"id": i, "objective": f"obj {i}", "allowed_files": ["foo.py"]} for i in ids],
        }
    )


# ── _summarize_prior_outcomes ────────────────────────────────────────────────


def test_summary_empty_when_no_prior():
    assert "no prior steps" in _summarize_prior_outcomes(())


def test_summary_includes_step_id_and_command():
    o = _outcome("step_1", cmd="pytest test_foo.py", exit_code=1)
    s = _summarize_prior_outcomes([o])
    assert "step_1" in s
    assert "pytest test_foo.py" in s
    assert "[1]" in s  # exit code surfaced


def test_summary_includes_file_actions():
    o = _outcome(
        "step_1",
        cmd="cat foo.py",
        exit_code=0,
        files=[{"path": "foo.py", "action": "modified"}],
    )
    s = _summarize_prior_outcomes([o])
    assert "modified:foo.py" in s


def test_summary_chains_multiple_prior_steps():
    a = _outcome("step_1", cmd="pytest", exit_code=1)
    b = _outcome("step_2", cmd="grep -r foo", exit_code=0)
    s = _summarize_prior_outcomes([a, b])
    assert "step_1" in s
    assert "step_2" in s
    assert s.index("step_1") < s.index("step_2")  # ordering preserved


# ── build_validator_prompt ───────────────────────────────────────────────────


def test_validator_prompt_includes_prior_evidence():
    plan = _plan_with_steps("step_1", "step_2")
    step = plan.steps[1]
    current_report = ExecutionReport(step_id="step_2")
    prior = _outcome("step_1", cmd="pytest test_foo.py", exit_code=1)

    prompt = build_validator_prompt(plan, step, current_report, [prior])

    # Prior failing test must be visible to the validator
    assert "pytest test_foo.py" in prompt
    assert "[1]" in prompt
    # The PRIOR_OUTCOMES marker must have been replaced
    assert "<PRIOR_OUTCOMES>" not in prompt
    # And the explanatory framing must be present
    assert "Prior approved steps" in prompt


def test_validator_prompt_handles_missing_prior():
    plan = _plan_with_steps("step_1")
    step = plan.steps[0]
    report = ExecutionReport(step_id="step_1")
    prompt = build_validator_prompt(plan, step, report, None)
    assert "<PRIOR_OUTCOMES>" not in prompt
    assert "no prior steps" in prompt


def test_validator_prompt_truncates_long_diff_summary():
    o = _outcome("step_1", cmd="x", exit_code=0)
    o.report.diff_summary = "A" * 1000
    s = _summarize_prior_outcomes([o])
    # Should clip to 300 chars max in summary
    assert "A" * 301 not in s
    assert "A" * 300 in s
