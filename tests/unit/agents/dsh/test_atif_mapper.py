"""ATIF mapper unit tests (plan §7 row 7): no loss, no fabricated relations.

- system/user/assistant map to ATIF sources;
- unknown REQUIRED events fail closed (conversion error);
- unknown ignorable events are preserved verbatim;
- orphan/duplicate/ambiguous call ids are recorded, never guessed;
- every output passes harbor's own TrajectoryValidator.
"""

from __future__ import annotations

import pytest

from aeval.agents.dsh.atif_mapper import (
    DSH_IGNORABLE_EXTRA_KEY,
    DSH_EXTRA_KEY,
    MAPPER_VERSION,
    DshAtifConversionError,
    build_canonical_transcript,
    convert_dsh_read_to_atif,
    derive_stop_reason,
    map_known_session_event,
    normalized_interactions_to_atif_steps,
    reduce_dsh_events,
    resolve_tool_call_relations,
)
from aeval.agents.dsh.bridge import DshReaderResponse
from aeval.contracts import EvidenceBundle


def _response(events, header=None, event_state="shared-frozen") -> DshReaderResponse:
    return DshReaderResponse(
        request_id="req-1",
        header=header or {"sessionId": "s-1", "agent": "dsh", "version": "0.1.7-alpha.1"},
        inherited_event_count=0,
        event_state=event_state,
        events=events,
    )


def test_message_sources_map_to_atif_sources():
    interactions, extras, issues = reduce_dsh_events({}, [
        {"type": "message", "source": "system", "text": "sys"},
        {"type": "message", "source": "user", "text": "q"},
        {"type": "message", "source": "assistant", "text": "a"},
    ])
    assert not issues.any()
    assert [i.source for i in interactions] == ["system", "user", "agent"]
    assert [i.message_text for i in interactions] == ["sys", "q", "a"]
    assert extras == []


def test_unknown_required_event_fails_closed():
    with pytest.raises(DshAtifConversionError, match="unknown required"):
        reduce_dsh_events({}, [
            {"type": "message", "source": "user", "text": "q"},
            {"type": "future_event_kind", "ignorable": False},
        ])


def test_unknown_ignorable_event_preserved_verbatim():
    raw = {"type": "vendor_nudge", "ignorable": True, "payload": {"x": 1}}
    _, extras, issues = reduce_dsh_events({}, [
        {"type": "message", "source": "user", "text": "q"},
        raw,
    ])
    assert not issues.any()
    assert len(extras) == 1
    assert extras[0]["eventIndex"] == 1
    assert extras[0]["type"] == "vendor_nudge"
    assert extras[0]["raw"] == raw  # byte-identical preservation


def test_tool_call_without_callid_records_issue_never_guesses():
    interactions, _, issues = reduce_dsh_events({}, [
        {"type": "message", "source": "user", "text": "q"},
        {"type": "tool_call", "name": "search"},  # no callId
    ])
    assert issues.ambiguous_relations
    assert interactions[-1].tool_calls[0]["callId"].startswith("__unidentified_")


def test_tool_result_without_callid_is_orphan():
    _, _, issues = reduce_dsh_events({}, [
        {"type": "tool_result", "content": "r"},  # no callId
    ])
    assert any("__orphan" in o or "without callId" in o for o in issues.orphan_tool_results)


def test_duplicate_call_ids_recorded():
    interactions, _, issues = reduce_dsh_events({}, [
        {"type": "message", "source": "user", "text": "q"},
        {"type": "tool_call", "callId": "c1", "name": "a"},
        {"type": "tool_call", "callId": "c1", "name": "b"},
    ])
    resolve_tool_call_relations(interactions, issues)
    assert issues.duplicate_call_ids == ["duplicate tool_call callId 'c1'"]


def test_result_referencing_unknown_callid_is_orphan():
    interactions, _, issues = reduce_dsh_events({}, [
        {"type": "tool_call", "callId": "c1", "name": "a"},
        {"type": "tool_result", "callId": "cX", "content": "r"},
    ])
    resolve_tool_call_relations(interactions, issues)
    assert any("cX" in o for o in issues.orphan_tool_results)


def test_matched_call_relation_is_clean():
    interactions, _, issues = reduce_dsh_events({}, [
        {"type": "message", "source": "user", "text": "q"},
        {"type": "tool_call", "callId": "c1", "name": "search", "arguments": {"q": "x"}},
        {"type": "tool_result", "callId": "c1", "content": "found"},
    ])
    resolve_tool_call_relations(interactions, issues)
    assert not issues.any()


