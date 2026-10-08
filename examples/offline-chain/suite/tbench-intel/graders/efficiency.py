"""Versioned efficiency grader for the tbench-intel variant.

Completion time and conversation turns vs suite-declared budgets:

* ``TaskWallClock`` — first-to-last-step wall time from the sealed
  step timestamps vs the run's time budget (900 s, mirroring the
  pilot's ``agent.run_timeout_sec``);
* ``TurnEfficiency`` — distinct turn markers vs the granted turn
  budget (terminal-bench trials are single-instruction; the budget
  guards against runaway conversational loops).

Both skip honestly when the transcript carries no timestamps / no
turn markers — an unmeasurable duration is never a guessed one.
"""

from __future__ import annotations

from typing import Any

from aeval.verdict.trajectory.base import TrajectoryGrader
from aeval.verdict.trajectory.metrics import TaskWallClock, TurnEfficiency

GRADER_ID = "tbench-efficiency"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

# Mirrors the suite declaration (efficiency grader carries no veto).
VETO = False

# 预算与 suites/tbench-pilot/budgets.yaml 的 agent.run_timeout_sec 对齐。
MAX_SECONDS = 900.0

# 终端基准任务单指令；轮次预算防的是会话式追问下的失控循环。
MAX_TURNS = 6

_IMPL = TrajectoryGrader(
    GRADER_ID,
    GRADER_VERSION,
    [
        TaskWallClock(max_seconds=MAX_SECONDS),
        TurnEfficiency(max_turns=MAX_TURNS),
    ],
    veto=VETO,
)


async def grade(record: Any) -> Any:
    return await _IMPL.grade(record)
