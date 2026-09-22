"""Official DSH session read → ATIF mapping (plan §3).

Mapping rules (all locked to DSH 0.1.7-alpha.1's SessionEvent types):

- ``system`` / ``user`` / ``assistant`` messages map to ATIF step
  ``source`` values (``system`` / ``user`` / ``agent``).
- tool call/result pairs are correlated by explicit ``callId``; orphan,
  duplicate or ambiguous relations are NEVER guessed — the original
  relation is preserved and a conversion issue is recorded.
- ``turn``/``end.reason.kind`` maps ONLY to the stop reason.
- token/cost accounting comes from ``request/header.reason=initial``
  entries; bypass ``session/title*`` calls are accounted separately.
- unknown-but-ignorable events are preserved verbatim into
  ``extra.dsh_ignorable_events`` — never dropped.
- the DSH header/eventState go verbatim into ``extra.dsh`` as
  provenance.
- every ATIF output must pass harbor's TrajectoryValidator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from harbor.models.trajectories import Agent, ObservationResult, Step, ToolCall, Trajectory
from harbor.models.trajectories.observation import Observation
from harbor.utils.trajectory_validator import TrajectoryValidator

from aeval.agents.dsh.bridge import DshReaderResponse
from aeval.contracts import (
    AEVAL_EXTRA_KEY,
    DSH_EXTRA_KEY,
    DSH_IGNORABLE_EXTRA_KEY,
    CanonicalTranscript,
    CompletenessRecord,
    EvidenceBundle,
    FieldCompleteness,
    StopReason,
)

__all__ = [
    "MAPPER_VERSION",
    "DshAtifConversionError",
    "NormalizedInteraction",
    "NormalizedEvent",
    "ConversionIssues",
    "convert_dsh_read_to_atif",
    "reduce_dsh_events",
    "map_known_session_event",
    "resolve_tool_call_relations",
    "normalized_interactions_to_atif_steps",
    "preserve_ignorable_event",
    "build_canonical_transcript",
    "validate_canonical_transcript",
]

MAPPER_VERSION = "dsh-official-session-persistence@0.1.7-alpha.1"

# SessionEvent.type values the locked DSH release defines. Anything
# else with ignorable=true is preserved; anything else with
# ignorable=false/unknown is a conversion error (fail-closed).
KNOWN_EVENT_TYPES = frozenset(
    {
        "message",
        "tool_call",
        "tool_result",
        "turn",
        "request",
        "usage_update",
    }
)

MESSAGE_SOURCE_MAP = {"system": "system", "user": "user", "assistant": "agent"}

STOP_REASON_MAP = {
    "end_turn": "agent_claimed_done",
    "max_tokens": "budget_exhausted",
    "timeout": "timeout_killed",
}


class DshAtifConversionError(ValueError):
    """The session read cannot be mapped without guessing.

    Never mapped to a score: the trial becomes infra_invalid (required
    unknown event, undecodable payload) — or cannot_judge when only
    downstream rubric fields are affected.
    """


@dataclass
class NormalizedEvent:
    index: int
    kind: str  # message|tool_call|tool_result|turn|request|usage_update|ignorable
    payload: dict[str, Any]
    ignorable: bool = False


@dataclass
class NormalizedInteraction:
    """One ATIF step's worth of normalized DSH events."""

    source: str  # system|user|agent
    message_text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    raw_events: list[NormalizedEvent] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(
            self.message_text or self.tool_calls or self.tool_results
        )


@dataclass
class ConversionIssues:
    orphan_tool_results: list[str] = field(default_factory=list)
    duplicate_call_ids: list[str] = field(default_factory=list)
    ambiguous_relations: list[str] = field(default_factory=list)
    unmapped_events: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(
            self.orphan_tool_results
            or self.duplicate_call_ids
            or self.ambiguous_relations
            or self.unmapped_events
        )


def _event_type(event: dict[str, Any]) -> tuple[str, bool]:
    """Return (type, ignorable). Unknown type + not ignorable → error."""
    etype = event.get("type")
    ignorable = bool(event.get("ignorable", False))
    if etype not in KNOWN_EVENT_TYPES and not ignorable:
        raise DshAtifConversionError(
            f"unknown required SessionEvent type {etype!r} at index — "
            "the official reader should have rejected this; refusing to map"
        )
    return str(etype), ignorable


def preserve_ignorable_event(event_index: int, event: dict[str, Any]) -> dict[str, Any]:
    """Verbatim preservation of unknown-but-ignorable events."""
    return {"eventIndex": event_index, "type": event.get("type"), "raw": event}


def map_known_session_event(event: dict[str, Any]) -> NormalizedEvent | None:
    etype, ignorable = _event_type(event)
    if ignorable:
        return NormalizedEvent(index=-1, kind="ignorable", payload=event, ignorable=True)
    return NormalizedEvent(index=-1, kind=etype, payload=event)


