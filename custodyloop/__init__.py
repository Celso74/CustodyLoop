"""CustodyLoop — autonomous Claude→Codex→GPT-5.5 execution loop.

Roles (strict):
- Claude Opus 4.7: planner, decomposer, final reporter. Cannot approve own work.
- Codex 5.3: executor. Performs file changes / commands / tests.
- GPT-5.5: validator. Independent approval gate. Authority to accept or reject.

The user is only invoked at true control junctions:
- first live modification, destructive ops, scope expansion, commits, pushes,
  auth/infra/schema changes.

Anti-failure rules (from V1 analysis):
- artifact compliance, no hallucinated specifics, audience alignment,
  no critique-only outputs.
"""

from .control_junctions import (
    ApprovalCallback,
    JunctionKind,
    JunctionRequest,
    auto_approve_callback,
    deny_callback,
    detect_destructive,
    detect_scope_creep,
    detect_sensitive,
)
from .executor import execute_step
from .loop import LoopResult, run_loop
from .planner import produce_plan
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

__all__ = [
    "ApprovalCallback",
    "AttemptRecord",
    "ExecutionReport",
    "FinalReport",
    "JunctionKind",
    "JunctionRequest",
    "PlanPacket",
    "PlanStep",
    "LoopResult",
    "Runner",
    "ShellRunner",
    "StageOutcome",
    "ValidatorVerdict",
    "auto_approve_callback",
    "deny_callback",
    "detect_destructive",
    "detect_scope_creep",
    "detect_sensitive",
    "execute_step",
    "produce_final_report",
    "produce_plan",
    "run_loop",
    "validate_step",
]
