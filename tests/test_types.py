"""Tests for custodyloop/types.py — strict schema dataclasses."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop.types import (
    ExecutionReport,
    FinalReport,
    PlanPacket,
    PlanStep,
    ValidatorVerdict,
)

# ── PlanStep / PlanPacket ──────────────────────────────────────────────────


def test_planstep_requires_id_and_objective():
    with pytest.raises(ValueError):
        PlanStep.from_dict({"objective": "x"})
    with pytest.raises(ValueError):
        PlanStep.from_dict({"id": "x"})


def test_planstep_defaults():
    s = PlanStep.from_dict({"id": "s1", "objective": "do thing"})
    assert s.allowed_files == []
    assert s.required_tests == []
    assert s.rollback_plan == ""


def test_planpacket_requires_steps():
    with pytest.raises(ValueError):
        PlanPacket.from_dict({"task": "t", "steps": []})
    with pytest.raises(ValueError):
        PlanPacket.from_dict({"task": "t"})


def test_planpacket_roundtrip():
    p = PlanPacket.from_dict(
        {
            "task": "do x",
            "audience": "engineer",
            "artifact_type": "code_change",
            "steps": [{"id": "s1", "objective": "obj1"}],
        }
    )
    j = p.to_json()
    assert "do x" in j and "s1" in j
    p2 = PlanPacket.from_dict(p.to_dict())
    assert p2.task == p.task and len(p2.steps) == 1


# ── ExecutionReport ─────────────────────────────────────────────────────────


def test_execution_report_requires_step_id():
    with pytest.raises(ValueError):
        ExecutionReport.from_dict({})


def test_execution_report_parses_nested():
    r = ExecutionReport.from_dict(
        {
            "step_id": "s1",
            "files_changed": [{"path": "a.py", "added": 5, "removed": 1, "action": "modified"}],
            "commands_executed": [{"cmd": "pytest", "exit_code": 0, "stdout_summary": "ok"}],
            "test_results": {"passed": 3, "failed": 0, "skipped": 1, "details": "all good"},
            "errors": [],
            "remaining_risks": "low",
        }
    )
    assert r.files_changed[0].path == "a.py"
    assert r.commands_executed[0].exit_code == 0
    assert r.test_results.passed == 3
    assert r.remaining_risks == "low"


def test_execution_report_handles_missing_optionals():
    r = ExecutionReport.from_dict({"step_id": "s1"})
    assert r.files_changed == []
    assert r.test_results.passed == 0


# ── ValidatorVerdict ─────────────────────────────────────────────────────────


def test_verdict_only_accepts_canonical_decisions():
    with pytest.raises(ValueError):
        ValidatorVerdict.from_dict({"decision": "maybe"})
    with pytest.raises(ValueError):
        ValidatorVerdict.from_dict({"decision": ""})


def test_verdict_normalizes_case():
    v = ValidatorVerdict.from_dict({"decision": "approved"})
    assert v.decision == "APPROVED"
    assert v.approved is True


def test_verdict_rejected():
    v = ValidatorVerdict.from_dict(
        {
            "decision": "REJECTED",
            "reasons": ["bad"],
            "fix_instructions": "do better",
            "checks": {"tests_actually_ran": False},
        }
    )
    assert v.approved is False
    assert v.fix_instructions == "do better"
    assert v.checks["tests_actually_ran"] is False


# ── FinalReport ──────────────────────────────────────────────────────────────


def test_final_report_to_markdown():
    fr = FinalReport.from_dict(
        {
            "summary": "done",
            "why": "needed",
            "changes": ["x", "y"],
            "risks_remaining": ["r1"],
            "next_steps": ["n1"],
        }
    )
    md = fr.to_markdown()
    assert "# Final Report" in md
    assert "## Summary" in md and "done" in md
    assert "- x" in md and "- r1" in md and "- n1" in md


def test_final_report_requires_summary():
    with pytest.raises(ValueError):
        FinalReport.from_dict({"why": "x"})
