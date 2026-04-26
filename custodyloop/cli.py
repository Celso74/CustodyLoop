#!/usr/bin/env python3
"""CustodyLoop CLI — autonomous Claude→Codex→GPT execution loop.

Usage:
    python -m custodyloop --task "<task>" [--workdir <path>] [--artifact-dir <path>]
                          [--planner-model ID] [--executor-model ID]
                          [--validator-model ID]
                          [--auto-approve] [--max-retries N]
                          [--task-file <path>]

Defaults:
- runner: ShellRunner (real model wrappers)
- approval: interactive stdin (auto-approves if --auto-approve and no real
  control junction is destructive)
- artifact_dir: /tmp/custodyloop_runs/<timestamp>/
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custodyloop import (
    ShellRunner,
    run_loop,
)
from custodyloop.control_junctions import (
    JunctionKind,
    JunctionRequest,
    stdin_approval_callback,
)


def _build_callback(auto_approve: bool):
    """Auto-approve only first_live_mod and sensitive (in dry mode);
    always interactive for destructive / commit / push."""
    if not auto_approve:
        return stdin_approval_callback

    def cb(req: JunctionRequest) -> bool:
        if req.kind in (JunctionKind.DESTRUCTIVE, JunctionKind.COMMIT, JunctionKind.PUSH):
            return stdin_approval_callback(req)
        print(f"[auto-approve] {req}")
        return True

    return cb


def _build_model_ids(args, parser: argparse.ArgumentParser) -> dict:
    """Build the role→model_id dict for ShellRunner from CLI flags.

    Fail-fast policy: if any role lacks both a per-role flag AND the legacy
    CUSTODYLOOP_MODEL_ID env var, exit before Stage 1 with a clear error.
    """
    flags = {
        "claude":  args.planner_model,
        "codex":   args.executor_model,
        "chatgpt": args.validator_model,
    }
    env_set = bool(os.environ.get("CUSTODYLOOP_MODEL_ID"))

    missing = []
    if not flags["claude"]:  missing.append("--planner-model")
    if not flags["codex"]:   missing.append("--executor-model")
    if not flags["chatgpt"]: missing.append("--validator-model")

    if missing and not env_set:
        parser.error(
            "missing model ID(s) for: " + ", ".join(missing) + ". "
            "Provide --planner-model, --executor-model, and --validator-model, "
            "or set CUSTODYLOOP_MODEL_ID for legacy single-model mode."
        )

    return {role: mid for role, mid in flags.items() if mid}


def main() -> int:
    p = argparse.ArgumentParser(description="CustodyLoop autonomous execution loop")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="Task description (string)")
    g.add_argument("--task-file", help="Path to file containing task description")
    p.add_argument("--workdir", help="Workdir for executor (Codex will be confined here)", default=None)
    p.add_argument("--artifact-dir", help="Directory for run artifacts", default=None)
    p.add_argument("--planner-model",   help="Model ID for planner + final reporter (claude wrapper)")
    p.add_argument("--executor-model",  help="Model ID for executor (codex wrapper)")
    p.add_argument("--validator-model", help="Model ID for validator (chatgpt wrapper)")
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help="Auto-approve non-destructive junctions (still prompts for destructive/commit/push)",
    )
    p.add_argument(
        "--max-retries", type=int, default=2, help="Max executor retries per step after a REJECTED verdict (default 2)"
    )
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    model_ids = _build_model_ids(args, p)

    task = args.task if args.task else Path(args.task_file).read_text().strip()
    workdir = Path(args.workdir).resolve() if args.workdir else None
    artifact_dir = Path(args.artifact_dir).resolve() if args.artifact_dir else None

    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)

    cb = _build_callback(args.auto_approve)
    runner = ShellRunner(model_ids=model_ids)

    try:
        result = run_loop(
            task,
            runner=runner,
            workdir=workdir,
            approval_cb=cb,
            artifact_dir=artifact_dir,
            max_retries=args.max_retries,
        )
    except ValueError as exc:
        print(f"CustodyLoop ABORTED (malformed model output): {exc}", file=sys.stderr)
        return 2

    print(f"\nArtifacts: {result.artifact_paths.get('artifact_dir', '?')}")
    if result.aborted:
        print(f"CustodyLoop ABORTED: {result.abort_reason}", file=sys.stderr)
        return 1

    assert result.final_report is not None
    print("\n" + result.final_report.to_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
