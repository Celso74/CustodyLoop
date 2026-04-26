"""Model runner abstraction.

A Runner has a single contract:
    run(model: str, prompt: str, *, workdir: Path | None, timeout: int) -> str

It returns the model's text output (stdout of the wrapper script).
ShellRunner shells out to wrappers/call_<wrapper>.sh with the proper
CUSTODYLOOP_MODEL_ID env var. Tests substitute a fake Runner.

Supported wrappers (role → script):
- claude   → call_claude.sh   (planner + final reporter)
- codex    → call_codex.sh    (executor)
- chatgpt  → call_chatgpt.sh  (validator)

Per-role model IDs are resolved by ShellRunner from its ``model_ids`` dict
(populated by CLI flags), falling back to the ``CUSTODYLOOP_MODEL_ID`` env
var for legacy single-model mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


# NOTE: model IDs are intentionally not hard-coded. The user supplies them
# via per-role CLI flags (--planner-model / --executor-model /
# --validator-model) which populate ShellRunner.model_ids. The legacy
# CUSTODYLOOP_MODEL_ID env var still works as a single-model fallback.
WRAPPERS = {
    "claude":  ("call_claude.sh",  ""),
    "codex":   ("call_codex.sh",   ""),
    "chatgpt": ("call_chatgpt.sh", ""),
}

BIN_DIR = Path(__file__).resolve().parent.parent  # bin/


class RunnerError(RuntimeError):
    pass


class Runner(ABC):
    @abstractmethod
    def run(self, model: str, prompt: str, *, workdir: Optional[Path] = None, timeout: int = 600) -> str: ...


class ShellRunner(Runner):
    """Invokes the bin/call_*.sh wrappers.

    Model-ID resolution order, per call:
      1. ``model_ids`` dict passed to __init__ (per-role IDs from CLI flags)
      2. ``CUSTODYLOOP_MODEL_ID`` env var (legacy single-model mode)
      3. WRAPPERS table default (always empty in the public repo)
      4. error
    """

    def __init__(self, bin_dir: Path = BIN_DIR, *, model_ids: Optional[dict] = None):
        self.bin_dir = Path(bin_dir)
        self.model_ids = dict(model_ids) if model_ids else {}

    def run(self, model: str, prompt: str, *, workdir: Optional[Path] = None, timeout: int = 600) -> str:
        if model not in WRAPPERS:
            raise RunnerError(f"Unknown model {model!r}; expected one of {sorted(WRAPPERS)}")
        wrapper_name, default_model_id = WRAPPERS[model]
        wrapper = self.bin_dir / wrapper_name
        if not wrapper.exists():
            raise RunnerError(f"Wrapper not found: {wrapper}")

        model_id = (
            self.model_ids.get(model)
            or os.environ.get("CUSTODYLOOP_MODEL_ID")
            or default_model_id
        )
        if not model_id:
            raise RunnerError(
                f"No model ID for role {model!r}: pass --planner-model / "
                f"--executor-model / --validator-model, or set "
                f"CUSTODYLOOP_MODEL_ID for legacy single-model mode"
            )

        tmpdir = Path(tempfile.mkdtemp(prefix=f"custodyloop_{model}_"))
        try:
            in_path = tmpdir / "in.txt"
            out_path = tmpdir / "out.txt"
            in_path.write_text(prompt)

            env = os.environ.copy()
            env["CUSTODYLOOP_MODEL_ID"] = model_id

            cmd = [str(wrapper), str(in_path), str(out_path)]
            if model == "codex" and workdir is not None:
                cmd.append(str(workdir))
            cmd.append(str(timeout))

            try:
                proc = subprocess.run(
                    cmd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 30,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RunnerError(f"{model} wrapper timed out after {timeout}s") from exc

            if proc.returncode != 0:
                err = (proc.stderr or "").strip()[-500:]
                raise RunnerError(f"{model} wrapper exit={proc.returncode}: {err}")

            if not out_path.exists():
                raise RunnerError(f"{model} wrapper produced no output file")
            return out_path.read_text()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
