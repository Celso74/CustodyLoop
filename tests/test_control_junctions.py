"""Tests for custodyloop/control_junctions.py — detection rules."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from custodyloop.control_junctions import (
    JunctionKind,
    auto_approve_callback,
    collect_post_execution_junctions,
    deny_callback,
    detect_commit_push,
    detect_destructive,
    detect_scope_creep,
    detect_sensitive,
    first_live_modification_request,
)
from custodyloop.types import (
    CommandRecord,
    ExecutionReport,
    FileChange,
    PlanStep,
)


def _step(allowed=None, forbidden=None):
    return PlanStep.from_dict(
        {
            "id": "s1",
            "objective": "x",
            "allowed_files": allowed or [],
            "forbidden_files": forbidden or [],
        }
    )


def _report(cmds=None, files=None):
    return ExecutionReport(
        step_id="s1",
        files_changed=[FileChange(**f) for f in (files or [])],
        commands_executed=[CommandRecord(**c) for c in (cmds or [])],
    )


# ── destructive ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/foo",
        "rm -fr /tmp/foo",
        "git reset --hard HEAD~3",
        "git push --force origin main",
        "git push -f",
        "git clean -fd",
        "DROP TABLE users;",
        "TRUNCATE TABLE x;",
        "git branch -D feature",
    ],
)
def test_detects_destructive_commands(cmd):
    r = _report(cmds=[{"cmd": cmd, "exit_code": 0}])
    found = detect_destructive(r)
    assert len(found) == 1
    assert found[0].kind == JunctionKind.DESTRUCTIVE


def test_detects_file_deletion_as_destructive():
    r = _report(files=[{"path": "important.py", "action": "deleted"}])
    found = detect_destructive(r)
    assert any(f.kind == JunctionKind.DESTRUCTIVE for f in found)


def test_does_not_flag_benign_commands():
    r = _report(
        cmds=[
            {"cmd": "pytest tests/", "exit_code": 0},
            {"cmd": "ls -la", "exit_code": 0},
            {"cmd": "rm temp.txt", "exit_code": 0},  # rm without -rf is not flagged
        ]
    )
    assert detect_destructive(r) == []


# ── commit/push ──────────────────────────────────────────────────────────────


def test_detects_commit_and_push():
    r = _report(
        cmds=[
            {"cmd": "git commit -m 'x'", "exit_code": 0},
            {"cmd": "git push origin main", "exit_code": 0},
        ]
    )
    found = detect_commit_push(r)
    kinds = sorted(f.kind for f in found)
    assert JunctionKind.COMMIT in kinds
    assert JunctionKind.PUSH in kinds


def test_no_false_positive_on_commit_string_in_arg():
    r = _report(cmds=[{"cmd": "echo 'about to commit'", "exit_code": 0}])
    assert detect_commit_push(r) == []


# ── scope creep ──────────────────────────────────────────────────────────────


def test_scope_creep_detects_unallowed_paths():
    step = _step(allowed=["src/foo.py"], forbidden=["secrets/*"])
    r = _report(
        files=[
            {"path": "src/foo.py"},
            {"path": "src/bar.py"},  # not allowed
        ]
    )
    found = detect_scope_creep(step, r)
    assert len(found) == 1
    assert found[0].kind == JunctionKind.SCOPE_EXPANSION
    assert any("src/bar.py" in e for e in found[0].evidence)


def test_scope_creep_flags_forbidden_paths_explicitly():
    step = _step(allowed=["src/*.py"], forbidden=["secrets/*"])
    r = _report(files=[{"path": "secrets/token"}])
    found = detect_scope_creep(step, r)
    assert any("FORBIDDEN" in e for e in found[0].evidence)


def test_scope_creep_no_allowed_means_no_check():
    """Advisory steps with no allowed_files should not trigger scope creep."""
    step = _step(allowed=[], forbidden=[])
    r = _report(files=[{"path": "anywhere.py"}])
    assert detect_scope_creep(step, r) == []


def test_scope_creep_glob_match():
    step = _step(allowed=["src/**/*.py", "tests/*.py"])
    r = _report(
        files=[
            {"path": "src/a/b.py"},
            {"path": "tests/foo.py"},
        ]
    )
    # fnmatch doesn't expand ** the same as glob, but tests/foo.py matches plainly.
    # src/a/b.py doesn't match "src/**/*.py" via fnmatch — it'd be flagged.
    # This is expected behavior — keep allowed_files specific.
    found = detect_scope_creep(step, r)
    # tests/foo.py should be allowed
    if found:
        assert all("tests/foo.py" not in e for e in found[0].evidence)


# ── sensitive paths ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "config/auth.py",
        ".env",
        "secrets/token.txt",
        "schema.sql",
        "migrations/001.sql",
        ".github/workflows/ci.yml",
    ],
)
def test_detects_sensitive_paths(path):
    step = _step()
    r = _report(files=[{"path": path}])
    found = detect_sensitive(step, r)
    assert len(found) == 1
    assert found[0].kind == JunctionKind.SENSITIVE


def test_benign_paths_not_flagged_sensitive():
    step = _step()
    r = _report(files=[{"path": "src/utils.py"}])
    assert detect_sensitive(step, r) == []


# ── collect_post_execution_junctions ─────────────────────────────────────────


def test_collect_combines_all_categories():
    step = _step(allowed=["src/foo.py"])
    r = _report(
        cmds=[
            {"cmd": "rm -rf /tmp/x", "exit_code": 0},
            {"cmd": "git push origin master", "exit_code": 0},
        ],
        files=[{"path": "config/auth.py"}],
    )
    junctions = collect_post_execution_junctions(step, r)
    kinds = {j.kind for j in junctions}
    assert JunctionKind.DESTRUCTIVE in kinds
    assert JunctionKind.PUSH in kinds
    assert JunctionKind.SCOPE_EXPANSION in kinds  # config/auth.py not in allowed
    assert JunctionKind.SENSITIVE in kinds


# ── first_live_modification_request ──────────────────────────────────────────


def test_first_live_mod_request_carries_step_info():
    step = _step(allowed=["src/foo.py"])
    req = first_live_modification_request(step)
    assert req.kind == JunctionKind.FIRST_LIVE_MOD
    assert req.step_id == "s1"
    assert "src/foo.py" in req.evidence


# ── default callbacks ────────────────────────────────────────────────────────


def test_auto_approve_returns_true():
    req = first_live_modification_request(_step())
    assert auto_approve_callback(req) is True


def test_deny_returns_false():
    req = first_live_modification_request(_step())
    assert deny_callback(req) is False
