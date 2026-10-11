"""terminal-bench-core pilot trajectory grader — a metric LIBRARY (v5 A).

The trace judgement reads the sealed ``canonical_transcript`` — the ATIF
trajectory mapped from the real agent's session log by
``aeval.agents.dsh.atif_mapper`` — and judges the metrics the suite
DECLARES (``verdict.guards[].metrics``). This module only supplies the
implementations (``METRICS``: id → factory) plus the suite-level budgets;
it keeps no metric list, no threshold and no veto flag of its own.

Two of the declared metrics are integrity gates on top of the standard
rubric (step/token efficiency, tool error rate, loop detection, recovery,
governance):

* anti-cheat screening — reading the tests or the oracle solution is
  forbidden; a pass obtained that way is vetoed by
  ``decide_final_verdict`` (the declaration carries ``veto: true``);
* workspace scope discipline — the trajectory must stay inside the
  workspace it was given.

Budgets mirror the pilot's broker budget so the efficiency metrics
measure against the budget the trial actually ran under (see
tools/gen_broker_spec.py): max_steps 60, max_tokens 2_000_000.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from aeval.verdict.trajectory.presets import tbench_metric_registry

GRADER_ID = "tbench-trajectory"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

# 与 jobs/tbench-m0.yaml 的 broker 限制对齐（不要在这里改，改 budgets）。
MAX_STEPS = 60
MAX_TOKENS = 2_000_000

# v5 A：判哪几项由套件声明（verdict.guards[].metrics）说了算；这份映射
# 只是"每个 id 怎么造出来"。声明的 id / severity 在加载期与这里对表。
METRICS: dict[str, Callable[[Mapping[str, Any]], Any]] = tbench_metric_registry(
    max_steps=MAX_STEPS,
    max_tokens=MAX_TOKENS,
)
