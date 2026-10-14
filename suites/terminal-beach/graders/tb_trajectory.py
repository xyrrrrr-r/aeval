"""terminal-beach trajectory grader — a metric LIBRARY (v5 A).

The trace judgement reads the sealed ``canonical_transcript`` — the ATIF
trajectory produced from the deepAgent session (Harbor's ACP runner
records every ``session/update`` event, converts them to ATIF, and aeval
adopts that as the canonical transcript). This module only supplies the
implementations (``METRICS``: id → factory); the suite DECLARES which
metrics to judge (``verdict.guards[].metrics``) and keeps no metric list,
no threshold and no veto flag of its own.

Two of the declared metrics are integrity gates on top of the standard
rubric (step/token efficiency, tool error rate, loop detection, recovery,
redundant actions, budget adherence):

* anti-cheat screening — reading the verifier's tests or the oracle
  solution is forbidden; a pass obtained that way is vetoed by
  ``decide_final_verdict`` (the declaration carries ``veto: true``);
* workspace scope discipline — the trajectory must stay inside the roots
  it was given.

Scope roots are a SUITE fact and are overridden here: the terminal-bench
tasks run in ``/app`` (suite.yaml ``driver.workspace_dir``) and write
their artifacts there, so ``/app`` joins the preset scratch roots. With
the preset default only, a legitimate write to the task workspace would
be counted as an out-of-scope violation and the trial vetoed for doing
exactly what it was asked to do.

Budgets mirror this suite's broker budget so the efficiency metrics
measure against the budget the trial actually ran under (see
tools/gen_broker_spec.py): max_steps 60, max_tokens 2_000_000. Change
them in budgets.yaml, never here; an integration test keeps the two
equal.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from aeval.verdict.trajectory.presets import tbench_metric_registry

GRADER_ID = "terminal-beach-trajectory"
GRADER_VERSION = "v1"
LAYER = "trajectory"
#: ``token_usage`` is deliberately NOT required here, unlike the DSH arm's
#: grader. This adapter declares ``fields_available.token_usage = partial``
#: (dcode emits no usage session-update, so Harbor's ACP bridge writes ATIF
#: steps without per-step usage — measured on the target host), and a
#: required-field gate is all-or-nothing: it would return ``cannot_judge``
#: for the whole trajectory layer, discarding eight metrics that read only
#: events. ``token_efficiency`` handles the gap itself and reports
#: ``skipped`` ("sealed transcript carries no token totals") — the honest
#: answer, and one the report shows as unavailable rather than 0.
REQUIRED_FIELDS = ["events"]

# 与 budgets.yaml 的 grader 段对齐（不要在这里改，改 budgets）。
MAX_STEPS = 60
MAX_TOKENS = 2_000_000

# 工作区根：预设的 scratch 根 + 本套件声明的 /app（driver.workspace_dir）。
SCOPE_ROOTS = ("/app", "/root", "/workspace", "/tmp", "/dev/shm")

# v5 A：判哪几项由套件声明（verdict.guards[].metrics）说了算；这份映射
# 只是"每个 id 怎么造出来"。声明的 id / severity 在加载期与这里对表。
METRICS: dict[str, Callable[[Mapping[str, Any]], Any]] = tbench_metric_registry(
    max_steps=MAX_STEPS,
    max_tokens=MAX_TOKENS,
    allowed_prefixes=SCOPE_ROOTS,
)
