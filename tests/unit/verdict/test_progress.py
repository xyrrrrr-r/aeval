"""P0-7 stage-wise requirement progress tests.

The six bits are never defaulted true and judge_finished cannot be set
before grading actually completed.
"""

from __future__ import annotations

import pytest

from aeval.verdict.progress import RequirementProgress


def test_all_stages_marked_produce_full_bitmap():
    p = RequirementProgress()
    p.mark("input_complete")
    p.mark("artifact_schema_ok")
    p.mark("agent_finished")
    p.mark("integration_valid")
    p.mark("render_valid")
    p.mark_judging_finished()
    bitmap = p.snapshot()
    assert bitmap.all_satisfied
    assert bitmap.satisfied_count == 6


def test_unmarked_stages_stay_false():
    p = RequirementProgress()
    p.mark("input_complete")
    bitmap = p.snapshot()
    assert bitmap.input_complete is True
    assert bitmap.judge_finished is False
    assert bitmap.agent_finished is False


def test_mark_twice_is_rejected():
    p = RequirementProgress()
    p.mark("render_valid")
    with pytest.raises(ValueError, match="already completed"):
        p.mark("render_valid")


def test_unknown_stage_rejected():
    p = RequirementProgress()
    with pytest.raises(ValueError, match="unknown requirement stage"):
        p.mark("made_up_stage")
    with pytest.raises(ValueError, match="unknown requirement stage"):
        p.is_marked("made_up_stage")


def test_judge_finished_cannot_be_set_via_mark():
    p = RequirementProgress()
    with pytest.raises(ValueError, match="mark_judging_finished"):
        p.mark("judge_finished")


def test_judge_finished_only_once():
    p = RequirementProgress()
    p.mark_judging_finished()
    with pytest.raises(ValueError, match="already completed"):
        p.mark_judging_finished()


def test_snapshot_is_immutable_view():
    p = RequirementProgress()
    p.mark("input_complete")
    first = p.snapshot()
    p.mark("agent_finished")
    second = p.snapshot()
    assert first.agent_finished is False
    assert second.agent_finished is True
    assert first.input_complete is True
