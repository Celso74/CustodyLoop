"""Tests for custodyloop/loop.py — pipeline integration with a fake Runner.

A FakeRunner returns canned text for each model call, so tests cover
plan→execute→validate→retry→approve→report flow without real LLMs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Dict, List

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop import (
    auto_approve_callback,
    deny_callback,
    run_loop,
)
from custodyloop.control_junctions import (
    JunctionKind,
    JunctionRequest,
)
from custodyloop.runner import Runner

# ── Fake runner ─────────────────────────────────────────────────────────────


class FakeRunner(Runner):
    """Calls a per-model handler. Records every call."""

    def __init__(self, handlers: Dict[str, Callable[[str], str]]):
        self.handlers = handlers
        self.calls: List[tuple] = []

    def run(self, model, prompt, *, workdir=None, timeout=600):
        self.calls.append((model, prompt[:80]))
        return self.handlers[model](prompt)


def _wrap_json(obj) -> str:
    return f"prefix\n===JSON===\n{json.dumps(obj)}\n===END===\nsuffix"


# ── Canned responses ────────────────────────────────────────────────────────


def _plan_packet(steps_count: int = 1) -> dict:
    return {
        "task": "create file foo.txt with hello",
        "audience": "engineer",
        "artifact_type": "code_change",
        "steps": [
            {
                "id": f"step_{i}",
                "objective": f"objective {i}",
                "allowed_files": [f"foo_{i}.txt"],
                "forbidden_files": ["secrets/*"],
                "expected_output": "file exists",
                "required_tests": [f"test -f foo_{i}.txt"],
                "success_criteria": ["exit 0"],
                "rollback_plan": "rm file",
            }
            for i in range(1, steps_count + 1)
        ],
        "anti_failure": {
            "artifact_compliance": "produce file",
            "no_hallucinated_specifics": "none provided",
            "audience_alignment": "engineer",
        },
        "notes": "ASSUMPTION: workdir is writable",
    }


def _exec_report(step_id: str, *, scope_violation=False, destructive=False) -> dict:
    files = [{"path": f"{step_id.replace('step_', 'foo_')}.txt", "added": 1, "removed": 0, "action": "created"}]
    if scope_violation:
        files.append({"path": "secrets/token", "added": 1, "removed": 0, "action": "created"})
    cmds = [
        {"cmd": f"echo hello > foo_{step_id[-1]}.txt", "exit_code": 0, "stdout_summary": ""},
        {"cmd": f"test -f foo_{step_id[-1]}.txt", "exit_code": 0, "stdout_summary": ""},
    ]
    if destructive:
        cmds.append({"cmd": "rm -rf /tmp/dangerous", "exit_code": 0, "stdout_summary": ""})
    return {
        "step_id": step_id,
        "files_changed": files,
        "diff_summary": "Created file.",
        "commands_executed": cmds,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0, "details": "ok"},
        "errors": [],
        "fixes_applied": [],
        "remaining_risks": "",
    }


def _verdict_approved() -> dict:
    return {
        "decision": "APPROVED",
        "reasons": ["matches plan"],
        "fix_instructions": "",
        "checks": {"execution_matched_plan": True, "tests_actually_ran": True},
    }


def _verdict_rejected() -> dict:
    return {
        "decision": "REJECTED",
        "reasons": ["test missing"],
        "fix_instructions": "actually run the test",
        "checks": {"tests_actually_ran": False},
    }


def _final_report() -> dict:
    return {
        "summary": "Created the file.",
        "why": "Operator asked for it.",
        "changes": ["Added foo_1.txt"],
        "risks_remaining": [],
        "next_steps": [],
    }


# ── Tests ───────────────────────────────────────────────────────────────────


def test_happy_path_one_step(tmp_path):
    plan = _plan_packet(1)
    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)

    result = run_loop(
        "create file foo_1.txt",
        runner=runner,
        workdir=tmp_path,
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "artifacts",
    )
    assert not result.aborted
    assert len(result.outcomes) == 1
    assert result.outcomes[0].verdict.approved
    assert result.outcomes[0].iterations == 1
    assert result.final_report is not None
    assert "Created" in result.final_report.summary
    assert (tmp_path / "artifacts" / "PLAN_PACKET.json").exists()
    assert (tmp_path / "artifacts" / "FINAL_REPORT.md").exists()
    assert (tmp_path / "artifacts" / "step_1.execution_report.json").exists()
    assert (tmp_path / "artifacts" / "step_1.verdict.json").exists()


def test_retry_on_rejected_verdict(tmp_path):
    plan = _plan_packet(1)
    state = {"verdict_calls": 0}

    def chatgpt_h(prompt):
        state["verdict_calls"] += 1
        # First call: reject. Second: approve.
        if state["verdict_calls"] == 1:
            return _wrap_json(_verdict_rejected())
        return _wrap_json(_verdict_approved())

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": chatgpt_h,
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "artifacts",
        max_retries=2,
    )
    assert not result.aborted
    assert result.outcomes[0].iterations == 2
    assert state["verdict_calls"] == 2


def test_validator_receives_prior_outcomes_in_prompt(tmp_path):
    """When validating step_2, the validator prompt must contain step_1's evidence."""
    plan = _plan_packet(2)
    captured = {"step_2_prompt": None}

    state = {"step": 0}

    def codex_h(prompt):
        state["step"] += 1
        return _wrap_json(_exec_report(f"step_{state['step']}"))

    def chatgpt_h(prompt):
        # Capture the prompt seen on the step_2 verdict
        if "step_2" in prompt and captured["step_2_prompt"] is None:
            # avoid capturing the step_1 verdict (which mentions step_1 only)
            captured["step_2_prompt"] = prompt
        return _wrap_json(_verdict_approved())

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": codex_h,
        "chatgpt": chatgpt_h,
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "artifacts",
        max_retries=0,
    )
    assert not result.aborted
    p = captured["step_2_prompt"]
    assert p is not None
    # Step 1's evidence (its commands and id) must appear in step 2's validator prompt
    assert "step_1" in p
    assert "echo hello > foo_1.txt" in p
    # The PRIOR_OUTCOMES placeholder must have been replaced
    assert "<PRIOR_OUTCOMES>" not in p