def reduce_dsh_events(
    header: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[list[NormalizedInteraction], list[dict[str, Any]], ConversionIssues]:
    """Reduce the official event stream into interactions + preserved extras."""
    issues = ConversionIssues()
    interactions: list[NormalizedInteraction] = []
    ignorable_extras: list[dict[str, Any]] = []

    current: NormalizedInteraction | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None and current.has_content:
            interactions.append(current)
        current = None

    for index, event in enumerate(events):
        normalized = map_known_session_event(event)
        if normalized is None:
            continue
        normalized.index = index
        if normalized.ignorable:
            ignorable_extras.append(preserve_ignorable_event(index, event))
            continue
        kind = normalized.kind
        payload = normalized.payload

        if kind == "message":
            source = MESSAGE_SOURCE_MAP.get(str(payload.get("source", "")))
            if source is None:
                issues.unmapped_events.append(
                    f"event[{index}]: message source {payload.get('source')!r}"
                )
                continue
            if source in ("system", "user") or current is None:
                _flush()
                current = NormalizedInteraction(source=source)
            elif current.source != source:
                _flush()
                current = NormalizedInteraction(source=source)
            text = payload.get("text") or payload.get("content")
            if text is not None:
                if current.message_text is None:
                    current.message_text = str(text)
                else:
                    current.message_text += "\n" + str(text)
            current.raw_events.append(normalized)

        elif kind == "tool_call":
            # ATIF: tool_calls only apply to source='agent' steps.
            if current is None or current.source != "agent":
                _flush()
                current = NormalizedInteraction(source="agent")
            call_id = payload.get("callId") or payload.get("id")
            if call_id is None:
                issues.ambiguous_relations.append(
                    f"event[{index}]: tool_call without callId"
                )
                call_id = f"__unidentified_{index}"
            current.tool_calls.append(
                {
                    "callId": str(call_id),
                    "name": payload.get("name"),
                    "arguments": payload.get("arguments") or payload.get("input"),
                    "index": index,
                }
            )
            current.raw_events.append(normalized)

        elif kind == "tool_result":
            if current is None or current.source != "agent":
                _flush()
                current = NormalizedInteraction(source="agent")
            call_id = payload.get("callId") or payload.get("id")
            if call_id is None:
                issues.orphan_tool_results.append(
                    f"event[{index}]: tool_result without callId"
                )
                call_id = f"__orphan_{index}"
            current.tool_results.append(
                {
                    "callId": str(call_id),
                    "content": payload.get("content") or payload.get("result"),
                    "isError": bool(payload.get("isError", False)),
                    "index": index,
                }
            )
            current.raw_events.append(normalized)

        elif kind in ("turn", "request", "usage_update"):
            # Accounting/lifecycle events: attached as raw provenance to
            # the current interaction; stop-reason handled separately.
            if current is None:
                current = NormalizedInteraction(source="agent")
            current.raw_events.append(normalized)

        else:  # pragma: no cover - kind set is closed above
            issues.unmapped_events.append(f"event[{index}]: kind {kind!r}")

    _flush()
    return interactions, ignorable_extras, issues


def resolve_tool_call_relations(
    interactions: list[NormalizedInteraction],
    issues: ConversionIssues,
) -> None:
    """Correlate tool results to calls by explicit callId — never guess.

    Duplicates and orphans are recorded as issues; the original payloads
    are kept so a strict grader can downgrade to cannot_judge instead of
    trusting a fabricated relation.
    """
    for interaction in interactions:
        seen: set[str] = set()
        for call in interaction.tool_calls:
            call_id = call["callId"]
            if call_id in seen:
                issues.duplicate_call_ids.append(
                    f"duplicate tool_call callId {call_id!r}"
                )
            seen.add(call_id)
        for result in interaction.tool_results:
            if result["callId"] not in seen and not result["callId"].startswith("__orphan"):
                issues.orphan_tool_results.append(
                    f"tool_result references unknown callId {result['callId']!r}"
                )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_call_model(call: dict[str, Any], seq: int) -> ToolCall:
    return ToolCall(
        tool_call_id=str(call["callId"]),
        function_name=str(call.get("name") or "unknown"),
        arguments=(call.get("arguments") if isinstance(call.get("arguments"), dict) else {}),
    )


def normalized_interactions_to_atif_steps(
    interactions: list[NormalizedInteraction],
) -> list[Step]:
    steps: list[Step] = []
    for interaction in interactions:
        tool_calls = [
            _tool_call_model(c, i) for i, c in enumerate(interaction.tool_calls)
        ]
        # Harbor's Trajectory model requires every ObservationResult to
        # reference a tool_call of the SAME step. Orphan results (no
        # matching call in this interaction) must not fabricate a
        # relation: their content is inlined verbatim into the message
        # and the orphan status stays in conversionIssues.
        call_ids = {c["callId"] for c in interaction.tool_calls}
        matched = [r for r in interaction.tool_results if r["callId"] in call_ids]
        orphan = [r for r in interaction.tool_results if r["callId"] not in call_ids]
        results = [
            ObservationResult(
                source_call_id=str(r["callId"]),
                content=str(r["content"]) if r["content"] is not None else "",
            )
            for r in matched
        ]
        observation = Observation(results=results) if results else None
        message: Any = interaction.message_text
        if message is None and (tool_calls or observation is not None):
            message = ""  # step carries only tool content — ATIF still needs a message
        if orphan:
            orphan_lines = [
                f"[unrelated tool_result callId={r['callId']}] "
                f"{r['content'] if r['content'] is not None else ''}"
                for r in orphan
            ]
            prefix = f"{message}\n" if message else ""
            message = prefix + "\n".join(orphan_lines)
        if message is None and observation is None:
            continue
        steps.append(
            Step(
                step_id=len(steps) + 1,
                source=interaction.source,  # type: ignore[arg-type]
                message=message,
                tool_calls=tool_calls or None,
                observation=observation,
                timestamp=_now_iso(),
            )
        )
    if not steps:
        # ATIF requires >= 1 step; an empty session becomes a single
        # system step so the trajectory is structurally valid (and the
        # evidence gate / claim checks will fail it as infra_invalid).
        steps.append(
            Step(
                step_id=1,
                source="system",
                message="(no mappable events in session)",
                timestamp=_now_iso(),
            )
        )
    return steps


def convert_dsh_read_to_atif(response: DshReaderResponse) -> Trajectory:
    """Full conversion: official read → valid ATIF Trajectory."""
    interactions, ignorable_extras, issues = reduce_dsh_events(
        response.header, response.events
    )
    resolve_tool_call_relations(interactions, issues)

    steps = normalized_interactions_to_atif_steps(interactions)
    agent_name = str(response.header.get("agent") or response.header.get("agentName") or "dsh")
    agent_version = str(response.header.get("version") or "0.1.7-alpha.1")
    model_name = response.header.get("model") or response.header.get("modelName")

    extra: dict[str, Any] = {
        DSH_EXTRA_KEY: {
            "mapperVersion": MAPPER_VERSION,
            "header": response.header,
            "eventState": response.event_state,
            "inheritedEventCount": response.inherited_event_count,
            "conversionIssues": {
                "orphanToolResults": issues.orphan_tool_results,
                "duplicateCallIds": issues.duplicate_call_ids,
                "ambiguousRelations": issues.ambiguous_relations,
                "unmappedEvents": issues.unmapped_events,
            },
        }
    }
    if ignorable_extras:
        extra[DSH_IGNORABLE_EXTRA_KEY] = ignorable_extras

    trajectory = Trajectory(
        agent=Agent(
            name=agent_name,
            version=agent_version,
            model_name=model_name,
        ),
        steps=steps,
        extra=extra,
        session_id=str(response.header.get("sessionId") or response.request_id),
    )
    validate_canonical_transcript(trajectory)
    return trajectory


def validate_canonical_transcript(trajectory: Trajectory) -> None:
    """Every ATIF output must pass harbor's own validator."""
    validator = TrajectoryValidator()
    errors = validator.validate(trajectory.model_dump(mode="json", exclude_none=True))
    if validator.errors:
        raise DshAtifConversionError(
            "mapped trajectory failed ATIF validation: " + "; ".join(validator.errors)
        )


def derive_stop_reason(events: list[dict[str, Any]]) -> StopReason:
    """Stop reason derives ONLY from turn/end.reason.kind (plan §3).

    Both explicit nestings observed across DSH preview event shapes
    are accepted; anything else falls back to infra_error — never a
    guessed success.
    """
    for event in reversed(events):
        if event.get("type") != "turn":
            continue
        kind = None
        end = event.get("end")
        if isinstance(end, dict):
            kind = end.get("kind")
            reason = end.get("reason")
            if kind is None and isinstance(reason, dict):
                kind = reason.get("kind")
        reason = event.get("reason")
        if kind is None and isinstance(reason, dict):
            kind = reason.get("kind")
        if kind in STOP_REASON_MAP:
            return STOP_REASON_MAP[kind]  # type: ignore[return-value]
    return "infra_error"


def build_canonical_transcript(
    atif: Trajectory,
    evidence: EvidenceBundle | None,
    stop_reason: StopReason,
) -> CanonicalTranscript:
    """Assemble the CanonicalTranscript with completeness metadata."""
    completeness = CompletenessRecord(
        fields=[
            FieldCompleteness(field="events", status="ok"),
            FieldCompleteness(
                field="token_usage",
                status="partial",
                reason="ACP path restores usage best-effort from usage_update",
            )
            if evidence is None
            else FieldCompleteness(field="token_usage", status="ok"),
        ]
    )
    return CanonicalTranscript.build(
        atif=atif,
        stop_reason=stop_reason,
        evidence_uri=None,
        completeness=completeness,
    )
