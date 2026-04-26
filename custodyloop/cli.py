#!/usr/bin/env python3
"""CustodyLoop CLI — autonomous Claude→Codex→GPT-5.5 execution loop.

Usage:
    bin/custodyloop.py --task "<task>" [--workdir <path>] [--artifact-dir <path>]
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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from r6 import (
    ShellRunner,
    run_loop,
)
from r6.control_junctions import (
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


def main() -> int:
    p = argparse.ArgumentParser(description="CustodyLoop autonomous execution loop")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="Task description (string)")
    g.add_argument("--task-file", help="Path to file containing task description")
    p.add_argument("--workdir", help="Workdir for executor (Codex will be confined here)", default=None)
    p.add_argument("--artifact-dir", help="Directory for run artifacts", default=None)
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

    task = args.task if args.task else Path(args.task_file).read_text().strip()
    workdir = Path(args.workdir).resolve() if args.workdir else None
    artifact_dir = Path(args.artifact_dir).resolve() if args.artifact_dir else None

    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)

    cb = _build_callback(args.auto_approve)
    runner = ShellRunner()

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
