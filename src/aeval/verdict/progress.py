"""Stage-wise requirement progress (P0-7).

The six fixed requirement bits are produced stage by stage during a
trial — they are never defaulted to ``True`` and never bulk-copied from
an unverified source. Each bit is marked exactly once, at the point its
stage completed with evidence; regression (un-marking) is a contract
violation because a completed stage cannot become un-completed.
"""

from __future__ import annotations

from aeval.contracts import RequirementBitmap

__all__ = ["RequirementProgress", "REQUIREMENT_STAGES"]


REQUIREMENT_STAGES = (
    "input_complete",
    "agent_finished",
    "integration_valid",
    "render_valid",
    "judge_finished",
    "artifact_schema_ok",
)


class RequirementProgress:
    """Accumulate the six requirement bits as their stages complete.

    ``judge_finished`` can only be marked through
    :meth:`mark_judging_finished`, never through :meth:`mark`, so the
    judging bit cannot be set before graders actually ran.
    """

    def __init__(self) -> None:
        self._bits: dict[str, bool] = {name: False for name in REQUIREMENT_STAGES}

    def mark(self, stage: str) -> None:
        if stage == "judge_finished":
            raise ValueError(
                "judge_finished is marked only by mark_judging_finished(), "
                "after grading actually completed"
            )
        self._mark(stage)

    def mark_judging_finished(self) -> None:
        self._mark("judge_finished")

    def _mark(self, stage: str) -> None:
        if stage not in self._bits:
            raise ValueError(f"unknown requirement stage: {stage!r}")
        if self._bits[stage]:
            raise ValueError(f"requirement stage already completed: {stage!r}")
        self._bits[stage] = True

    def is_marked(self, stage: str) -> bool:
        if stage not in self._bits:
            raise ValueError(f"unknown requirement stage: {stage!r}")
        return self._bits[stage]

    def snapshot(self) -> RequirementBitmap:
        """The sealed bitmap — unset stages stay ``False`` by construction."""
        return RequirementBitmap(**{name: self._bits[name] for name in REQUIREMENT_STAGES})