def test_max_retries_exhausted_aborts(tmp_path):
    plan = _plan_packet(1)
    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": lambda p: _wrap_json(_verdict_rejected()),
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "artifacts",
        max_retries=1,
    )
    assert result.aborted
    assert "not approved" in result.abort_reason
    assert result.final_report is None


def test_destructive_command_pauses_for_approval(tmp_path):
    plan = _plan_packet(1)
    asked: List[JunctionRequest] = []

    def cb(req: JunctionRequest) -> bool:
        asked.append(req)
        return req.kind != JunctionKind.DESTRUCTIVE  # deny destructive

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1", destructive=True)),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=cb,
        artifact_dir=tmp_path / "artifacts",
    )
    assert result.aborted
    kinds = [r.kind for r in asked]
    assert JunctionKind.DESTRUCTIVE in kinds


def test_first_live_mod_junction_asked_once(tmp_path):
    plan = _plan_packet(2)
    asked: List[JunctionRequest] = []

    def cb(req):
        asked.append(req)
        return True

    state = {"step": 0}

    def codex_h(prompt):
        state["step"] += 1
        return _wrap_json(_exec_report(f"step_{state['step']}"))

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": codex_h,
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=cb,
        artifact_dir=tmp_path / "artifacts",
    )
    first_mods = [r for r in asked if r.kind == JunctionKind.FIRST_LIVE_MOD]
    assert len(first_mods) == 1
    assert not result.aborted


def test_first_live_mod_denial_aborts_before_any_execution(tmp_path):
    plan = _plan_packet(1)
    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=deny_callback,
        artifact_dir=tmp_path / "artifacts",
    )
    assert result.aborted
    # codex should never have been invoked
    assert not any(model == "codex" for model, _ in runner.calls)


def test_advisory_steps_skip_first_live_mod(tmp_path):
    """Steps with no allowed_files = advisory; no junction needed."""
    plan = _plan_packet(1)
    plan["steps"][0]["allowed_files"] = []
    asked: List[JunctionRequest] = []

    def cb(req):
        asked.append(req)
        return True

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    run_loop("task", runner=runner, workdir=tmp_path, approval_cb=cb, artifact_dir=tmp_path / "artifacts")
    assert all(r.kind != JunctionKind.FIRST_LIVE_MOD for r in asked)


def test_scope_creep_pauses_for_approval(tmp_path):
    plan = _plan_packet(1)
    asked: List[JunctionRequest] = []

    def cb(req):
        asked.append(req)
        return req.kind != JunctionKind.SCOPE_EXPANSION

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(_exec_report("step_1", scope_violation=True)),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=cb,
        artifact_dir=tmp_path / "artifacts",
    )
    assert result.aborted
    assert any(r.kind == JunctionKind.SCOPE_EXPANSION for r in asked)


def test_planner_malformed_output_raises(tmp_path):
    handlers = {
        "claude": lambda p: "no json here at all",
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    with pytest.raises(ValueError):
        run_loop(
            "task",
            runner=runner,
            workdir=tmp_path,
            approval_cb=auto_approve_callback,
            artifact_dir=tmp_path / "artifacts",
        )


def test_executor_wrong_step_id_raises(tmp_path):
    plan = _plan_packet(1)
    bad = _exec_report("step_1")
    bad["step_id"] = "step_999"
    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(bad),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    with pytest.raises(ValueError):
        run_loop(
            "task",
            runner=runner,
            workdir=tmp_path,
            approval_cb=auto_approve_callback,
            artifact_dir=tmp_path / "artifacts",
        )


def test_final_report_is_sanitized(tmp_path):
    """Forbidden internal terms must be redacted from FinalReport text."""
    plan = _plan_packet(1)
    leaky = {
        "summary": "Claude wrote it and Codex approved.",
        "why": "CustodyLoop council decided.",
        "changes": ["GPT-5.5 validated"],
        "risks_remaining": [],
        "next_steps": [],
    }
    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else leaky),
        "codex": lambda p: _wrap_json(_exec_report("step_1")),
        "chatgpt": lambda p: _wrap_json(_verdict_approved()),
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "artifacts",
    )
    fr = result.final_report
    assert fr is not None
    md = fr.to_markdown().lower()
    for bad in ("claude", "codex", "gpt", "validator", "planner", "executor"):
        assert bad not in md, f"Forbidden term {bad!r} not redacted"


def test_no_self_approval_codex_does_not_decide(tmp_path):
    """Verify Codex output is never used as a verdict — even if Codex emits a 'decision' field."""
    plan = _plan_packet(1)
    sneaky_exec = _exec_report("step_1")
    sneaky_exec["decision"] = "APPROVED"  # Codex tries to self-approve
    state = {"verdict_called": False}

    def chatgpt_h(prompt):
        state["verdict_called"] = True
        return _wrap_json(_verdict_rejected())

    handlers = {
        "claude": lambda p: _wrap_json(plan if "PLANNER" in p else _final_report()),
        "codex": lambda p: _wrap_json(sneaky_exec),
        "chatgpt": chatgpt_h,
    }
    runner = FakeRunner(handlers)
    result = run_loop(
        "task",
        runner=runner,
        workdir=tmp_path,
        approval_cb=auto_approve_callback,
        artifact_dir=tmp_path / "artifacts",
        max_retries=0,
    )
    # GPT-5.5 must have been the actual decider
    assert state["verdict_called"]
    # And its REJECTED verdict is binding even though Codex claimed APPROVED
    assert result.aborted
