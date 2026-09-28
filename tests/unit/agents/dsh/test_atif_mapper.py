"""ATIF mapper unit tests (plan §7 row 7): map the official log, invent nothing.

The mapper speaks the pinned DSH 0.1.7-alpha.1 Session V4 vocabulary, so every
fixture here is built by ``dsh_log`` (the same envelope the real
``JsonlSessionPersistence`` returns). Pinned behaviours:

- unknown REQUIRED events fail closed; unknown ignorable ones stay verbatim;
- surface reconstruction is context, never a second execution;
- tool relations are only claimed when the log proves them;
- timestamps, model identity and usage come from the log, not the wall clock;
- every emitted trajectory passes Harbor's own ``TrajectoryValidator``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aeval.agents.dsh.atif_mapper import (
    DSH_PRESERVED_EVENT_EXTRA_KEY,
    DSH_EXTRA_KEY,
    DSH_VERSION,
    MAPPER_VERSION,
    ConversionIssues,
    DshAtifConversionError,
    build_canonical_transcript,
    convert_dsh_read_to_atif,
    derive_stop_reason,
    normalized_interactions_to_atif_steps,
    reduce_dsh_events,
    resolve_tool_call_relations,
)
from aeval.agents.dsh.bridge import DshReaderResponse
from aeval.contracts import EvidenceBundle
from dsh_log import MODEL, SessionLog, happy_log, seeded_log, session_header, usage


def _response(log: SessionLog, *, header=None, inherited=0, event_state="shared-frozen") -> DshReaderResponse:
    return DshReaderResponse(
        request_id="req-1",
        header=header if header is not None else session_header(),
        inherited_event_count=inherited,
        event_state=event_state,
        events=log.events,
    )


def _dsh(trajectory) -> dict:
    return trajectory.extra[DSH_EXTRA_KEY]


def _reduce(log: SessionLog, *, header=None):
    return reduce_dsh_events(header or session_header(), log.events)


# -- surface-free reduction -------------------------------------------------
def test_happy_session_reduces_to_append_origin_interactions():
    interactions, extras, issues = _reduce(happy_log())
    assert not issues.any(), issues.to_dict()
    assert extras == []
    assert [(i.source, i.turn, i.step) for i in interactions] == [
        ("system", 0, 0), ("user", None, None), ("agent", 0, 0), ("agent", 0, 1),
    ]
    agent = interactions[2]
    assert agent.message_text == "I will list the files."
    assert [c["callId"] for c in agent.tool_calls] == ["call-1"]
    assert agent.tool_calls[0]["arguments"] == {"cmd": "ls"}
    assert agent.tool_results[0]["content"] == "answer.txt"
    assert agent.config == {"provider": "deepseek-official", "model": MODEL}


def test_unknown_required_event_fails_closed():
    log = happy_log()
    log.raw("audit/nudge", {"turn": 0})
    with pytest.raises(DshAtifConversionError, match="unknown required SessionEvent"):
        _reduce(log)


def test_unknown_ignorable_event_preserved_verbatim():
    log = happy_log()
    raw = {"vendor": "opaque", "nested": [1, 2]}
    event = log.raw("audit/nudge", raw, ignorable=True)
    _, extras, issues = _reduce(log)
    assert not issues.any(), issues.to_dict()
    assert extras == [
        {"eventIndex": event["seq"], "type": "audit/nudge", "class": "ignorable", "raw": event}
    ]
    assert extras[0]["raw"]["data"] == raw


def test_official_log_only_events_are_retained_without_failing_closed():
    # A real session writes these; refusing them would make it unconvertible.
    log = happy_log()
    title = log.raw("session/title", {"title": "list files"})
    log.raw("todo/write", {"todos": [{"content": "answer", "status": "complete"}]})
    log.raw("compaction/start", {"compactionId": "c-1", "turn": 0})
    trajectory = convert_dsh_read_to_atif(_response(log))
    preserved = trajectory.extra[DSH_PRESERVED_EVENT_EXTRA_KEY]
    assert [p["class"] for p in preserved] == ["log-only"] * 3
    assert preserved[0] == {
        "eventIndex": title["seq"], "type": "session/title", "class": "log-only", "raw": title}
    assert [s.source for s in trajectory.steps] == ["system", "user", "agent", "agent"]


def test_log_only_events_are_enough_to_make_usage_partial():
    summary = _usage_status(_with_log_only())
    assert summary["status"] == "partial"
    assert summary["totalTokens"] is None


def _with_log_only():
    log = happy_log()
    log.raw("llm/retry", {"turn": 0, "step": 1})
    return log


def test_ignorable_event_does_not_claim_to_be_settled_usage():
    log = happy_log()
    log.raw("audit/nudge", {}, ignorable=True)
    trajectory = convert_dsh_read_to_atif(_response(log))
    assert trajectory.extra[DSH_PRESERVED_EVENT_EXTRA_KEY][0]["type"] == "audit/nudge"
    assert _dsh(trajectory)["usage"]["status"] == "partial"


def test_noncontiguous_sequence_rejected():
    log = happy_log()
    log.events[4]["seq"] = 9
    with pytest.raises(DshAtifConversionError, match="noncontiguous seq"):
        _reduce(log)


def test_header_storage_version_is_pinned():
    with pytest.raises(DshAtifConversionError, match="storage version"):
        _reduce(happy_log(), header=session_header(version=3))


def test_lifecycle_events_carry_their_turn_and_step():
    log = happy_log()
    del log.events[4]["data"]["step"]  # step/start without step
    with pytest.raises(DshAtifConversionError, match="step is required"):
        _reduce(log)


# -- tool relations ---------------------------------------------------------
def _calls_and_results(*events_pairs):
    log = SessionLog()
    log.request_header()
    log.turn_start(0)
    log.step_start(0, 0)
    for call_id, name in events_pairs:
        log.tool_call(0, 0, call_id, name)
        log.tool_result(0, 0, call_id)
    log.step_end(0, 0)
    log.turn_end(0, "completed")
    return log


def test_unique_earlier_call_in_same_step_proves_the_relation():
    interactions, _, issues = _reduce(_calls_and_results(("c1", "bash")))
    resolve_tool_call_relations(interactions, issues)
    assert not issues.any(), issues.to_dict()
    steps = normalized_interactions_to_atif_steps(interactions)
    agent = [s for s in steps if s.source == "agent"][0]
    assert agent.tool_calls[0].tool_call_id == "c1"
    assert agent.observation.results[0].source_call_id == "c1"


def test_duplicate_call_ids_are_never_matched():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.tool_call(0, 0, "c1", "alpha")
    log.tool_call(0, 0, "c1", "beta")
    log.tool_result(0, 0, "c1")
    interactions, _, issues = _reduce(log)
    resolve_tool_call_relations(interactions, issues)
    assert issues.duplicate_call_ids == [
        "duplicate tool/call callId 'c1' in (0, 0)"
    ]
    assert issues.ambiguous_relations
    assert not issues.orphan_tool_results
    steps = normalized_interactions_to_atif_steps(interactions)
    assert steps[-1].observation.results[0].source_call_id is None
    assert steps[-1].observation.results[0].extra["dsh"]["relation"] == "unresolved"


def test_result_without_earlier_call_is_orphan():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.tool_result(0, 0, "ghost")
    interactions, _, issues = _reduce(log)
    resolve_tool_call_relations(interactions, issues)
    assert any("ghost" in o for o in issues.orphan_tool_results)
    assert not issues.ambiguous_relations


def test_call_and_result_in_different_steps_do_not_prove_a_relation():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.tool_call(0, 0, "c1", "bash")
    log.step_end(0, 0)
    log.step_start(0, 1)
    log.tool_result(0, 1, "c1")
    interactions, _, issues = _reduce(log)
    resolve_tool_call_relations(interactions, issues)
    assert any("c1" in o for o in issues.orphan_tool_results)


def test_result_source_call_id_must_agree_with_the_message():
    log = _calls_and_results(("c1", "bash"))
    result = [e for e in log.events if e["type"] == "tool/result"][0]
    result["data"]["message"]["source"]["callId"] = "other"
    interactions, _, issues = _reduce(log)
    resolve_tool_call_relations(interactions, issues)
    assert any("ambiguous tool result" in o for o in issues.ambiguous_relations)


def test_malformed_tool_arguments_are_kept_raw_and_flagged():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.tool_call(0, 0, "c1", "bash", "{not json")
    log.tool_call(0, 0, "c2", "bash", '["a list"]')
    interactions, _, issues = _reduce(log)
    assert len(issues.malformed_arguments) == 2
    steps = normalized_interactions_to_atif_steps(interactions)
    calls = steps[-1].tool_calls
    assert [c.arguments for c in calls] == [{}, {}]
    assert [c.extra["dsh"]["rawArguments"] for c in calls] == ["{not json", '["a list"]']
    assert [c.extra["dsh"]["argumentsDecoded"] for c in calls] == [False, False]


def test_assistant_tool_call_blocks_are_not_double_counted_as_executions():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.assistant_message(
        0, 0, blocks=[
            {"type": "text", "text": "listing"},
            {"type": "tool-call", "id": "c1", "name": "bash", "arguments": '{"cmd":"ls"}'},
        ],
        usage_report=usage(),
    )
    log.tool_call(0, 0, "c1", "bash", {"cmd": "ls"})
    log.tool_result(0, 0, "c1", "done")
    interactions, _, issues = _reduce(log)
    resolve_tool_call_relations(interactions, issues)
    assert not issues.any(), issues.to_dict()
    agent = [i for i in interactions if i.source == "agent"][0]
    assert agent.message_text == "listing"
    assert len(agent.tool_calls) == 1


def test_two_assistant_messages_in_one_step_are_flagged_not_merged_silently():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.assistant_message(0, 0, "first")
    log.assistant_message(0, 0, "second")
    interactions, _, issues = _reduce(log)
    assert any("multiple assistant messages" in o for o in issues.unmapped_events)


def test_reasoning_and_structured_blocks_render_separately():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.assistant_message(
        0, 0, blocks=[
            {"type": "reasoning", "text": "thinking"},
            {"type": "text", "text": "answer"},
            {"type": "file", "handle": "att-1", "mimeType": "image/png"},
        ],
        usage_report=usage(),
    )
    steps = normalized_interactions_to_atif_steps(_reduce(log)[0])
    agent = steps[-1]
    assert agent.reasoning_content == "thinking"
    # An attachment handle is neither a filename nor a URL: keep it visible as JSON.
    assert '"handle":"att-1"' in agent.message


# -- surface reconstruction -------------------------------------------------
def _with_replacement():
    log = SessionLog()
    log.request_header()
    log.system_message(0, 0)
    log.turn_start(0)
    log.user_message()
    log.step_start(0, 0)
    log.tool_call(0, 0, "c1", "bash")
    original = log.tool_result(0, 0, "c1", "transient failure")
    log.tool_result(
        0, 0, "c1", "final content",
        surface_op={"op": "replace", "startSeq": original["seq"], "endSeq": original["seq"]},
        source_seqs=[original["seq"]],
        message_id=original["data"]["message"]["id"],
    )
    log.step_end(0, 0)
    log.turn_end(0, "completed")
    return log


def _surface_replacement(log: SessionLog) -> dict:
    return next(e for e in log.events if isinstance(e.get("surfaceOp"), dict))


def test_surface_replacement_is_not_a_second_execution():
    log = _with_replacement()
    trajectory = convert_dsh_read_to_atif(_response(log))
    agent = [s for s in trajectory.steps if s.source == "agent"][0]
    assert agent.observation.results[0].content == "transient failure"
    assert len(agent.observation.results) == 1
    assert not _dsh(trajectory)["conversionIssues"]["ambiguousRelations"]
    surface = _dsh(trajectory)["surface"]
    assert surface["replacements"] == [
        {"seq": 7, "start": 6, "end": 6, "shadowedSeqs": [6]}
    ]
    tool_nodes = [m for m in surface["messages"] if m["message"]["role"] == "tool"]
    assert tool_nodes[0]["seq"] == 7
    assert tool_nodes[0]["message"]["content"][0]["text"] == "final content"


def test_replacement_must_cite_every_shadowed_node():
    log = SessionLog()
    log.request_header()
    log.system_message(0, 0)
    log.turn_start(0)
    first = log.user_message("q1")
    second = log.user_message("q2")
    second["surfaceOp"] = {"op": "replace", "startSeq": first["seq"], "endSeq": first["seq"]}
    second["sourceEventSeqs"] = [1]  # the system node, not the user node being shadowed
    with pytest.raises(DshAtifConversionError, match="cite every shadowed"):
        convert_dsh_read_to_atif(_response(log))


def test_tool_result_replacement_may_change_only_content():
    log = _with_replacement()
    _surface_replacement(log)["data"]["message"]["toolCallId"] = "other"
    with pytest.raises(DshAtifConversionError, match="change only content"):
        convert_dsh_read_to_atif(_response(log))


def test_assistant_messages_cannot_cite_source_events():
    log = happy_log()
    log.events[-1]["sourceEventSeqs"] = [0]  # turn/end is not surface-eligible
    with pytest.raises(DshAtifConversionError, match="not surface-eligible"):
        convert_dsh_read_to_atif(_response(log))


def test_developer_tool_addition_must_resolve_a_published_schema():
    log = SessionLog()
    log.request_header(tools=[{"name": "bash", "description": "Run a command", "parameters": {"type": "object"}}])
    log.developer_message(0, 0, [{"type": "tool-addition", "toolName": "bash"}], header_seq=0)
    interactions, _, issues = _reduce(log)
    assert not issues.any(), issues.to_dict()
    assert "bash" in interactions[0].message_text

    broken = SessionLog()
    broken.request_header(tools=[{"name": "bash", "description": "Run", "parameters": {}}])
    broken.developer_message(0, 0, [{"type": "tool-addition", "toolName": "missing"}], header_seq=0)
    with pytest.raises(DshAtifConversionError, match="resolve exactly one tool schema"):
        _reduce(broken)


# -- inheritance ------------------------------------------------------------
def test_inherited_prefix_is_context_not_a_live_execution():
    log = seeded_log()
    trajectory = convert_dsh_read_to_atif(
        _response(log, header=session_header(seeded=True), inherited=log.inherited)
    )
    copied = [s.extra["dsh"]["eventSeqs"] for s in trajectory.steps if s.is_copied_context]
    assert copied and max(max(seqs) for seqs in copied) < log.inherited
    live = [s for s in trajectory.steps if not s.is_copied_context]
    assert [s.message for s in live] == ["live question", "live answer"]
    assert [o["inherited"] for o in _dsh(trajectory)["observedModels"]] == [True, False]


def test_seeded_header_requires_the_marker_at_inheritedEventCount():
    log = seeded_log()
    with pytest.raises(DshAtifConversionError, match="requires tagged marker"):
        convert_dsh_read_to_atif(_response(log, header=session_header(seeded=True), inherited=0))


def test_unseeded_session_cannot_inherit_or_claim_a_marker():
    log = happy_log()
    with pytest.raises(DshAtifConversionError, match="unseeded session cannot inherit"):
        convert_dsh_read_to_atif(_response(log, inherited=3))

    marker = SessionLog()
    marker.request_header()
    marker.end_seed()
    with pytest.raises(DshAtifConversionError, match="unseeded session cannot carry inherited marker"):
        convert_dsh_read_to_atif(_response(marker))


# -- conversion envelope ----------------------------------------------------
def test_conversion_emits_valid_atif_with_provenance():
    trajectory = convert_dsh_read_to_atif(_response(happy_log()))
    assert trajectory.agent.name == "dsh"
    assert trajectory.agent.version == DSH_VERSION
    assert trajectory.agent.model_name == MODEL
    assert trajectory.session_id == "session-under-test"
    extra = _dsh(trajectory)
    assert extra["mapperVersion"] == MAPPER_VERSION
    assert extra["eventState"] == "shared-frozen"
    assert extra["conversionIssues"] == {k: [] for k in (
        "orphanToolResults", "duplicateCallIds", "ambiguousRelations",
        "unmappedEvents", "malformedArguments", "invalidUsage")}
    assert [s.step_id for s in trajectory.steps] == [1, 2, 3, 4]
    assert DSH_PRESERVED_EVENT_EXTRA_KEY not in trajectory.extra


def test_step_timestamps_come_from_event_time():
    log = happy_log()
    trajectory = convert_dsh_read_to_atif(_response(log))
    expected = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        milliseconds=log.events[1]["time"]
    )
    assert trajectory.steps[0].timestamp == expected.isoformat(timespec="milliseconds")


def test_impossible_usage_report_is_flagged_and_yields_no_metrics():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.assistant_message(
        0, 0, "a",
        usage_report={"inputTokens": 10, "outputTokens": 20, "reasoningTokens": 21},
    )
    interactions, _, issues = _reduce(log)
    assert issues.invalid_usage == ["event[2]: invalid assistant usage"]
    steps = normalized_interactions_to_atif_steps(interactions)
    assert steps[-1].metrics is None


def test_all_request_headers_are_observed_in_order():
    log = happy_log()
    log.request_header(reason="change", model="some-other-model")
    log.turn_start(1)
    log.assistant_message(1, 0, "again", usage_report=usage())
    trajectory = convert_dsh_read_to_atif(_response(log))
    observed = _dsh(trajectory)["observedModels"]
    assert [(o["seq"], o["reason"], o["config"]["model"]) for o in observed] == [
        (0, "initial", MODEL), (13, "change", "some-other-model")
    ]
    # The agent's identity reports the last route, claims read every snapshot.
    assert trajectory.agent.model_name == "some-other-model"


def test_empty_session_yields_one_synthetic_step():
    log = SessionLog()
    log.request_header()
    trajectory = convert_dsh_read_to_atif(_response(log))
    assert len(trajectory.steps) == 1
    assert trajectory.steps[0].source == "system"
    assert trajectory.steps[0].extra["dsh"]["synthetic"] is True


def test_unknown_event_state_is_rejected():
    with pytest.raises(DshAtifConversionError, match="invalid eventState"):
        convert_dsh_read_to_atif(_response(happy_log(), event_state="whatever"))


# -- usage completeness -----------------------------------------------------
def _usage_status(log, **kwargs):
    return _dsh(convert_dsh_read_to_atif(_response(log, **kwargs)))["usage"]


def test_exact_usage_requires_settled_steps_and_reported_totals():
    summary = _usage_status(happy_log())
    assert summary["status"] == "ok"
    # (100+5+0+20) + (140+5+0+12)
    assert summary["totalTokens"] == 282
    assert summary["promptTokens"] == 250
    assert summary["outputTokens"] == 32
    assert summary["cachedTokens"] == 10
    assert summary["eventSeqs"] == [5, 10]


def test_retry_attempts_make_usage_partial():
    log = happy_log()
    log.assistant_attempt(0, 1)
    summary = _usage_status(log)
    assert summary["status"] == "partial"
    assert summary["totalTokens"] is None
    assert summary["reportedTotalTokensSubtotal"] == 282


def test_unsettled_step_makes_usage_partial():
    log = happy_log()
    log.step_start(0, 2)
    log.assistant_message(0, 2, "cut short", usage_report=usage())
    log.turn_end(0, "interrupted")
    summary = _usage_status(log)
    assert summary["status"] == "partial"


def test_missing_cache_buckets_are_unknown_not_zero():
    log = SessionLog()
    log.request_header()
    log.step_start(0, 0)
    log.assistant_message(0, 0, "a", usage_report={"inputTokens": 10, "outputTokens": 5})
    log.step_end(0, 0)
    log.turn_end(0, "completed")
    summary = _usage_status(log)
    assert summary["status"] == "partial"
    assert summary["promptTokens"] is None
    assert summary["outputTokens"] == 5


def test_completeness_follows_usage_status_not_evidence_presence():
    exact = convert_dsh_read_to_atif(_response(happy_log()))
    attempted = happy_log()
    attempted.assistant_attempt(0, 1)
    partial = convert_dsh_read_to_atif(_response(attempted))

    assert build_canonical_transcript(
        exact, None, "agent_claimed_done"
    ).completeness.status_of("token_usage") == "ok"
    assert build_canonical_transcript(
        exact, EvidenceBundle(trial_id="t1", stop_reason="agent_claimed_done"), "agent_claimed_done"
    ).completeness.status_of("token_usage") == "ok"

    degraded = build_canonical_transcript(partial, None, "agent_claimed_done")
    assert degraded.completeness.status_of("token_usage") == "partial"
    assert degraded.completeness.worst_status == "partial"
    assert degraded.completeness.status_of("events") == "ok"


# -- stop reason ------------------------------------------------------------
@pytest.mark.parametrize(
    "kind, expected",
    [
        ("completed", "agent_claimed_done"),
        ("max-tokens", "budget_exhausted"),
        ("aborted", "infra_error"),
        ("blocked", "infra_error"),
        ("error", "infra_error"),
        ("interrupted", "infra_error"),
        ("forked", "infra_error"),
        ("undreamed-of-reason", "infra_error"),
    ],
)
def test_stop_reason_from_official_turn_end_kind(kind, expected):
    log = SessionLog()
    log.turn_start(0)
    log.turn_end(0, kind)
    assert derive_stop_reason(log.events) == expected


def test_stop_reason_never_defaults_to_success():
    assert derive_stop_reason([]) == "infra_error"
    log = happy_log()
    assert derive_stop_reason(log.events) == "agent_claimed_done"
    # An unfinished trailing turn outranks the earlier completion.
    log.turn_start(1)
    log.step_start(1, 0)
    log.assistant_message(1, 0, "still going")
    assert derive_stop_reason(log.events) == "infra_error"


def test_turn_end_without_a_matching_start_proves_nothing():
    log = SessionLog()
    log.turn_end(0, "completed")
    assert derive_stop_reason(log.events) == "infra_error"


def test_inherited_closers_do_not_claim_the_live_run_finished():
    log = seeded_log()
    assert derive_stop_reason(log.events, log.inherited) == "agent_claimed_done"
    # Keep the inherited prefix, including its own "completed" turn/end.
    log.events = log.events[:log.inherited + 4]
    assert log.events[-1]["type"] == "user/message"
    assert derive_stop_reason(log.events, log.inherited) == "infra_error"


def test_issues_dataclass_reports_any_and_dict_forms():
    issues = ConversionIssues(malformed_arguments=["x"])
    assert issues.any()
    assert issues.to_dict()["malformedArguments"] == ["x"]
    assert not issues.to_dict()["orphanToolResults"]


# --- D44: narrowed usage exactness + authoritative pre-dispatch rejections ---

import json as _json  # noqa: E402

import pytest  # noqa: E402

from aeval.agents.dsh import atif_mapper as _m  # noqa: E402
from aeval.agents.dsh.atif_mapper import (  # noqa: E402
    GATEWAY_REJECTION_LOG, _usage_summary, count_pre_dispatch_auxiliary_rejections,
)


def _ev(kind, seq, data=None, surface="append"):
    return {"seq": seq, "type": kind, "surfaceOp": surface, "data": data or {}}


def _sample(seq, turn, step, total):
    return _ev("assistant/message", seq, {
        "turn": turn, "step": step,
        "usage": {"inputTokens": total - 2, "outputTokens": 2, "totalTokens": total},
    })


def _live():
    return [
        _ev("turn/start", 1, {"turn": 1}),
        _ev("step/start", 2, {"turn": 1, "step": 1}),
        _sample(3, 1, 1, 100),
        _ev("step/end", 4, {"turn": 1, "step": 1}),
        _ev("turn/end", 5, {"turn": 1}),
        _ev("session", 6),
        _ev("permission/preset", 7, {"preset": "workspace-write"}),
        _ev("sandbox/mode", 8, {"mode": "workspace-write"}),
        _ev("approval/policy", 9, {"policy": "ask"}),
        _ev("agent/inbox/spliced", 10, {"target": "next-turn"}),
        _ev("session/title", 11, {"title": "t", "source": {"kind": "fallback"}}),
    ]


def _aux():
    return _ev("session/title-llm-request", 14, {
        "titleProvider": "session-title-first-prompt-llm", "maxTokens": 64,
    })


@pytest.fixture()
def claimed_done(monkeypatch):
    monkeypatch.setattr(
        _m, "derive_stop_reason", lambda events, inherited: "agent_claimed_done"
    )


def test_exact_usage_accepts_token_free_official_lifecycle_events(claimed_done):
    assert _usage_summary(_live(), 0)["status"] == "ok"


def test_auxiliary_request_without_authoritative_rejection_is_partial(claimed_done):
    assert _usage_summary(_live() + [_aux()], 0)["status"] == "partial"


def test_pre_dispatch_rejection_covers_the_auxiliary_request(claimed_done):
    assert _usage_summary(_live() + [_aux()], 0, 1)["status"] == "ok"
    # More requests than records: the extra one could have consumed tokens.
    assert _usage_summary(_live() + [_aux(), _ev("session/title-llm-request", 15, {})], 0, 1)["status"] == "partial"


def test_unknown_event_type_still_fails_closed(claimed_done):
    events = _live() + [_aux(), _ev("mystery/envelope", 15, {"x": 1})]
    assert _usage_summary(events, 0, 1)["status"] == "partial"
    assert _usage_summary(events, 0, 9)["status"] == "partial"


def test_attempt_is_never_covered_by_a_rejection(claimed_done):
    events = _live() + [_ev("assistant/attempt", 12, {"turn": 1, "step": 1})]
    assert _usage_summary(events, 0, 5)["status"] == "partial"


def test_count_pre_dispatch_auxiliary_rejections_reads_transport_log(tmp_path):
    log = tmp_path / GATEWAY_REJECTION_LOG
    log.write_text("\n".join([
        _json.dumps({"code": "AEVAL_LEASE_BUSY", "purpose": "session-title"}),
        _json.dumps({"code": "AEVAL_AUXILIARY_REFUSED", "purpose": ""}),
        _json.dumps({"code": "AEVAL_IDENTITY_MISMATCH", "purpose": "session-title"}),
        "{not json",
    ]), encoding="utf-8")
    assert count_pre_dispatch_auxiliary_rejections(tmp_path) == 1
    assert count_pre_dispatch_auxiliary_rejections(tmp_path / "missing") == 0
    oversized = tmp_path / "big"
    oversized.mkdir()
    (oversized / GATEWAY_REJECTION_LOG).write_text("x" * ((1 << 20) + 1), encoding="utf-8")
    assert count_pre_dispatch_auxiliary_rejections(oversized) == 0


# ---------------------------------------------------------------------------
# D47: allowed auxiliary calls are dispatched, ledgered, and accounted
# ---------------------------------------------------------------------------

def _compaction_cycle(seq):
    return [
        _ev("compaction/start", seq, {"reason": "context-limit"}),
        _ev("compaction/summary", seq + 1, {}),
        _ev("compaction/end", seq + 2, {}),
    ]


def _dispatch(purpose, *, total=50, cached=None):
    usage = {"inputTokens": 10, "outputTokens": 10, "totalTokens": total}
    if cached is not None:
        usage["cacheReadTokens"] = cached
    return {"purpose": purpose, "usage": usage}


def test_compaction_events_without_ledger_evidence_stay_partial(claimed_done):
    assert _usage_summary(_live() + _compaction_cycle(20), 0)["status"] == "partial"


def test_ledgered_compaction_call_is_covered_and_merged(claimed_done):
    summary = _usage_summary(
        _live() + _compaction_cycle(20), 0, dispatched_auxiliary=[_dispatch("compaction")]
    )
    assert summary["status"] == "ok"
    # the merged total adds the ledgered call to the settled sample
    assert summary["totalTokens"] == 100 + 50
    assert summary["reportedTotalTokensSubtotal"] == 100
    assert summary["promptTokens"] == 98 + 40
    assert summary["outputTokens"] == 2 + 10
    assert summary["dispatchedAuxiliaryCalls"] == [
        {"purpose": "compaction",
         "usage": {"prompt": 40, "output": 10, "total": 50, "cached": None}}
    ]


def test_every_compaction_cycle_needs_its_own_ledgered_call(claimed_done):
    events = _live() + _compaction_cycle(20) + _compaction_cycle(30)
    assert _usage_summary(events, 0, dispatched_auxiliary=[_dispatch("compaction")])["status"] == "partial"
    assert _usage_summary(
        events, 0,
        dispatched_auxiliary=[_dispatch("compaction"), _dispatch("compaction", total=60)],
    )["status"] == "ok"


def test_ledgered_compaction_without_session_events_is_still_accounted(claimed_done):
    """A dispatch with no matching session events is surplus model work —
    counted in the totals, never silently dropped."""
    summary = _usage_summary(_live(), 0, dispatched_auxiliary=[_dispatch("compaction")])
    assert summary["status"] == "ok"
    assert summary["totalTokens"] == 150


def test_dispatched_session_title_covers_its_request_event(claimed_done):
    summary = _usage_summary(
        _live() + [_aux()], 0, dispatched_auxiliary=[_dispatch("session-title")]
    )
    assert summary["status"] == "ok"
    assert summary["totalTokens"] == 100 + 50


def test_dispatch_ledger_never_covers_unknown_or_attempt_events(claimed_done):
    unknown = _live() + [_ev("mystery/envelope", 15, {"x": 1})]
    assert _usage_summary(unknown, 0, dispatched_auxiliary=[_dispatch("compaction")])["status"] == "partial"
    attempt = _live() + [_ev("assistant/attempt", 12, {"turn": 1, "step": 1})]
    assert _usage_summary(attempt, 0, dispatched_auxiliary=[_dispatch("compaction")])["status"] == "partial"


def test_invalid_dispatch_records_do_not_count_as_coverage(claimed_done):
    bad_purpose = [{"purpose": "research", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}]
    bad_usage = [{"purpose": "compaction", "usage": {"inputTokens": 1}}]
    not_total = [{"purpose": "compaction", "usage": {"inputTokens": 1, "outputTokens": 1}}]
    for record in (bad_purpose, bad_usage, not_total):
        assert _usage_summary(
            _live() + _compaction_cycle(20), 0, dispatched_auxiliary=record
        )["status"] == "partial"


def test_cached_tokens_merge_all_or_nothing(claimed_done):
    cached_sample = _ev("assistant/message", 3, {
        "turn": 1, "step": 1,
        "usage": {"inputTokens": 40, "cacheReadTokens": 55, "cacheWriteTokens": 5,
                  "outputTokens": 2, "totalTokens": 102},
    })
    events = [
        _ev("turn/start", 1, {"turn": 1}),
        _ev("step/start", 2, {"turn": 1, "step": 1}),
        cached_sample,
        _ev("step/end", 4, {"turn": 1, "step": 1}),
        _ev("turn/end", 5, {"turn": 1}),
    ]
    merged = _usage_summary(
        events, 0, dispatched_auxiliary=[_dispatch("compaction", total=50, cached=30)]
    )
    assert merged["status"] == "ok"
    assert merged["cachedTokens"] == 55 + 30


def test_read_dispatched_auxiliary_calls_parses_the_transport_log(tmp_path):
    import json

    from aeval.agents.dsh.atif_mapper import read_dispatched_auxiliary_calls
    good = {"code": "AEVAL_AUXILIARY_DISPATCHED", "purpose": "compaction",
            "usage": {"inputTokens": 10, "cacheReadTokens": 30, "cacheWriteTokens": 0,
                      "outputTokens": 10, "totalTokens": 50}}
    lines = [
        json.dumps(good),
        json.dumps({"code": "AEVAL_AUXILIARY_DISPATCHED", "purpose": "research",
                    "usage": good["usage"]}),                     # unknown purpose
        json.dumps({"code": "AEVAL_AUXILIARY_REFUSED", "purpose": "compaction"}),  # wrong code
        json.dumps({"code": "AEVAL_AUXILIARY_DISPATCHED", "purpose": "session-title",
                    "usage": {"inputTokens": 3}}),                # invalid usage
        "{not json",
    ]
    (tmp_path / "gateway_aux_dispatches.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    calls = read_dispatched_auxiliary_calls(tmp_path)
    assert calls == [{"purpose": "compaction",
                      "usage": {"prompt": 40, "output": 10, "total": 50, "cached": 30}}]
    # a missing or oversized ledger is absent evidence, not an error
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    assert read_dispatched_auxiliary_calls(empty) == []
