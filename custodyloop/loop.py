"""CustodyLoop main loop wiring.

Sequence:
  1. Plan (Claude) → PlanPacket
  2. For each step:
       a. Pause for first-live-mod junction (once, before step 1 if it modifies files)
       b. Codex execute → ExecutionReport
       c. Post-exec junctions: destructive / scope_creep / commit / push / sensitive
       d. GPT-5.5 validate → Verdict
       e. If REJECTED → re-execute with fix instructions (max retries)
  3. Final report (Claude) → FinalReport
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .control_junctions import (
    ApprovalCallback,
    JunctionRequest,
    collect_post_execution_junctions,
    first_live_modification_request,
    stdin_approval_callback,
)
from .executor import execute_step
from .planner import capabilities_to_json, detect_workdir_capabilities, produce_plan
from .reporter import produce_final_report
from .runner import Runner, ShellRunner
from .types import (
    AttemptRecord,
    ExecutionReport,
    FinalReport,
    PlanPacket,
    PlanStep,
    StageOutcome,
    ValidatorVerdict,
)
from .validator import validate_step

logger = logging.getLogger("custodyloop")


@dataclass
class LoopResult:
    plan: PlanPacket
    outcomes: List[StageOutcome] = field(default_factory=list)
    final_report: Optional[FinalReport] = None
    aborted: bool = False
    abort_reason: str = ""
    artifact_paths: dict = field(default_factory=dict)


class LoopAborted(RuntimeError):
    pass


def _step_modifies_files(step: PlanStep) -> bool:
    return bool(step.allowed_files)


def _ask(approval_cb: ApprovalCallback, req: JunctionRequest) -> None:
    logger.info("Control junction: %s", req)
    if not approval_cb(req):
        raise LoopAborted(f"Operator denied control junction: {req}")


def run_loop(
    task: str,
    *,
    runner: Optional[Runner] = None,
    workdir: Optional[Path] = None,
    approval_cb: Optional[ApprovalCallback] = None,
    artifact_dir: Optional[Path] = None,
    max_retries: int = 2,
    plan_timeout: int = 600,
    exec_timeout: int = 1200,
    validate_timeout: int = 600,
    report_timeout: int = 600,
) -> LoopResult:
    """Run the full CustodyLoop pipeline.

    On any operator denial → returns LoopResult with aborted=True.
    On any unrecoverable malformed model output → raises ValueError.
    """
    if runner is None:
        runner = ShellRunner()
    if approval_cb is None:
        approval_cb = stdin_approval_callback

    artifact_dir = _prepare_artifact_dir(artifact_dir)
    artifact_paths = {"artifact_dir": str(artifact_dir)}

    # ── Stage 1: Plan ─────────────────────────────────────────────────────────
    logger.info("CustodyLoop stage 1: planning")
    capabilities = detect_workdir_capabilities(workdir)
    caps_path = artifact_dir / "WORKDIR_CAPABILITIES.json"
    caps_path.write_text(capabilities_to_json(capabilities))
    artifact_paths["capabilities"] = str(caps_path)
    logger.info(
        "Workdir capabilities: is_git_repo=%s python=%s pytest=%s",
        capabilities["is_git_repo"],
        capabilities["python_cmd"],
        capabilities["has_pytest"],
    )

    plan = produce_plan(task, runner, workdir=workdir, timeout=plan_timeout)
    plan_path = artifact_dir / "PLAN_PACKET.json"
    plan_path.write_text(plan.to_json())
    artifact_paths["plan"] = str(plan_path)

    result = LoopResult(plan=plan, artifact_paths=artifact_paths)

    # ── Stage 2: per-step execute → validate loop ────────────────────────────
    first_live_done = False
    try:
        for idx, step in enumerate(plan.steps):
            logger.info("CustodyLoop step %d/%d: %s", idx + 1, len(plan.steps), step.id)

            # First live modification junction: ask once, before the first
            # step that has an allowed_files whitelist.
            if not first_live_done and _step_modifies_files(step):
                _ask(approval_cb, first_live_modification_request(step))
                first_live_done = True

            outcome = _run_step_with_retries(
                plan,
                step,
                runner,
                workdir,
                approval_cb,
                prior_outcomes=tuple(result.outcomes),
                exec_timeout=exec_timeout,
                validate_timeout=validate_timeout,
                max_retries=max_retries,
            )
            result.outcomes.append(outcome)

            # Persist per-step artifacts
            (artifact_dir / f"{step.id}.execution_report.json").write_text(outcome.report.to_json())
            (artifact_dir / f"{step.id}.verdict.json").write_text(json.dumps(outcome.verdict.to_dict(), indent=2))

            if not outcome.verdict.approved:
                raise LoopAborted(
                    f"Step {step.id} not approved after {max_retries} retries: {'; '.join(outcome.verdict.reasons)}"
                )

    except LoopAborted as exc:
        result.aborted = True
        result.abort_reason = str(exc)
        logger.warning("CustodyLoop aborted: %s", exc)
        return result

    # ── Stage 3: Final report ────────────────────────────────────────────────
    logger.info("CustodyLoop stage 3: final report")
    final = produce_final_report(task, plan, result.outcomes, runner, timeout=report_timeout)
    final_path_json = artifact_dir / "FINAL_REPORT.json"
    final_path_md = artifact_dir / "FINAL_REPORT.md"
    final_path_json.write_text(json.dumps(final.to_dict(), indent=2))
    final_path_md.write_text(final.to_markdown())
    result.final_report = final
    result.artifact_paths["final_report_json"] = str(final_path_json)
    result.artifact_paths["final_report_md"] = str(final_path_md)
    return result


def _run_step_with_retries(
    plan: PlanPacket,
    step: PlanStep,
    runner: Runner,
    workdir: Optional[Path],
    approval_cb: ApprovalCallback,
    *,
    prior_outcomes: tuple = (),
    exec_timeout: int,
    validate_timeout: int,
    max_retries: int,
) -> StageOutcome:
    """Drive Codex→GPT-5.5 loop for a single step until approved or retries exhausted.

    Prior REJECTED attempts at this step are accumulated and passed to both
    Codex (so it knows the file may already be in the desired state) and
    GPT-5.5 (so it judges the cumulative step outcome, not only the latest).
    """
    prior_failures = ""
    prior_attempts: List[AttemptRecord] = []
    last_report: Optional[ExecutionReport] = None
    last_verdict: Optional[ValidatorVerdict] = None

    for attempt in range(1, max_retries + 2):  # 1 baseline + max_retries
        report = execute_step(
            plan,
            step,
            runner,
            workdir=workdir,
            timeout=exec_timeout,
            prior_failures=prior_failures or None,
        )
        last_report = report

        # Post-execution junctions — operator decides before the verdict matters
        for req in collect_post_execution_junctions(step, report):
            _ask(approval_cb, req)

        verdict = validate_step(
            plan,
            step,
            report,
            runner,
            timeout=validate_timeout,
            prior_outcomes=prior_outcomes,
            prior_attempts=tuple(prior_attempts),
        )
        last_verdict = verdict

        if verdict.approved:
            return StageOutcome(step_id=step.id, report=report, verdict=verdict, iterations=attempt)

        # Record this rejected attempt for cumulative judgement on the next try
        prior_attempts.append(AttemptRecord(attempt=attempt, report=report, verdict=verdict))

        # Build prior_failures payload for the executor's next attempt
        prior_failures = _build_prior_failures_text(prior_attempts)
        logger.info("Step %s attempt %d rejected; retrying", step.id, attempt)

    assert last_report is not None and last_verdict is not None
    return StageOutcome(
        step_id=step.id,
        report=last_report,
        verdict=last_verdict,
        iterations=max_retries + 1,
    )


def _build_prior_failures_text(prior_attempts: List[AttemptRecord]) -> str:
    """Compact prior-failure trail for the EXECUTOR (not the validator).

    Codex reads this to know what was already attempted and why each
    attempt was rejected. Cumulative file-touch list is included so
    Codex knows the workdir may already contain the prior fix.
    """
    parts = []
    union: dict[str, str] = {}
    for a in prior_attempts:
        parts.append(
            f"Attempt {a.attempt} REJECTED.\n"
            f"  Reasons: {'; '.join(a.verdict.reasons) or '(no reasons given)'}\n"
            f"  Fix instructions: {a.verdict.fix_instructions or '(none)'}\n"
            f"  Files changed in this attempt: "
            f"{', '.join(f'{fc.action}:{fc.path}' for fc in a.report.files_changed) or '(none)'}"
        )
        for fc in a.report.files_changed:
            union[fc.path] = fc.action

    cumulative = ", ".join(f"{action}:{path}" for path, action in sorted(union.items())) or "(none)"
    parts.append(f"Cumulative files touched across prior attempts: {cumulative}")
    return "\n\n".join(parts)


def _prepare_artifact_dir(artifact_dir: Optional[Path]) -> Path:
    if artifact_dir is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact_dir = Path("/tmp/custodyloop_runs") / ts
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir
