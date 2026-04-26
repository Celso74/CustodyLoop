# Research findings — what we learned building CustodyLoop

This is an honest, short technical note. No hype, no marketing.

## Background

The loop in this repository is the surviving piece of a larger
research effort that explored multi-model orchestration for autonomous
coding tasks. The earlier configurations were broader; the current one
is what survived contact with reality.

## V1 — The multi-model council (failed)

The first version routed each task through a council of 5+ models
(planner, multiple reviewers, an adjudicator, a validator), with
review rounds, divergence detection, and adjudicative merge logic.
Approximately 5,000 lines of orchestration sat above the model calls.

A hostile blind A/B/C evaluation on 8 fresh hard tasks (3 adversarial
review, 3 ambiguous spec, 2 multi-constraint) compared the council
against two single-model baselines. The council:

- Won 0 of 8 tasks.
- Scored ~6.99 average composite vs ~8.96 for the single best baseline.
- Was 6.8x slower on hard tasks, 17.7x slower on simple tasks.
- Quality-per-latency ratio was ~9x worse than the single-model baseline.

The judge's failure-mode breakdown was consistent: jargon-heavy
engineering-spec tone, invented organizational specifics (made-up
roles, dates not in the prompt), residual scaffolding in outputs, and
a tendency to substitute critique for the requested artifact.

The councils were not adding intelligence. They were averaging biases,
multiplying latency, and producing committee-shaped output. The whole
council was archived.

## The current loop (works for small tasks)

What survived:

- Three roles (plan / execute / validate) and one reporter.
- Strict typed contracts between stages.
- An independent validator that can reject the executor's work.
- A retry mechanism that carries prior-attempt evidence forward, so
  the validator judges the **cumulative** state of the step rather
  than only the latest attempt.
- Workdir capability detection at planning time, so the planner does
  not specify verification commands the workdir cannot run (e.g., no
  `git diff` checks in a non-git workdir).
- A small set of operator control junctions (destructive operations,
  scope creep, commits, pushes, sensitive paths) that pause for
  approval rather than asking on every routine step.

Four rounds of live validation against real Claude / Codex / GPT-class
models on small coding tasks (fix a failing test, add a function, fix
a broken import, flatten nested control flow):

- Round 1: 2/3 verdict approvals, 3/3 file outcomes correct.
- Round 2: 3/4 verdict approvals, 4/4 file outcomes correct. Failure
  exposed a retry-audit-trail bug where the validator on attempt 2 saw
  empty `files_changed` and rejected for inconsistency with prior
  evidence.
- Round 3: 2/4 verdict approvals, 4/4 file outcomes correct. Failure
  exposed the planner picking `git diff` as a verification command in
  non-git workdirs.
- Round 4 (after the two fixes): 3/3 verdict approvals on three fresh
  tasks, zero retries needed.

The loop is declared **standalone live-ready for small coding tasks**.
It has not been validated on multi-file refactors, network-touching
tasks, or long-running tasks.

## Key insight

The single most surprising finding from this work:

> **Independent validation may matter more than generation.**

The council's loss in V1 was not a generation-quality problem — the
underlying models are perfectly capable of writing correct code. The
loss came from the orchestration layer adding noise, latency, and
committee-style framing. When the orchestration was reduced to one
fixed-role pipeline (plan, execute, validate), the loss disappeared
on small coding tasks.

This suggests the leverage in autonomous coding may not be "more
intelligence in more places," but "an independent verdict before
output is trusted." That is the only piece that turns out to be
hard to replicate with a single model — a model rarely catches its
own mistakes as well as a different model checking it does.

## Open question — validator rotation

The current loop uses one fixed validator. The interesting open
question: **does the validator's identity matter, and can the right
choice be picked empirically?**

A natural extension is to rotate the validator slot across several
models on a fixed bug-fixture suite, measuring catch rate, false
positive rate, cost per call, and latency. The validator role would
then be assigned by composite (`catch_rate * (1 - false_positive_rate) /
(cost_per_call * latency)`), not by prestige. If a cheaper model
catches 85%+ of what a flagship validator catches, at substantially
lower cost, the cheaper model becomes the default; the flagship
becomes the escalation.

This experiment is not in the current code. It is the obvious next
research step. We have not run it.

## What this loop will and will not do

It will:

- Run small bounded coding tasks autonomously, in a workdir.
- Reject changes that don't match the plan.
- Produce a structured audit trail per run.
- Detect destructive commands and pause for approval.
- Strip internal model attributions from operator-facing reports.

It will not:

- Match a strong single model on open-ended reasoning tasks.
  (V1 settled this. Don't use this for strategy memos.)
- Run faster than a single model on simple tasks. The orchestration
  overhead is real and unrecoverable.
- Validate creative work. The validator has no objective ground
  truth for taste-shaped artifacts.
- Fix incorrect plans. If the planner gets the spec wrong, the
  validator can only enforce the wrong spec.
- Survive tasks where the plan's `required_tests` cannot run in the
  target workdir. Capability detection helps, but it is not perfect.

## Reproducing

Every claim above is testable from this repository:

- The fake-runner integration test (`tests/test_dry_run.py`)
  exercises the full pipeline end-to-end without real model calls.
- The unit tests cover schema enforcement, JSON extraction, retry
  audit trail wiring, and capability detection.
- A real-model live run requires installing the three wrappers in
  `wrappers/` and pointing them at your own model accounts. There is
  no telemetry or fixed cloud backend.

## Acknowledgements

The V1 failure data and the shape of the surviving loop were the
result of running roughly 100 real model calls across nine LLMs over
several iterations. The V1 result is the most useful artifact in this
work; the loop is the second most useful.
