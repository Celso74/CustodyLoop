"""Stage 2 — Codex executor.

Codex performs the actual file/command/test work. It runs with the
caller-supplied workdir; the wrapper script (call_codex.sh) confines
operations there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .json_extract import extract_json
from .runner import Runner
from .types import ExecutionReport, PlanPacket, PlanStep

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text()


def build_executor_prompt(
    plan_packet: PlanPacket,
    step: PlanStep,
    workdir: Optional[Path],
    prior_failures: Optional[str] = None,
) -> str:
    template = _load("executor")
    return (
        template.replace("<PLAN_PACKET>", plan_packet.to_json())
        .replace("<STEP>", json.dumps(step.__dict__, indent=2, default=str))
        .replace("<WORKDIR>", str(workdir) if workdir else "(no workdir — advisory step)")
        .replace("<PRIOR_FAILURES>", prior_failures or "(none — first attempt)")
    )


def execute_step(
    plan_packet: PlanPacket,
    step: PlanStep,
    runner: Runner,
    *,
    workdir: Optional[Path] = None,
    timeout: int = 1200,
    prior_failures: Optional[str] = None,
) -> ExecutionReport:
    """Run Codex against one step. Returns parsed ExecutionReport.

    Raises ValueError on malformed output (caller can retry).
    """
    prompt = build_executor_prompt(plan_packet, step, workdir, prior_failures)
    raw = runner.run("codex", prompt, workdir=workdir, timeout=timeout)
    obj = extract_json(raw)
    report = ExecutionReport.from_dict(obj)
    if report.step_id != step.id:
        raise ValueError(f"Executor returned wrong step_id: expected {step.id!r}, got {report.step_id!r}")
    return report
