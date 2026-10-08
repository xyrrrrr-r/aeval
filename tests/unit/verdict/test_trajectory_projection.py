"""Trajectory-evidence projection tests (integration).

The additive ``TrajectoryEvidence`` views — conversation surfaces,
timestamps, turn count, wall clock — and the two efficiency metrics
that consume them. Every unmeasurable input must skip with a reason,
never produce a guessed number.
"""

from __future__ import annotations

from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.models.trajectories.observation import Observation, ObservationResult

from aeval.contracts import CanonicalTranscript
from aeval.verdict.trajectory import (
    TaskWallClock,
    TurnEfficiency,
    build_evidence,
)
from aeval.verdict.trajectory.base import (
    agent_replies_from_steps,
    user_messages_from_steps,
)


def _step(
    step_id: int,
    source: str,
    text: str = "",
    *,
    timestamp: str | None = None,
    extra: dict | None = None,
) -> Step:
    # ATIF: per-step `metrics` is only applicable to agent steps.
    step_metrics = (
        Metrics(prompt_tokens=10, completion_tokens=2)
        if source == "agent"
        else None
    )
    return Step(
        step_id=step_id,
        source=source,
        message=text,
        timestamp=timestamp,
        extra=extra,
        metrics=step_metrics,
    )


def _evidence(steps: list[Step]):
    transcript = CanonicalTranscript(
        atif=Trajectory(
            agent=Agent(name="dsh", version="test"),
            steps=steps,
            final_metrics=FinalMetrics(
                total_prompt_tokens=100, total_completion_tokens=20,
                total_cached_tokens=0,
            ),
        ),
        stop_reason="agent_claimed_done",
    )
    return build_evidence(transcript, "agent_claimed_done")


# --- conversation-surface views -------------------------------------------


def test_projection_extracts_agent_and_user_surfaces():
    steps = [
        _step(1, "user", "hello there", extra={"adapter": {"turn": 1}}),
        _step(2, "agent", "hi!", extra={"adapter": {"turn": 1}}),
        _step(3, "agent", "   "),  # blank surface: dropped
        _step(4, "user", ""),      # blank input: dropped
        _step(5, "agent", "done"),
    ]
    ev = _evidence(steps)
    assert [m.step_id for m in ev.agent_messages] == [2, 5]
    assert [m.step_id for m in agent_replies_from_steps(steps)] == [2, 5]
    assert [m.text for m in ev.agent_messages] == ["hi!", "done"]
    assert ev.user_message_texts == ("hello there",)
    assert [m.step_id for m in user_messages_from_steps(steps)] == [1]
    # the turn marker is found without naming the adapter that wrote it
    assert ev.agent_messages[0].turn == 1
    assert ev.agent_messages[1].turn is None


# --- timing views -----------------------------------------------------------


def test_wall_clock_from_parseable_endpoints():
    steps = [
        _step(1, "user", "go", timestamp="2026-10-05T12:00:00+00:00"),
        _step(2, "agent", "working", timestamp="2026-10-05T12:01:30+00:00"),
        _step(3, "agent", "done", timestamp="2026-10-05T12:02:30+00:00"),
    ]
    ev = _evidence(steps)
    assert ev.step_timestamps == (
        "2026-10-05T12:00:00+00:00",
        "2026-10-05T12:01:30+00:00",
        "2026-10-05T12:02:30+00:00",
    )
    assert ev.wall_clock_seconds == 150.0


def test_wall_clock_none_when_endpoints_missing():
    # no timestamps at all
    ev = _evidence([_step(1, "user", "go"), _step(2, "agent", "done")])
    assert ev.wall_clock_seconds is None
    # one endpoint missing
    ev = _evidence([
        _step(1, "user", "go", timestamp="2026-10-05T12:00:00+00:00"),
        _step(2, "agent", "done"),
    ])
    assert ev.wall_clock_seconds is None
    # (an unparseable timestamp cannot even enter a sealed transcript:
    # the ATIF schema validates ISO 8601 at parse time — the defensive
    # parser in _wall_clock_seconds is depth, not a reachable path)
    # clocks ran backwards: the span is not trustworthy
    ev = _evidence([
        _step(1, "user", "go", timestamp="2026-10-05T12:05:00+00:00"),
        _step(2, "agent", "done", timestamp="2026-10-05T12:00:00+00:00"),
    ])
    assert ev.wall_clock_seconds is None
    # single instant: a zero span is a real measurement
    ev = _evidence([
        _step(1, "agent", "fast", timestamp="2026-10-05T12:00:00+00:00"),
    ])
    assert ev.wall_clock_seconds == 0.0


