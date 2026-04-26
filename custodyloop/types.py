"""CustodyLoop schemas — strict, dataclass-backed contracts between stages.

Every stage produces structured output. Loose text is rejected.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class PlanStep:
    id: str
    objective: str
    allowed_files: List[str] = field(default_factory=list)
    forbidden_files: List[str] = field(default_factory=list)
    expected_output: str = ""
    required_tests: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    rollback_plan: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PlanStep:
        missing = [k for k in ("id", "objective") if k not in d]
        if missing:
            raise ValueError(f"PlanStep missing required keys: {missing}")
        return cls(
            id=str(d["id"]),
            objective=str(d["objective"]),
            allowed_files=list(d.get("allowed_files", []) or []),
            forbidden_files=list(d.get("forbidden_files", []) or []),
            expected_output=str(d.get("expected_output", "")),
            required_tests=list(d.get("required_tests", []) or []),
            success_criteria=list(d.get("success_criteria", []) or []),
            rollback_plan=str(d.get("rollback_plan", "")),
        )


@dataclass
class PlanPacket:
    task: str
    audience: str
    artifact_type: str
    steps: List[PlanStep]
    anti_failure: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PlanPacket:
        if "task" not in d or "steps" not in d:
            raise ValueError("PlanPacket requires 'task' and 'steps'")
        steps_raw = d["steps"]
        if not isinstance(steps_raw, list) or len(steps_raw) == 0:
            raise ValueError("PlanPacket.steps must be a non-empty list")
        steps = [PlanStep.from_dict(s) for s in steps_raw]
        return cls(
            task=str(d["task"]),
            audience=str(d.get("audience", "")),
            artifact_type=str(d.get("artifact_type", "")),
            steps=steps,
            anti_failure=dict(d.get("anti_failure", {}) or {}),
            notes=str(d.get("notes", "")),
        )


@dataclass
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    action: str = "modified"  # modified | created | deleted

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> FileChange:
        return cls(
            path=str(d.get("path", "")),
            added=int(d.get("added", 0) or 0),
            removed=int(d.get("removed", 0) or 0),
            action=str(d.get("action", "modified")),
        )


@dataclass
class CommandRecord:
    cmd: str
    exit_code: int = 0
    stdout_summary: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> CommandRecord:
        return cls(
            cmd=str(d.get("cmd", "")),
            exit_code=int(d.get("exit_code", 0) or 0),
            stdout_summary=str(d.get("stdout_summary", "")),
        )


@dataclass
class TestResults:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    details: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> TestResults:
        return cls(
            passed=int(d.get("passed", 0) or 0),
            failed=int(d.get("failed", 0) or 0),
            skipped=int(d.get("skipped", 0) or 0),
            details=str(d.get("details", "")),
        )


@dataclass
class ExecutionReport:
    step_id: str
    files_changed: List[FileChange] = field(default_factory=list)
    diff_summary: str = ""
    commands_executed: List[CommandRecord] = field(default_factory=list)
    test_results: TestResults = field(default_factory=TestResults)
    errors: List[str] = field(default_factory=list)
    fixes_applied: List[str] = field(default_factory=list)
    remaining_risks: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ExecutionReport:
        if "step_id" not in d:
            raise ValueError("ExecutionReport requires 'step_id'")
        return cls(
            step_id=str(d["step_id"]),
            files_changed=[FileChange.from_dict(x) for x in d.get("files_changed", []) or []],
            diff_summary=str(d.get("diff_summary", "")),
            commands_executed=[CommandRecord.from_dict(x) for x in d.get("commands_executed", []) or []],
            test_results=TestResults.from_dict(d.get("test_results", {}) or {}),
            errors=list(d.get("errors", []) or []),
            fixes_applied=list(d.get("fixes_applied", []) or []),
            remaining_risks=str(d.get("remaining_risks", "")),
        )


@dataclass
class ValidatorVerdict:
    decision: str  # APPROVED | REJECTED
    reasons: List[str] = field(default_factory=list)
    fix_instructions: str = ""
    checks: Dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> ValidatorVerdict:
        decision = str(d.get("decision", "")).strip().upper()
        if decision not in ("APPROVED", "REJECTED"):
            raise ValueError(f"ValidatorVerdict.decision must be APPROVED or REJECTED, got {decision!r}")
        return cls(
            decision=decision,
            reasons=list(d.get("reasons", []) or []),
            fix_instructions=str(d.get("fix_instructions", "")),
            checks=dict(d.get("checks", {}) or {}),
        )

    @property
    def approved(self) -> bool:
        return self.decision == "APPROVED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FinalReport:
    summary: str
    why: str
    changes: List[str] = field(default_factory=list)
    risks_remaining: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = ["# Final Report", "", "## Summary", self.summary, "", "## Why", self.why, ""]
        if self.changes:
            lines += ["## Changes"] + [f"- {c}" for c in self.changes] + [""]
        if self.risks_remaining:
            lines += ["## Risks Remaining"] + [f"- {r}" for r in self.risks_remaining] + [""]
        if self.next_steps:
            lines += ["## Next Steps"] + [f"- {n}" for n in self.next_steps] + [""]
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> FinalReport:
        if "summary" not in d:
            raise ValueError("FinalReport requires 'summary'")
        return cls(
            summary=str(d["summary"]),
            why=str(d.get("why", "")),
            changes=list(d.get("changes", []) or []),
            risks_remaining=list(d.get("risks_remaining", []) or []),
            next_steps=list(d.get("next_steps", []) or []),
        )


@dataclass
class StageOutcome:
    """Per-step outcome bundle."""

    step_id: str
    report: ExecutionReport
    verdict: ValidatorVerdict
    iterations: int = 1


@dataclass
class AttemptRecord:
    """One Codex attempt at a single step plus the validator's verdict on it.

    Carried forward across retries so the validator can judge the cumulative
    state of the step rather than only the latest attempt's report.
    """

    attempt: int  # 1-indexed
    report: ExecutionReport
    verdict: ValidatorVerdict