def test_tool_calls_force_agent_source_step():
    interactions, _, _ = reduce_dsh_events({}, [
        {"type": "message", "source": "user", "text": "q"},
        {"type": "tool_call", "callId": "c1", "name": "search"},
        {"type": "tool_result", "callId": "c1", "content": "r"},
        {"type": "message", "source": "assistant", "text": "done"},
    ])
    steps = normalized_interactions_to_atif_steps(interactions)
    # the assistant reply continues the agent interaction that carries
    # the tool round-trip — one agent step, fully correlated
    assert [s.source for s in steps] == ["user", "agent"]
    tool_step = steps[1]
    assert tool_step.tool_calls[0].tool_call_id == "c1"
    assert tool_step.observation.results[0].source_call_id == "c1"
    assert tool_step.message == "done"


def test_empty_session_yields_placeholder_system_step():
    steps = normalized_interactions_to_atif_steps([])
    assert len(steps) == 1
    assert steps[0].source == "system"


def test_full_conversion_passes_harbor_validator_and_records_provenance():
    response = _response([
        {"type": "message", "source": "user", "text": "refund?"},
        {"type": "tool_call", "callId": "c1", "name": "lookup", "arguments": {"id": 1}},
        {"type": "tool_result", "callId": "c1", "content": "order found"},
        {"type": "message", "source": "assistant", "text": "refunded"},
        {"type": "turn", "end": {"reason": {"kind": "end_turn"}}},
    ])
    trajectory = convert_dsh_read_to_atif(response)
    assert trajectory.agent.name == "dsh"
    assert trajectory.agent.version == "0.1.7-alpha.1"
    dsh_extra = trajectory.extra[DSH_EXTRA_KEY]
    assert dsh_extra["mapperVersion"] == MAPPER_VERSION
    assert dsh_extra["eventState"] == "shared-frozen"
    assert dsh_extra["conversionIssues"]["orphanToolResults"] == []
    assert DSH_IGNORABLE_EXTRA_KEY not in trajectory.extra


def test_conversion_carries_ignorable_events_and_issues():
    response = _response([
        {"type": "message", "source": "user", "text": "q"},
        {"type": "vendor_nudge", "ignorable": True},
        {"type": "tool_result", "callId": "ghost", "content": "orphan"},
    ])
    trajectory = convert_dsh_read_to_atif(response)
    ignorable = trajectory.extra[DSH_IGNORABLE_EXTRA_KEY]
    assert ignorable[0]["type"] == "vendor_nudge"
    issues = trajectory.extra[DSH_EXTRA_KEY]["conversionIssues"]
    assert any("ghost" in o for o in issues["orphanToolResults"])


@pytest.mark.parametrize(
    "turn_event, expected",
    [
        ({"type": "turn", "end": {"reason": {"kind": "end_turn"}}}, "agent_claimed_done"),
        ({"type": "turn", "end": {"kind": "end_turn"}}, "agent_claimed_done"),
        ({"type": "turn", "end": {"reason": {"kind": "max_tokens"}}}, "budget_exhausted"),
        ({"type": "turn", "end": {"reason": {"kind": "timeout"}}}, "timeout_killed"),
    ],
)
def test_stop_reason_from_turn_end_reason_kind(turn_event, expected):
    assert derive_stop_reason([turn_event]) == expected


def test_stop_reason_defaults_to_infra_error_never_success():
    assert derive_stop_reason([]) == "infra_error"
    assert derive_stop_reason([
        {"type": "message", "source": "user", "text": "q"},
        {"type": "turn"},  # no recognizable end reason
    ]) == "infra_error"


def test_build_canonical_transcript_completeness_tracks_evidence():
    response = _response([{"type": "message", "source": "user", "text": "q"}])
    trajectory = convert_dsh_read_to_atif(response)

    with_evidence = build_canonical_transcript(
        trajectory,
        EvidenceBundle(trial_id="t1", stop_reason="agent_exit_0"),
        "agent_exit_0",
    )
    assert with_evidence.completeness.status_of("token_usage") == "ok"

    without = build_canonical_transcript(trajectory, None, "agent_exit_0")
    assert without.completeness.status_of("token_usage") == "partial"