def test_turn_count_counts_distinct_markers_and_none_without_them():
    steps = [
        _step(1, "user", "a", extra={"adapter": {"turn": 1}}),
        _step(2, "agent", "b", extra={"adapter": {"turn": 1}}),
        _step(3, "user", "c", extra={"adapter": {"turn": 2}}),
        _step(4, "agent", "d", extra={"adapter": {"turn": 2}}),
    ]
    assert _evidence(steps).turn_count == 2
    assert _evidence([_step(1, "user", "a"), _step(2, "agent", "b")]).turn_count is None


# --- TaskWallClock ----------------------------------------------------------


def test_task_wall_clock_ok_degraded_and_skips():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

    def timed(seconds: float):
        end = base + timedelta(seconds=seconds)
        return [
            _step(1, "user", "go", timestamp=base.isoformat()),
            _step(2, "agent", "done", timestamp=end.isoformat()),
        ]

    # 30s of a 60s budget: full headroom
    outcome = TaskWallClock(max_seconds=60).evaluate(_evidence(timed(30)))
    assert outcome.status == "ok" and outcome.score == 1.0
    # 60s of a 60s budget: consumed, no headroom
    outcome = TaskWallClock(max_seconds=60).evaluate(_evidence(timed(60)))
    assert outcome.status == "degraded" and outcome.score == 1.0
    # 120s of a 60s budget: half credit, still not zeroed
    outcome = TaskWallClock(max_seconds=60).evaluate(_evidence(timed(120)))
    assert outcome.status == "degraded" and outcome.score == 0.5
    # no timestamps recorded
    outcome = TaskWallClock(max_seconds=60).evaluate(
        _evidence([_step(1, "user", "go"), _step(2, "agent", "done")])
    )
    assert outcome.status == "skipped" and "not computable" in outcome.reasons[0]
    # no budget declared
    outcome = TaskWallClock().evaluate(_evidence(timed(30)))
    assert outcome.status == "skipped" and "no time budget" in outcome.reasons[0]
    # guard: non-positive budget is a config error
    import pytest

    with pytest.raises(ValueError, match="max_seconds"):
        TaskWallClock(max_seconds=0)


def test_turn_efficiency_ok_degraded_and_skips():
    def turned(turns: int):
        steps = []
        sid = 1
        for turn in range(1, turns + 1):
            steps.append(_step(sid, "user", "go", extra={"s": {"turn": turn}}))
            sid += 1
            steps.append(_step(sid, "agent", "ok", extra={"s": {"turn": turn}}))
            sid += 1
        return steps

    # 2 turns of a 4-turn budget
    outcome = TurnEfficiency(max_turns=4).evaluate(_evidence(turned(2)))
    assert outcome.status == "ok" and outcome.score == 1.0
    # budget consumed
    outcome = TurnEfficiency(max_turns=4).evaluate(_evidence(turned(4)))
    assert outcome.status == "degraded" and outcome.score == 1.0
    # overspent: half credit
    outcome = TurnEfficiency(max_turns=4).evaluate(_evidence(turned(8)))
    assert outcome.status == "degraded" and outcome.score == 0.5
    # single-shot transcript: no markers, no guess
    outcome = TurnEfficiency(max_turns=4).evaluate(
        _evidence([_step(1, "user", "go"), _step(2, "agent", "done")])
    )
    assert outcome.status == "skipped" and "no turn markers" in outcome.reasons[0]
    # no budget declared
    outcome = TurnEfficiency().evaluate(_evidence(turned(2)))
    assert outcome.status == "skipped" and "no turn budget" in outcome.reasons[0]
    import pytest

    with pytest.raises(ValueError, match="max_turns"):
        TurnEfficiency(max_turns=0)
