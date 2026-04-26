"""Stage 4 — Claude final reporter.

Claude composes the operator-facing FinalReport from approved
StageOutcomes. Strips internal model attributions and CustodyLoop-internal
vocabulary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from .json_extract import extract_json
from .runner import Runner
from .types import FinalReport, PlanPacket, StageOutcome

PROMPTS_DIR = Path(__file__).parent / "prompts"

_FORBIDDEN_TERMS = [
    "claude",
    "codex",
    "gpt-5.5",
    "gpt5.5",
    "chatgpt",
    "validator",
    "planner",
    "executor",
    "scaffolding",
    "===final answer===",
    "===json===",
    "===end===",
    "return only the json",
]


def _load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text()


def build_reporter_prompt(
    task: str,
    plan_packet: PlanPacket,
    outcomes: List[StageOutcome],
    audience: str,
) -> str:
    template = _load("reporter")
    outcomes_payload = [
        {
            "step_id": o.step_id,
            "report": o.report.to_dict(),
            "verdict": o.verdict.to_dict(),
            "iterations": o.iterations,
        }
        for o in outcomes
    ]
    return (
        template.replace("<TASK>", task.strip())
        .replace("<PLAN_PACKET>", plan_packet.to_json())
        .replace("<OUTCOMES>", json.dumps(outcomes_payload, indent=2, default=str))
        .replace("<AUDIENCE>", audience.strip() or "operator")
    )


def sanitize_report(report: FinalReport) -> FinalReport:
    """Strip internal vocabulary from a FinalReport in place; returns a new instance."""

    def clean(s: str) -> str:
        out = s
        for term in _FORBIDDEN_TERMS:
            out = re.sub(re.escape(term), "[redacted]", out, flags=re.IGNORECASE)
        return out

    return FinalReport(
        summary=clean(report.summary),
        why=clean(report.why),
        changes=[clean(c) for c in report.changes],
        risks_remaining=[clean(r) for r in report.risks_remaining],
        next_steps=[clean(n) for n in report.next_steps],
    )


def produce_final_report(
    task: str,
    plan_packet: PlanPacket,
    outcomes: List[StageOutcome],
    runner: Runner,
    *,
    timeout: int = 600,
) -> FinalReport:
    audience = plan_packet.audience or "operator"
    prompt = build_reporter_prompt(task, plan_packet, outcomes, audience)
    raw = runner.run("claude", prompt, timeout=timeout)
    obj = extract_json(raw)
    report = FinalReport.from_dict(obj)
    return sanitize_report(report)
