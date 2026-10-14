"""The facade call ledger as evidence: parsing, honest rules, transcript folding.

The point of these tests is the SET of refusals, not just the happy path: a
partial total is worse than no total, a guessed step mapping is worse than a
null timestamp, and corrupt evidence must never be reported as measured fact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory

from aeval.agents.contract import SuiteError, sandbox_model_call_ledger
from aeval.contracts import (
    CanonicalTranscript,
    CompletenessRecord,
    FieldCompleteness,
)
from aeval.hooks.collection import read_model_call_ledger
from aeval.hooks.model_call_ledger import (
    LEDGER_SCHEMA_VERSION,
    enrich_transcript_with_call_ledger,
    missing_call_ledger,
    parse_call_ledger,
)

_COMPLETE_USAGE = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}


def ledger_line(
    *,
    call_id: str = "call-1",
    started_at: str = "2026-10-11T16:35:01.123Z",
    ended_at: str = "2026-10-11T16:35:03.456Z",
    tool_calls: list | None = None,
    tool_payloads: list | None = None,
    usage: dict | None = _COMPLETE_USAGE,
    status: str = "ok",
    error_code: str | None = None,
    schema_version: int = LEDGER_SCHEMA_VERSION,
    first_token_ms: int | None = None,
    first_output_at: str | None = None,
) -> str:
    record: dict = {
        "schema_version": schema_version,
        "call_id": call_id,
        "protocol": "responses",
        "model": "deepseek-chat",
        "provider": "deepseek",
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": 2333,
        "status": status,
        # Payloads, as the facade writes them: the wire name differs from the
        # ACP kind the trajectory records, so the payload is the verifiable link.
        "tool_calls": (
            [
                {"name": name, "arguments": payload}
                for name, payload in zip(
                    tool_calls if tool_calls is not None else [],
                    tool_payloads if tool_payloads is not None else [{}] * len(tool_calls or []),
                )
            ]
        ),
    }
    if usage is not None:
        record["usage"] = usage
    if error_code is not None:
        record["error_code"] = error_code
    if first_token_ms is not None:
        record["first_token_ms"] = first_token_ms
    if first_output_at is not None:
        record["first_output_at"] = first_output_at
    return json.dumps(record)


def transcript_with(
    steps: list[Step], completeness: CompletenessRecord | None = None
) -> CanonicalTranscript:
    return CanonicalTranscript(
        atif=Trajectory(agent=Agent(name="deepagents-code", version="0.1.78"), steps=steps),
        stop_reason="agent_claimed_done",
        completeness=completeness or CompletenessRecord(fields=[
            FieldCompleteness(field="events", status="ok"),
            FieldCompleteness(field="token_usage", status="partial", reason="no live coverage"),
        ]),
    )


def token_reason(transcript: CanonicalTranscript) -> str | None:
    return next(
        f.reason for f in transcript.completeness.fields if f.field == "token_usage"
    )


def agent_step(step_id: int, *names: str, arguments: dict | None = None) -> Step:
    return Step(
        step_id=step_id, source="agent", message=f"step {step_id}",
        tool_calls=[
            ToolCall(
                tool_call_id=f"c{index}", function_name=name,
                arguments=arguments if arguments is not None else {},
            )
            for index, name in enumerate(names)
        ],
    )


def user_step(step_id: int = 1) -> Step:
    return Step(step_id=step_id, source="user", message="do the task")


# --- parsing -----------------------------------------------------------------

def test_a_well_formed_ledger_parses_into_metered_calls():
    ledger = parse_call_ledger(
        ledger_line(tool_calls=["execute"]), source="/logs/agent/model_calls.jsonl"
    )
    assert ledger.usable and not ledger.issues and not ledger.missing
    call = ledger.calls[0]
    assert [tool.name for tool in call.tool_calls] == ["execute"]
    assert call.metered
    assert (call.prompt_tokens, call.completion_tokens) == (100, 10)
    assert call.started_at.isoformat(timespec="milliseconds") == "2026-10-11T16:35:01.123+00:00"


def test_an_unknown_schema_version_is_reported_not_interpreted():
    ledger = parse_call_ledger(ledger_line(schema_version=99), source="ledger")
    assert not ledger.usable
    assert "schema_version" in ledger.issues[0]


def test_a_malformed_line_is_reported_and_skipped():
    text = "\n".join([ledger_line(), "{not json", ledger_line(call_id="call-2")])
    ledger = parse_call_ledger(text, source="ledger")
    assert [call.call_id for call in ledger.calls] == ["call-1", "call-2"]
    assert len(ledger.issues) == 1 and ledger.issues[0].startswith("line 2:")


def test_a_bad_timestamp_or_tool_list_is_reported_not_guessed():
    bad_time = parse_call_ledger(ledger_line(started_at="yesterday"), source="ledger")
    assert not bad_time.usable and "started_at" in bad_time.issues[0]

    bad_tools = parse_call_ledger(ledger_line(tool_calls=[1, 2]), source="ledger")
    assert not bad_tools.usable and "tool_calls" in bad_tools.issues[0]


def test_a_boolean_is_not_a_token_count():
    ledger = parse_call_ledger(
        ledger_line(usage={"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2}),
        source="ledger",
    )
    assert not ledger.usable and "non-negative integer" in ledger.issues[0]


# --- totals ------------------------------------------------------------------

def test_totals_require_every_completed_call_to_report_usage():
    complete = parse_call_ledger(
        "\n".join([
            ledger_line(),
            ledger_line(call_id="call-2", usage={
                "prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55,
            }),
        ]),
        source="ledger",
    )
    totals = complete.totals()
    assert totals is not None
    assert (totals.prompt_tokens, totals.completion_tokens, totals.total_tokens) == (150, 15, 165)

    # A completed call the broker never metered: summing anyway would understate
    # spend, so no total is stated at all.
    partial = parse_call_ledger(
        "\n".join([ledger_line(), ledger_line(call_id="call-2", usage=None)]),
        source="ledger",
    )
    assert partial.totals() is None
    assert len(partial.unmetered_ok_calls()) == 1


def test_a_failed_call_is_excluded_from_the_totals():
    ledger = parse_call_ledger(
        "\n".join([
            ledger_line(),
            ledger_line(
                call_id="call-2", status="error", error_code="model_not_found",
                usage={"prompt_tokens": 9, "completion_tokens": 9, "total_tokens": 18},
            ),
        ]),
        source="ledger",
    )
    assert (len(ledger.calls), len(ledger.ok_calls)) == (2, 1)
    assert ledger.totals().total_tokens == 110


def test_a_cached_total_is_stated_only_when_every_call_reported_cache():
    cached = parse_call_ledger(
        "\n".join([
            ledger_line(usage={
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cached_tokens": 60,
            }),
            ledger_line(call_id="call-2", usage={
                "prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22, "cached_tokens": 5,
            }),
        ]),
        source="ledger",
    )
    assert cached.totals().cached_tokens == 65

    unknown = parse_call_ledger(
        "\n".join([
            ledger_line(usage={
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cached_tokens": 60,
            }),
            ledger_line(call_id="call-2", usage={
                "prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22,
            }),
        ]),
        source="ledger",
    )
    assert unknown.totals().cached_tokens is None
    assert unknown.totals().total_tokens == 132


# --- transcript folding ------------------------------------------------------

def test_verified_mapping_attaches_step_timestamps_usage_and_totals():
    steps = [user_step(), agent_step(2, "execute"), agent_step(3, "edit", "read")]
    ledger = parse_call_ledger(
        "\n".join([
            ledger_line(tool_calls=["execute"], usage={
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cached_tokens": 40,
            }),
            ledger_line(
                call_id="call-2", tool_calls=["edit", "read"],
                started_at="2026-10-11T16:35:10.000Z", ended_at="2026-10-11T16:35:12.000Z",
                usage={
                    "prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220, "cached_tokens": 10,
                },
            ),
        ]),
        source="/logs/agent/model_calls.jsonl",
    )
    original = transcript_with(steps)
    enriched = enrich_transcript_with_call_ledger(original, ledger)

    assert enriched is not original
    assert enriched.atif.notes is not None and "facade call ledger" in enriched.atif.notes
    assert [s.timestamp for s in enriched.atif.steps] == [
        "2026-10-11T16:35:01.123+00:00",   # the first step: first metered call's start
        "2026-10-11T16:35:01.123+00:00",
        "2026-10-11T16:35:10.000+00:00",
    ]
    second = enriched.atif.steps[2].metrics
    assert (second.prompt_tokens, second.completion_tokens, second.cached_tokens) == (200, 20, 10)
    assert second.cost_usd is None  # never invented
    final = enriched.atif.final_metrics
    assert (final.total_prompt_tokens, final.total_completion_tokens, final.total_cached_tokens) == (300, 30, 50)
    assert final.total_cost_usd is None
    assert enriched.completeness.status_of("token_usage") == "ok"
    # the transcript the adapter cached is untouched
    assert all(step.timestamp is None for step in original.atif.steps)


def test_first_token_latency_lands_in_the_step_metrics_extra():
    """首 token 时延随步骤落进 metrics.extra（轨迹 schema 只允许 extra 扩展）。"""
    steps = [user_step(), agent_step(2, "execute"), agent_step(3, "execute")]
    ledger = parse_call_ledger(
        "\n".join([
            ledger_line(
                tool_calls=["execute"],
                first_token_ms=812,
                first_output_at="2026-10-11T16:35:01.935Z",
            ),
            ledger_line(
                call_id="call-2", tool_calls=["execute"],
                started_at="2026-10-11T16:35:10.000Z", ended_at="2026-10-11T16:35:12.000Z",
            ),
        ]),
        source="/logs/agent/model_calls.jsonl",
    )
    assert ledger.calls[0].first_token_ms == 812
    assert ledger.calls[1].first_token_ms is None  # 没记录 ≠ 0

    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    first = enriched.atif.steps[1].metrics
    assert first.extra == {
        "first_token_ms": 812,
        "first_output_at": "2026-10-11T16:35:01.935+00:00",
    }
    # usage 与 extra 共存
    assert (first.prompt_tokens, first.completion_tokens) == (100, 10)
    assert "first_token_ms" in (enriched.atif.notes or "")
    # 第二个调用没有时延记录：那一步不带这个字段（不写 0）
    second = enriched.atif.steps[2].metrics
    assert second is not None and not (second.extra or {}).get("first_token_ms")


def test_an_unmetered_call_can_still_carry_first_token_latency():
    steps = [user_step(), agent_step(2, "execute")]
    ledger = parse_call_ledger(
        ledger_line(tool_calls=["execute"], usage=None, first_token_ms=500),
        source="ledger",
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert enriched.atif.steps[1].metrics.extra == {"first_token_ms": 500}
    assert enriched.atif.steps[1].metrics.prompt_tokens is None


def test_the_pairing_is_by_payload_so_a_wire_name_may_differ_from_the_recorded_one():
    # Measured on the lab: the model asks for ``write_file`` while the sealed
    # trajectory records the ACP kind ``edit``; the arguments are identical.
    steps = [user_step(), agent_step(2, "edit", arguments={"file_path": "/app/hello.txt", "content": "hi"})]
    ledger = parse_call_ledger(
        ledger_line(
            tool_calls=["write_file"],
            tool_payloads=[{"file_path": "/app/hello.txt", "content": "hi"}],
        ),
        source="ledger",
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert enriched.atif.steps[1].timestamp == "2026-10-11T16:35:01.123+00:00"
    assert enriched.completeness.status_of("token_usage") == "ok"


def test_a_payload_mismatch_refuses_the_pairing():
    steps = [user_step(), agent_step(2, "edit", arguments={"file_path": "/app/hello.txt", "content": "hi"})]
    ledger = parse_call_ledger(
        ledger_line(
            tool_calls=["write_file"],
            tool_payloads=[{"file_path": "/app/other.txt", "content": "hi"}],
        ),
        source="ledger",
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert all(step.timestamp is None for step in enriched.atif.steps)
    assert "different arguments" in enriched.atif.notes


def test_an_unverifiable_payload_with_a_different_name_refuses_but_still_totals():
    # No parsed payload on the ledger side and different names: there is no
    # honest link between the call and the step, so no timestamp is attached —
    # while the trial-level totals (which need no per-step link) are stated.
    steps = [user_step(), agent_step(2, "execute", arguments={"command": "ls"})]
    ledger = parse_call_ledger(
        json.dumps({
            "schema_version": LEDGER_SCHEMA_VERSION, "call_id": "c", "protocol": "responses",
            "model": "m", "provider": "p", "started_at": "2026-10-11T16:35:01.123Z",
            "ended_at": "2026-10-11T16:35:02.000Z", "duration_ms": 877, "status": "ok",
            "tool_calls": [{"name": "write_file", "arguments_raw": "{oops"}],
            "usage": _COMPLETE_USAGE,
        }),
        source="ledger",
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert [s.timestamp for s in enriched.atif.steps] == [None, None]
    assert enriched.atif.steps[1].metrics is None
    assert enriched.atif.final_metrics.total_prompt_tokens == 100
    assert "not attached" in enriched.atif.notes
    assert enriched.completeness.status_of("token_usage") == "ok"


def test_a_count_mismatch_refuses_the_mapping():
    steps = [user_step(), agent_step(2, "execute", arguments={}), agent_step(3, "execute", arguments={})]
    ledger = parse_call_ledger(
        ledger_line(tool_calls=["execute"], tool_payloads=[{}]), source="ledger"
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert all(step.timestamp is None for step in enriched.atif.steps)
    assert "one-to-one mapping is not verifiable" in enriched.atif.notes


def test_an_unmetered_completed_call_keeps_token_usage_partial_but_still_times_the_step():
    steps = [user_step(), agent_step(2, "execute")]
    ledger = parse_call_ledger(
        ledger_line(tool_calls=["execute"], usage=None), source="ledger"
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert enriched.atif.final_metrics is None
    assert enriched.completeness.status_of("token_usage") == "partial"
    assert "reported no usage" in str(token_reason(enriched))
    # the call happened, so its verified timestamp is still evidence
    assert enriched.atif.steps[1].timestamp == "2026-10-11T16:35:01.123+00:00"
    assert enriched.atif.steps[1].metrics is None


def test_a_missing_ledger_leaves_the_transcript_alone():
    transcript = transcript_with([user_step(), agent_step(2, "execute")])
    enriched = enrich_transcript_with_call_ledger(
        transcript, missing_call_ledger("/logs/agent/model_calls.jsonl")
    )
    assert enriched is transcript
    assert enriched.completeness.status_of("token_usage") == "partial"


def test_a_name_only_tool_call_still_verifies_against_a_matching_step_name():
    steps = [user_step(), agent_step(2, "execute")]
    ledger = parse_call_ledger(
        json.dumps({
            "schema_version": LEDGER_SCHEMA_VERSION, "call_id": "c", "protocol": "responses",
            "model": "m", "provider": "p", "started_at": "2026-10-11T16:35:01.123Z",
            "ended_at": "2026-10-11T16:35:02.000Z", "duration_ms": 877, "status": "ok",
            "tool_calls": ["execute"], "usage": _COMPLETE_USAGE,
        }),
        source="ledger",
    )
    enriched = enrich_transcript_with_call_ledger(transcript_with(steps), ledger)
    assert enriched.atif.steps[1].timestamp == "2026-10-11T16:35:01.123+00:00"


def test_a_ledger_with_no_readable_call_degrades_with_the_reason():
    transcript = transcript_with([user_step(), agent_step(2, "execute")])
    ledger = parse_call_ledger("{oops", source="/logs/agent/model_calls.jsonl")
    enriched = enrich_transcript_with_call_ledger(transcript, ledger)
    assert enriched.completeness.status_of("token_usage") == "partial"
    reason = str(token_reason(enriched))
    assert "no readable metered call" in reason and "line 1" in reason


def test_mock_call_records_pass_the_ledger_through_and_report_issues():
    ledger = parse_call_ledger("\n".join([ledger_line(), "garbage"]), source="ledger")
    records = ledger.mock_call_records()
    assert records[0]["schema_version"] == LEDGER_SCHEMA_VERSION
    assert records[0]["usage"]["prompt_tokens"] == 100
    assert records[1]["event"] == "ledger_lines_unreadable"
    assert records[1]["issues"][0].startswith("line 2:")


# --- the store view must agree with the sealed evidence -----------------------

class _State:
    def __init__(self, trial_dir) -> None:
        self.trial_dir = trial_dir
        self.trial_id = "trial-1"
        self.evidence_issues: list[str] = []


class _RawSessionAgent:
    def __init__(self, transcript) -> None:
        self._transcript = transcript

    def read_trial_session(self):
        return self._transcript


class _Environments:
    def __init__(self, agent) -> None:
        self._agent = agent

    def agent(self, trial_id: str):
        return self._agent


class _Context:
    def __init__(self, agent=None) -> None:
        self.environments = _Environments(agent) if agent is not None else None


def _seal(trial_dir, transcript) -> None:
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "canonical_transcript.json").write_text(
        transcript.model_dump_json(indent=2), encoding="utf-8"
    )


def test_the_store_view_prefers_the_sealed_completeness(tmp_path):
    from aeval.hooks.plugin import _transcript_extra

    # The live session read is the pre-collection view (partial); the sealed
    # transcript is what was graded, and the ledger was folded into it.
    raw = transcript_with([user_step(), agent_step(2, "edit")])
    sealed = transcript_with(
        [user_step(), agent_step(2, "edit")],
        CompletenessRecord(fields=[
            FieldCompleteness(field="events", status="ok"),
            FieldCompleteness(field="token_usage", status="ok", reason="sealed ledger"),
        ]),
    )
    _seal(tmp_path / "trial", sealed)
    state = _State(tmp_path / "trial")
    extra = _transcript_extra(_Context(_RawSessionAgent(raw)), state)
    assert extra is not None
    assert extra["aeval"]["completeness"]["fields"][1]["status"] == "ok"
    assert extra["aeval"]["stop_reason"] == "agent_claimed_done"
    # the live read is not even consulted
    assert state.evidence_issues == []


def test_the_store_view_falls_back_to_the_live_read_without_a_sealed_file(tmp_path):
    from aeval.hooks.plugin import _transcript_extra

    raw = transcript_with([user_step(), agent_step(2, "edit")])
    extra = _transcript_extra(_Context(_RawSessionAgent(raw)), _State(tmp_path / "trial"))
    assert extra is not None
    assert extra["aeval"]["completeness"]["fields"][1]["status"] == "partial"


def test_a_sealed_file_without_completeness_does_not_mask_the_live_read(tmp_path):
    from aeval.hooks.plugin import _transcript_extra

    trial = tmp_path / "trial"
    trial.mkdir(parents=True)
    (trial / "canonical_transcript.json").write_text(
        json.dumps({"stop_reason": "agent_exit_0"}), encoding="utf-8"
    )
    raw = transcript_with([user_step(), agent_step(2, "edit")])
    extra = _transcript_extra(_Context(_RawSessionAgent(raw)), _State(trial))
    assert extra is not None
    assert extra["aeval"]["completeness"]["fields"][1]["status"] == "partial"
    # the fallback keeps the raw stop reason, not the half-sealed one
    assert extra["aeval"]["stop_reason"] == "agent_claimed_done"


# --- the collector's opt-in reader -------------------------------------------

class _NoLedger:
    pass


class _DeclaredLedger:
    SANDBOX_MODEL_CALL_LEDGER = "/logs/agent/model_calls.jsonl"


class _FakeEnvironment:
    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    async def download_file(self, source_path: str, target_path) -> None:
        self.calls.append((source_path, str(target_path)))
        if self.content is None:
            raise RuntimeError("no such file")
        Path(target_path).write_text(self.content, encoding="utf-8")


async def test_an_adapter_without_a_declared_ledger_reads_nothing(tmp_path):
    environment = _FakeEnvironment(ledger_line())
    assert await read_model_call_ledger(environment, _NoLedger(), tmp_path) is None
    assert environment.calls == []


async def test_a_declared_ledger_is_downloaded_parsed_and_placed_beside_the_logs(tmp_path):
    environment = _FakeEnvironment(ledger_line(tool_calls=["execute"]))
    ledger = await read_model_call_ledger(environment, _DeclaredLedger(), tmp_path)
    assert ledger is not None and ledger.usable
    assert environment.calls == [(
        "/logs/agent/model_calls.jsonl",
        str(tmp_path / "agent" / "model_calls.jsonl"),
    )]
    # The raw file is left where Harbor's own log download would put it.
    assert (tmp_path / "agent" / "model_calls.jsonl").is_file()


async def test_a_declared_but_absent_ledger_is_recorded_as_missing(tmp_path):
    ledger = await read_model_call_ledger(_FakeEnvironment(None), _DeclaredLedger(), tmp_path)
    assert ledger is not None and ledger.missing and not ledger.usable


def test_only_a_declaring_adapter_has_a_ledger_and_the_path_must_be_absolute():
    from aeval.agents.deepagent.agent import DcodeAgent
    from aeval.agents.dsh.agent import DshAgent

    assert sandbox_model_call_ledger(DcodeAgent) == "/logs/agent/model_calls.jsonl"
    assert sandbox_model_call_ledger(DshAgent) is None

    class Misdeclared:
        SANDBOX_MODEL_CALL_LEDGER = "logs/agent/model_calls.jsonl"

    with pytest.raises(SuiteError):
        sandbox_model_call_ledger(Misdeclared)
