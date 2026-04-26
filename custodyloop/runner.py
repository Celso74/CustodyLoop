"""Model runner abstraction.

A Runner has a single contract:
    run(model: str, prompt: str, *, workdir: Path | None, timeout: int) -> str

It returns the model's text output (stdout of the wrapper script).
ShellRunner shells out to bin/call_<wrapper>.sh with the proper
CUSTODYLOOP_MODEL_ID env var. Tests substitute a fake Runner.

Supported models:
- claude   → call_claude.sh    (CUSTODYLOOP_MODEL_ID=claude-opus-4-7)
- codex    → call_codex.sh     (CUSTODYLOOP_MODEL_ID=gpt-5.3-codex)
- chatgpt  → call_chatgpt.sh   (CUSTODYLOOP_MODEL_ID=gpt-5.5)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


# NOTE: model IDs are intentionally not hard-coded. The user MUST set
# CUSTODYLOOP_MODEL_ID per call (or export it before invoking the wrapper)
# to point at a model identifier their CLI/account actually accepts.
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
    """Invokes the bin/call_*.sh wrappers."""

    def __init__(self, bin_dir: Path = BIN_DIR):
        self.bin_dir = Path(bin_dir)

    def run(self, model: str, prompt: str, *, workdir: Optional[Path] = None, timeout: int = 600) -> str:
        if model not in WRAPPERS:
            raise RunnerError(f"Unknown model {model!r}; expected one of {sorted(WRAPPERS)}")
        wrapper_name, default_model_id = WRAPPERS[model]
        wrapper = self.bin_dir / wrapper_name
        if not wrapper.exists():
            raise RunnerError(f"Wrapper not found: {wrapper}")

        tmpdir = Path(tempfile.mkdtemp(prefix=f"r6_{model}_"))
        try:
            in_path = tmpdir / "in.txt"
            out_path = tmpdir / "out.txt"
            in_path.write_text(prompt)

            env = os.environ.copy()
            if "CUSTODYLOOP_MODEL_ID" not in env:
                if not default_model_id:
                    raise RunnerError(
                        f"CUSTODYLOOP_MODEL_ID env var must be set for model {model!r}"
                    )
                env["CUSTODYLOOP_MODEL_ID"] = default_model_id

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
