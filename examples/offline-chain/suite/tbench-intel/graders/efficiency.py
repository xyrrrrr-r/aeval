"""Versioned efficiency grader for the tbench-intel variant (v5 A).

Completion time and conversation turns vs suite-declared budgets:

* ``TaskWallClock`` — first-to-last-step wall time from the sealed
  step timestamps vs the run's time budget (900 s, mirroring the
  pilot's ``agent.run_timeout_sec``);
* ``TurnEfficiency`` — distinct turn markers vs the granted turn
  budget (terminal-bench trials are single-instruction; the budget
  guards against runaway conversational loops).

Both skip honestly when the transcript carries no timestamps / no
turn markers — an unmeasurable duration is never a guessed one.

v5 A：模块只提供实现（``METRICS``）；判哪几项由套件声明
（``verdict.guards[].metrics``），veto 同样是声明的事（这个守卫不设
veto，是信息项）。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from aeval.verdict.trajectory.presets import efficiency_metric_registry

GRADER_ID = "tbench-efficiency"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

# 预算与 suites/tbench-pilot/budgets.yaml 的 agent.run_timeout_sec 对齐。
MAX_SECONDS = 900.0

# 终端基准任务单指令；轮次预算防的是会话式追问下的失控循环。
MAX_TURNS = 6

METRICS: dict[str, Callable[[Mapping[str, Any]], Any]] = efficiency_metric_registry(
    max_seconds=MAX_SECONDS,
    max_turns=MAX_TURNS,
)
