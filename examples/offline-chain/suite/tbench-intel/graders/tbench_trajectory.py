"""terminal-bench-core pilot trajectory grader — inherits the framework.

Thin wrapper (the inheritance pattern every suite uses): identity
constants plus the Terminal-Bench preset, which adds the two integrity
gates on top of the standard rubric (step/token efficiency, tool error
rate, loop detection, recovery, governance):

* anti-cheat screening — reading the tests or the oracle solution is
  forbidden; a pass obtained that way is vetoed by
  ``decide_final_verdict`` (``veto=True``);
* workspace scope discipline — the trajectory must stay inside the
  workspace it was given.

Budgets mirror the pilot's broker budget so the efficiency metrics
measure against the budget the trial actually ran under (see
tools/gen_broker_spec.py): max_steps 60, max_tokens 2_000_000.
"""

from __future__ import annotations

from typing import Any

from aeval.verdict.trajectory.presets import build_terminalbench_grader

GRADER_ID = "tbench-trajectory"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

# Declared so the loading contract and the preset agree.
VETO = True

_IMPL = build_terminalbench_grader(
    GRADER_ID,
    GRADER_VERSION,
    veto=VETO,
    max_steps=60,
    max_tokens=2_000_000,
)


async def grade(record: Any) -> Any:
    return await _IMPL.grade(record)
