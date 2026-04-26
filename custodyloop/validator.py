"""Stage 3 — GPT-5.5 validator.

The validator is the binding approval gate. Claude cannot override it.
Codex cannot self-approve. If REJECTED, the loop re-issues the step
to Codex with the validator's fix instructions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from .json_extract import extract_json
from .runner import Runner
from .types import (
    AttemptRecord,
    ExecutionReport,
    PlanPacket,
    PlanStep,
    StageOutcome,
    ValidatorVerdict,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text()


def _summarize_prior_outcomes(prior_outcomes: Sequence[StageOutcome]) -> str:
    """Compact prior-step trail for validator context.

    Returns a single string. Each prior step contributes:
      - step_id
      - the step's objective and allowed_files (so the validator knows scope)
      - every command that was run with its exit_code
      - test_results
      - diff_summary
    Nothing else — keep token cost low and remove noise.
    """
    if not prior_outcomes:
        return "(no prior steps in this run)"
    blocks = []
    for o in prior_outcomes:
        cmds = "\n".join(f"    [{c.exit_code}] {c.cmd}" for c in o.report.commands_executed) or "    (none)"
        files = ", ".join(f"{f.action}:{f.path}" for f in o.report.files_changed) or "(none)"
        blocks.append(
            f"--- {o.step_id} (verdict: {o.verdict.decision}, attempts: {o.iterations}) ---\n"
            f"  files_changed: {files}\n"
            f"  commands_executed:\n{cmds}\n"
            f"  test_results: passed={o.report.test_results.passed} "
            f"failed={o.report.test_results.failed} "
            f"skipped={o.report.test_results.skipped}\n"
            f"  diff_summary: {o.report.diff_summary[:300]}"
        )
    return "\n\n".join(blocks)


def _summarize_prior_attempts(prior_attempts: Sequence[AttemptRecord]) -> str:
    """Compact same-step retry trail for the validator.

    Each prior attempt contributes:
      - attempt number and verdict
      - rejection reasons (so the validator can verify the latest attempt
        addressed them)
      - files_changed (so the validator can see cumulative file edits)
      - every command + exit_code
      - test_results
      - diff_summary (truncated)

    The cumulative union of all attempts' files_changed is also stated
    explicitly so the validator does not need to compute it.
    """
    if not prior_attempts:
        return "(no prior attempts at this step — this is the first attempt)"

    blocks = []
    for a in prior_attempts:
        cmds = "\n".join(f"      [{c.exit_code}] {c.cmd}" for c in a.report.commands_executed) or "      (none)"
        files = ", ".join(f"{f.action}:{f.path}" for f in a.report.files_changed) or "(none)"
        reasons = "; ".join(a.verdict.reasons) or "(no reasons given)"
        blocks.append(
            f"--- attempt {a.attempt} (verdict: {a.verdict.decision}) ---\n"
            f"    rejection_reasons: {reasons}\n"
            f"    files_changed: {files}\n"
            f"    commands_executed:\n{cmds}\n"
            f"    test_results: passed={a.report.test_results.passed} "
            f"failed={a.report.test_results.failed} "
            f"skipped={a.report.test_results.skipped}\n"
            f"    diff_summary: {a.report.diff_summary[:300]}"
        )

    # Cumulative union of files across all prior attempts.
    union: dict[str, str] = {}
    for a in prior_attempts:
        for fc in a.report.files_changed:
            # Last-write-wins on action — created → modified → deleted ordering matters
            # less than just signaling that the path was touched in some attempt.
            union[fc.path] = fc.action
    union_str = ", ".join(f"{action}:{path}" for path, action in sorted(union.items())) or "(none)"

    blocks.append(f"--- cumulative union across prior attempts ---\n    files_touched: {union_str}")
    return "\n\n".join(blocks)


def build_validator_prompt(
    plan_packet: PlanPacket,
    step: PlanStep,
    report: ExecutionReport,
    prior_outcomes: Optional[Sequence[StageOutcome]] = None,
    prior_attempts: Optional[Sequence[AttemptRecord]] = None,
) -> str:
    template = _load("validator")
    return (
        template.replace("<PLAN_PACKET>", plan_packet.to_json())
        .replace("<STEP>", json.dumps(step.__dict__, indent=2, default=str))
        .replace("<EXECUTION_REPORT>", report.to_json())
        .replace("<PRIOR_OUTCOMES>", _summarize_prior_outcomes(prior_outcomes or ()))
        .replace("<PRIOR_ATTEMPTS>", _summarize_prior_attempts(prior_attempts or ()))
    )


def validate_step(
    plan_packet: PlanPacket,
    step: PlanStep,
    report: ExecutionReport,
    runner: Runner,
    *,
    timeout: int = 600,
    prior_outcomes: Optional[Sequence[StageOutcome]] = None,
    prior_attempts: Optional[Sequence[AttemptRecord]] = None,
) -> ValidatorVerdict:
    """Invoke GPT-5.5 validator. Returns binding verdict.

    `prior_outcomes` is the list of already-approved StageOutcomes from
    earlier steps in the same run.

    `prior_attempts` is the list of REJECTED earlier attempts at the
    current step. Validators consult it to judge the cumulative outcome
    across retries — a latest-attempt files_changed=[] is acceptable
    when an earlier attempt already produced the required edits and the
    latest attempt's tests pass against the cumulative state.
    """
    prompt = build_validator_prompt(
        plan_packet,
        step,
        report,
        prior_outcomes=prior_outcomes,
        prior_attempts=prior_attempts,
    )
    raw = runner.run("chatgpt", prompt, timeout=timeout)
    obj = extract_json(raw)
    return ValidatorVerdict.from_dict(obj)
