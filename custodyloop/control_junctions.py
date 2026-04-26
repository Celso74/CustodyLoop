"""Control junctions — the only points where the user is asked.

Categories:
- first_live_modification: before the first execution step that actually
  touches a file outside a sandbox.
- destructive: rm -rf, git reset --hard, force-push, schema DROP, secret
  rotation, file deletion outside a temp dir.
- scope_expansion: a file changed that is NOT in plan.allowed_files.
- commit / push: any git commit or git push.
- sensitive: auth, schema, infra, production keywords in changed paths.

Detection runs over the planned step (allowed_files / forbidden_files)
and over the actual ExecutionReport (commands_executed, files_changed).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

from .types import ExecutionReport, PlanStep


class JunctionKind(str, Enum):
    FIRST_LIVE_MOD = "first_live_modification"
    DESTRUCTIVE = "destructive"
    SCOPE_EXPANSION = "scope_expansion"
    COMMIT = "commit"
    PUSH = "push"
    SENSITIVE = "sensitive"


@dataclass
class JunctionRequest:
    kind: JunctionKind
    detail: str
    step_id: Optional[str] = None
    evidence: Optional[List[str]] = None

    def __str__(self) -> str:
        return f"[{self.kind.value}] {self.detail}"


ApprovalCallback = Callable[[JunctionRequest], bool]


# ── Detection rules ──────────────────────────────────────────────────────────

_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*f[a-z]*r", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+(--force|-f)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[a-z]*f", re.IGNORECASE),
    re.compile(r"\bgit\s+branch\s+-D\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+(table|database|schema)\b", re.IGNORECASE),
    re.compile(r"\btruncate\s+table\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r":\s*>\s*/dev/sd[a-z]", re.IGNORECASE),
    re.compile(r"\bshred\b", re.IGNORECASE),
]

_COMMIT_PATTERNS = [re.compile(r"\bgit\s+commit\b", re.IGNORECASE)]
_PUSH_PATTERNS = [re.compile(r"\bgit\s+push\b", re.IGNORECASE)]

_SENSITIVE_PATH_HINTS = [
    "auth",
    "secret",
    "credential",
    ".env",
    "id_rsa",
    "id_ed25519",
    "kubeconfig",
    "production",
    "prod/",
    "schema.sql",
    "migrations/",
    "alembic/",
    ".github/workflows",
]


def detect_destructive(report: ExecutionReport) -> List[JunctionRequest]:
    found: List[JunctionRequest] = []
    for cmd in report.commands_executed:
        for pat in _DESTRUCTIVE_PATTERNS:
            if pat.search(cmd.cmd):
                found.append(
                    JunctionRequest(
                        kind=JunctionKind.DESTRUCTIVE,
                        detail=f"Destructive command: {cmd.cmd}",
                        step_id=report.step_id,
                        evidence=[cmd.cmd],
                    )
                )
                break
    for fc in report.files_changed:
        if fc.action == "deleted":
            found.append(
                JunctionRequest(
                    kind=JunctionKind.DESTRUCTIVE,
                    detail=f"File deletion: {fc.path}",
                    step_id=report.step_id,
                    evidence=[fc.path],
                )
            )
    return found


def detect_commit_push(report: ExecutionReport) -> List[JunctionRequest]:
    found: List[JunctionRequest] = []
    for cmd in report.commands_executed:
        for pat in _PUSH_PATTERNS:
            if pat.search(cmd.cmd):
                found.append(
                    JunctionRequest(
                        kind=JunctionKind.PUSH,
                        detail=f"Push command: {cmd.cmd}",
                        step_id=report.step_id,
                        evidence=[cmd.cmd],
                    )
                )
        for pat in _COMMIT_PATTERNS:
            if pat.search(cmd.cmd):
                found.append(
                    JunctionRequest(
                        kind=JunctionKind.COMMIT,
                        detail=f"Commit command: {cmd.cmd}",
                        step_id=report.step_id,
                        evidence=[cmd.cmd],
                    )
                )
    return found


def detect_scope_creep(step: PlanStep, report: ExecutionReport) -> List[JunctionRequest]:
    if not step.allowed_files:
        return []
    out_of_scope: List[str] = []
    for fc in report.files_changed:
        path = fc.path
        if any(fnmatch.fnmatch(path, pat) for pat in step.forbidden_files):
            out_of_scope.append(f"FORBIDDEN: {path}")
            continue
        if not any(fnmatch.fnmatch(path, pat) for pat in step.allowed_files):
            out_of_scope.append(f"NOT-ALLOWED: {path}")
    if out_of_scope:
        return [
            JunctionRequest(
                kind=JunctionKind.SCOPE_EXPANSION,
                detail=f"Files outside allowed_files for step {step.id}",
                step_id=step.id,
                evidence=out_of_scope,
            )
        ]
    return []


def detect_sensitive(step: PlanStep, report: ExecutionReport) -> List[JunctionRequest]:
    flagged: List[str] = []
    for fc in report.files_changed:
        path = fc.path.lower()
        for hint in _SENSITIVE_PATH_HINTS:
            if hint in path:
                flagged.append(f"{fc.path} (matched {hint})")
                break
    if flagged:
        return [
            JunctionRequest(
                kind=JunctionKind.SENSITIVE,
                detail=f"Sensitive paths touched in step {step.id}",
                step_id=step.id,
                evidence=flagged,
            )
        ]
    return []


def first_live_modification_request(step: PlanStep) -> JunctionRequest:
    return JunctionRequest(
        kind=JunctionKind.FIRST_LIVE_MOD,
        detail=f"About to make first live modification (step {step.id}: {step.objective})",
        step_id=step.id,
        evidence=list(step.allowed_files),
    )


def collect_post_execution_junctions(step: PlanStep, report: ExecutionReport) -> List[JunctionRequest]:
    return (
        detect_destructive(report)
        + detect_commit_push(report)
        + detect_scope_creep(step, report)
        + detect_sensitive(step, report)
    )


# ── Standard callbacks ───────────────────────────────────────────────────────


def auto_approve_callback(req: JunctionRequest) -> bool:
    """For dry-runs and tests only — approves everything. Never use in prod."""
    return True


def deny_callback(req: JunctionRequest) -> bool:
    """Default-deny callback — useful for fail-closed integration tests."""
    return False


def stdin_approval_callback(req: JunctionRequest) -> bool:
    """Interactive approval via stdin. Returns False on EOF or 'no'."""
    print(f"\n[CONTROL JUNCTION] {req}")
    if req.evidence:
        for line in req.evidence:
            print(f"  - {line}")
    try:
        ans = input("Approve? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")
