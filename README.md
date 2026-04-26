# CustodyLoop

> **CustodyLoop runs AI-generated code changes and refuses to trust them until a separate model independently verifies the result.**

It's built to answer one question:

> *Does a second model catch mistakes the first one misses?*

The loop has three stages, run by three different models, plus a final
report:

1. **Plan** — a planner model produces a structured JSON plan packet:
   objectives, allowed files, forbidden files, expected output, required
   tests, success criteria, rollback plan.
2. **Execute** — an executor model performs the work in a workdir under
   the plan's contract: file edits, command runs, test execution.
3. **Validate** — a *separate* validator model issues a binding verdict
   (`APPROVED` / `REJECTED`) on the execution report. The validator can
   reject; the planner cannot override.
4. **Report** — the planner composes a clean operator-facing report,
   stripped of internal vocabulary.

Every run produces a JSON audit trail: plan packet, execution report,
verdict, final report. The trail is what makes the answer auditable.

## What this is NOT

- **Not production-ready.** This is a research artifact.
- **Not an autonomous agent platform.** It runs one bounded task at a time.
- **Not an IDE replacement.** It does not pair-program. It does not
  autocomplete. It runs in the background and emits reports.
- **Not a general agent framework.** It has exactly five paths and one
  loop. Adding a sixth path is a design smell.
- **Not vendor-locked.** The runner is just a model name -> wrapper
  script mapping. You supply the wrappers; you supply the auth.

## Why it exists

Two open questions in autonomous AI coding:

1. **Does independent validation actually catch what the planner misses?**
   Or does a single strong model with a good scaffold do better than a
   loop of weaker steps?
2. **Is a chain-of-custody audit trail ever worth the latency cost?**
   Most current tools optimize for speed. This one optimizes for the
   verdict trail.

CustodyLoop is the smallest reproducible artifact for testing those two
questions.

## Current state

- The three-stage loop works for **small coding tasks** in disposable
  workdirs (fix a failing test, add a function, fix an import,
  flatten nested control flow).
- An earlier version with a multi-model council was tested against
  single-model baselines on harder strategic tasks and **lost
  decisively**: 0/8 wins, 6.8x to 17.7x slower, ~9x worse
  quality-per-latency. The council was removed. Only the three-stage
  loop survived. See `RESEARCH_FINDINGS.md`.
- Validator rotation (running the validator slot through several models
  and picking the best by catch-rate-per-cost) is **future work**. The
  current code uses one validator per run.
- All inter-stage contracts are typed and round-trippable.
- Test coverage focuses on the loop, the schemas, the JSON extraction,
  the retry-audit-trail mechanism, and the control junctions.

## Requirements

You bring your own model access. CustodyLoop calls three shell wrappers
that the user installs and authenticates separately:

- `call_claude.sh` — invokes a planner / reporter model
- `call_codex.sh` — invokes an executor model with workdir access
- `call_chatgpt.sh` — invokes a validator model

Sample wrappers are provided in `wrappers/`. They are minimal stubs:
they do not handle account failover, sandboxing, or retries. You are
expected to harden them for your own environment.

The runner sets a `CUSTODYLOOP_MODEL_ID` environment variable per call.
You must point each wrapper at a model identifier your CLI / account
actually accepts. Defaults are intentionally absent to force explicit
choice.

Python 3.10+. No third-party runtime dependencies.

## Quick start

A canned-response demo runs end-to-end with no API access:

```bash
git clone <this repo>
cd custodyloop
pip install -e .
python -m custodyloop.dry_run
```

This runs a fake-runner version of the loop on the task "create
greeting.txt with the line Hello World" in a temp workdir and prints
the full audit trail. Useful for understanding the pipeline shape
before pointing it at real models.

For a real run:

```bash
# 1. Install a wrapper for each model role (see wrappers/).
# 2. Make them executable and add their directory to PATH.

export CUSTODYLOOP_MODEL_ID="<your-planner-model-id>"
python -m custodyloop \
    --task "fix the failing test in test_foo.py" \
    --workdir /tmp/my_safe_workdir \
    --auto-approve \
    --max-retries 2
```

Run `python -m custodyloop --help` for all options.

## Architecture at a glance

```
operator task
   |
   v
Stage 1 — planner   --> PlanPacket (JSON)
   |
   v  for each step:
[junction] first live modification — ask once
   |
   v
Stage 2 — executor (in workdir) --> ExecutionReport (JSON)
   |
   v
[junctions] destructive / scope-creep / commit / push / sensitive paths
   |
   v
Stage 3 — validator --> Verdict (APPROVED | REJECTED + fix instructions)
   |                                                    ^
   | if REJECTED and attempts < max ----------------+    | (retry, with
   |                                                |    |  prior-attempt
   v if APPROVED, next step. After last step:       |    |  evidence)
Stage 4 — reporter  --> FinalReport (JSON + Markdown)
```

Five control junctions can pause for operator approval. Detection
covers `rm -rf`, `git reset --hard`, force-push, schema drops,
deletion, scope expansion, commits, pushes, and writes to sensitive
path patterns (`auth`, `secret`, `.env`, `migrations/`, etc).

The validator on retry attempts sees prior rejected attempts'
`files_changed` and rejection reasons, so a step can be approved on
cumulative evidence even if the latest attempt's `files_changed` is
empty (the file was already in the desired state from a prior attempt).

## Repository layout

```
custodyloop/                 # the library
  types.py                   # PlanPacket, ExecutionReport, Verdict, etc.
  runner.py                  # model-runner abstraction + ShellRunner
  planner.py                 # stage 1 + workdir capability detection
  executor.py                # stage 2
  validator.py               # stage 3 (with prior-attempt context)
  reporter.py                # stage 4 (with output sanitization)
  control_junctions.py       # operator-pause detection
  loop.py                    # pipeline wiring
  json_extract.py            # robust JSON extraction from LLM output
  dry_run.py                 # canned-response demo
  cli.py                     # `python -m custodyloop` entrypoint
  prompts/                   # planner / executor / validator / reporter
wrappers/                    # minimal example wrappers
tests/                       # unit + integration tests
```

## Testing

```bash
pip install pytest
pytest
```

All tests use a `FakeRunner` and run offline. No API calls are made
during the test suite.

## Warning

Experimental code. The validator is one model issuing one verdict; it
can be wrong. The control junctions catch destructive shell commands by
regex, not by formal proof. The retry mechanism trusts the executor's
file-change report; a malicious or hallucinating executor can lie.
**Do not point CustodyLoop at production code, real secrets, or shared
infrastructure without your own additional sandboxing layer.**

Use at your own risk. No warranties.

## License

MIT. See `LICENSE`.
