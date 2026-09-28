"""Map the pinned DSH 0.1.7-alpha.1 Session V4 log, not CLI output.

ATIF is an append-origin transcript grouped by explicit (turn, step). Surface
replacement copies are model context, not new executions. The full immutable
log, current surface, replacement history and conversion issues live in
``extra.dsh``; no provider streams or unsupported content are discarded.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Agent, ObservationResult, Step, ToolCall, Trajectory
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics
from harbor.models.trajectories.observation import Observation
from harbor.utils.trajectory_validator import TrajectoryValidator

from aeval.agents.dsh.bridge import DshReaderResponse
from aeval.contracts import (
    DSH_EXTRA_KEY,
    DSH_PRESERVED_EVENT_EXTRA_KEY,
    CanonicalTranscript,
    CompletenessRecord,
    EvidenceBundle,
    FieldCompleteness,
    StopReason,
)

__all__ = [
    "MAPPER_VERSION", "DshAtifConversionError", "NormalizedInteraction",
    "NormalizedEvent", "ConversionIssues", "convert_dsh_read_to_atif",
    "reduce_dsh_events", "map_known_session_event", "resolve_tool_call_relations",
    "normalized_interactions_to_atif_steps", "preserve_unmapped_event",
    "build_canonical_transcript", "validate_canonical_transcript", "derive_stop_reason",
]

MAPPER_VERSION = "dsh-official-session-persistence@0.1.7-alpha.1"
DSH_VERSION = "0.1.7-alpha.1"
SURFACE_EVENT_TYPES = frozenset({
    "system/message", "developer/message", "user/message", "assistant/message", "tool/result",
})
# Event types this mapper interprets: the conversation surface, the lifecycle
# brackets that make a step settle, and the request/inheritance records.
MAPPED_EVENT_TYPES = SURFACE_EVENT_TYPES | frozenset({
    "turn/start", "turn/end", "step/start", "step/end", "tool/call",
    "request/header", "request/context", "session/end-seed", "assistant/attempt",
})
# ``KNOWN_SESSION_EVENT_TYPES`` of the pinned 0.1.7-alpha.1 build, dumped from
# @deepseek-ai/dsh-session/lib/types/known-event-types.js. Official readers accept
# these without an `ignorable` marker; the mapper only preserves them verbatim.
OFFICIAL_EVENT_TYPES = frozenset({
    "agent-preset/selected", "agent/inbox/spliced", "approval/asked", "approval/decided",
    "approval/policy", "assistant/attempt", "assistant/message", "command/done", "command/run",
    "compaction/end", "compaction/prune", "compaction/start", "compaction/summary",
    "deliverables/presented", "developer/message", "feedback/message-delete",
    "feedback/message-put", "feedback/record", "goal/change", "hook/invoked", "hook/result",
    "image/offload", "llm/retry", "llm/retry-started", "model/selection", "permission/preset",
    "plan/mode", "request/context", "request/header", "sandbox/mode", "schedule/change",
    "session-log-deepseek/delivery-accepted", "session/end-seed", "session/title",
    "session/title-llm-request", "step/end", "step/start", "subagent/catalog",
    "subagent/descriptor", "subagent/model-selection-policy", "system/message", "team/member",
    "team/message/delivered", "team/message/queued", "team/task", "todo/write",
    "tool-workflow/agent-end", "tool-workflow/agent-start", "tool-workflow/run-end",
    "tool-workflow/run-start", "tool/call", "tool/ptc-dispatch", "tool/ptc-dispatch-start",
    "tool/result", "turn/end", "turn/start", "user/message",
    "web/deepseek-search-llm-request", "workspace/changes",
})
# Official vocabulary members the mapper does not project into ATIF: retained as
# durable context, and enough on their own to make token accounting inexact.
LOG_ONLY_EVENT_TYPES = OFFICIAL_EVENT_TYPES - MAPPED_EVENT_TYPES
STOP_REASON_MAP: dict[str, StopReason] = {
    "completed": "agent_claimed_done", "max-tokens": "budget_exhausted",
}
MAX_SAFE_INTEGER = 2**53 - 1


class DshAtifConversionError(ValueError):
    """The official log cannot be converted without inventing semantics."""


@dataclass
class NormalizedEvent:
    index: int
    kind: str
    payload: dict[str, Any]  # the complete official envelope
    preserved: str | None = None  # "log-only" | "ignorable" | None when mapped


@dataclass
class NormalizedInteraction:
    source: str
    message_text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    raw_events: list[NormalizedEvent] = field(default_factory=list)
    turn: int | None = None
    step: int | None = None
    reasoning_text: str | None = None
    config: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.raw_events or self.tool_calls or self.tool_results)


@dataclass
class ConversionIssues:
    orphan_tool_results: list[str] = field(default_factory=list)
    duplicate_call_ids: list[str] = field(default_factory=list)
    ambiguous_relations: list[str] = field(default_factory=list)
    unmapped_events: list[str] = field(default_factory=list)
    malformed_arguments: list[str] = field(default_factory=list)
    invalid_usage: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return any(vars(self).values())

    def to_dict(self) -> dict[str, list[str]]:
        return dict(zip(
            ("orphanToolResults", "duplicateCallIds", "ambiguousRelations",
             "unmappedEvents", "malformedArguments", "invalidUsage"),
            vars(self).values(),
        ))


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise DshAtifConversionError(detail)


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _iso(epoch_ms: int) -> str:
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            milliseconds=epoch_ms
        )).isoformat(timespec="milliseconds")
    except (OverflowError, ValueError) as exc:
        raise DshAtifConversionError("timestamp outside representable ISO range") from exc


def _validate_header(header: dict[str, Any]) -> None:
    _require(isinstance(header, dict), "SessionHeader must be an object")
    _require(type(header.get("version")) is int and header["version"] == 4,
             "SessionHeader.version must be the pinned storage version 4")
    _require(_string(header.get("id")), "SessionHeader.id must be a nonempty string")
    _require(_count(header.get("createdAt")), "SessionHeader.createdAt must be epoch milliseconds")
    _iso(header["createdAt"])
    _require(type(header.get("isSeeded")) is bool, "SessionHeader.isSeeded must be boolean")
    for name in ("cwd", "parentSession", "agentPreset"):
        if name in header:
            _require(_string(header[name]), f"SessionHeader.{name} must be a nonempty string")
    if "origin" in header:
        _require(header["origin"] == "subagent", "invalid SessionHeader.origin")
    if "delegationDepth" in header:
        _require(_count(header["delegationDepth"]), "invalid SessionHeader.delegationDepth")


def _event_type(event: dict[str, Any]) -> tuple[str, str | None]:
    """Classify one event: mapped types yield ``None``, preserved ones a class.

    ``log-only`` members belong to the pinned official vocabulary but project no
    ATIF content; only a type outside that vocabulary is a newer-harness event,
    which the official read path refuses unless it declares itself ignorable.
    """
    _require(isinstance(event, dict), "SessionEvent must be an object")
    etype = event.get("type")
    _require(_string(etype), "SessionEvent.type must be a nonempty string")
    if "ignorable" in event:
        _require(event["ignorable"] is True, "SessionEvent.ignorable must be true or absent")
    if etype in MAPPED_EVENT_TYPES:
        return etype, None
    if etype in OFFICIAL_EVENT_TYPES:
        return etype, "log-only"
    _require(event.get("ignorable") is True, f"unknown required SessionEvent type {etype!r}")
    return etype, "ignorable"


def _validate_envelope(event: dict[str, Any]) -> tuple[str, str | None]:
    etype, preserved = _event_type(event)
    _require(_count(event.get("seq")), "SessionEvent.seq must be a nonnegative safe integer")
    _require(_count(event.get("time")), "SessionEvent.time must be epoch milliseconds")
    _iso(event["time"])
    _require("data" in event, "SessionEvent.data is required")
    try:
        json.dumps(event, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise DshAtifConversionError("SessionEvent must be lossless JSON") from exc
    if preserved is None:
        _require(isinstance(event["data"], dict), f"{etype}.data must be an object")
        _validate_data(event)
    return etype, preserved


def _validate_data(event: dict[str, Any]) -> None:
    kind, data = event["type"], event["data"]
    if kind.startswith(("turn/", "step/")) or kind in {
        "system/message", "developer/message", "assistant/message", "assistant/attempt",
        "tool/call", "tool/result",
    }:
        _require(_count(data.get("turn")), f"{kind}.data.turn is required")
        if not kind.startswith("turn/"):
            _require(_count(data.get("step")), f"{kind}.data.step is required")
    if kind == "turn/end":
        _require(isinstance(data.get("reason"), dict) and _string(data["reason"].get("kind")),
                 "turn/end.data.reason.kind is required")
    if kind in {"assistant/message", "assistant/attempt"}:
        _require(isinstance(data.get("stream"), list), f"{kind}.data.stream must be an array")
    if "interrupted" in data and kind == "assistant/message":
        _require(data["interrupted"] is True, "assistant/message.interrupted must be true or absent")
    if kind == "tool/call":
        for name in ("callId", "name"):
            _require(_string(data.get(name)), f"tool/call.data.{name} is required")
        _require(isinstance(data.get("arguments"), str), "tool/call arguments must be a JSON string")
    if kind in SURFACE_EVENT_TYPES:
        message = data if kind == "user/message" else data.get("message")
        _require(isinstance(message, dict), f"{kind} requires a message object")
        role = "tool" if kind == "tool/result" else kind.split("/")[0]
        _require(message.get("role") == role, f"{kind} requires role {role}")
        _require(_string(message.get("id")), f"{kind} requires message.id")
        source = message.get("source")
        _require(isinstance(source, dict) and _string(source.get("kind")),
                 f"{kind} requires message.source")
        if role == "assistant":
            _require(source["kind"] == "model" and _string(source.get("provider"))
                     and _string(source.get("model")), "assistant source must identify its model route")
        if role == "system":
            _require(source["kind"] == "system-prompt", "system source must be system-prompt")
        if role == "tool":
            _require(source["kind"] == "tool" and _string(source.get("callId"))
                     and _string(message.get("toolCallId")), "tool result requires explicit call IDs")
            if "isError" in message:
                _require(type(message["isError"]) is bool, "tool result isError must be boolean")
            _require("error" not in data or message.get("isError") is True,
                     "tool/result.error requires isError true")
        content = message.get("content")
        _require(isinstance(content, list), f"{kind} message.content must be a block array")
        for block in content:
            _require(isinstance(block, dict) and _string(block.get("type")), "invalid content block")
            if block["type"] in {"text", "reasoning"}:
                _require(isinstance(block.get("text"), str), "text/reasoning block requires text")
            if block["type"] == "tool-call":
                _require(_string(block.get("id")) and _string(block.get("name"))
                         and isinstance(block.get("arguments"), str), "invalid tool-call block")
            if block["type"] in {"tool-addition", "tool-removal"}:
                _require(role == "developer" and _string(block.get("toolName"))
                         and "tool" not in block, "invalid developer tool-change block")
    if kind == "request/header":
        header = data.get("header")
        _require(isinstance(header, dict), "request/header requires data.header")
        config = header.get("config")
        _require(isinstance(config, dict) and _string(config.get("provider"))
                 and _string(config.get("model")), "request/header requires config provider/model")
        if "reasoningEffort" in config:
            _require(_string(config["reasoningEffort"]), "reasoningEffort must be a string")
        _require(data.get("reason") in {"initial", "resume", "change", "series"},
                 "invalid request/header reason")
        _require("system" not in header, "request/header must omit system")
        _require(header.get("tools") != [], "request/header must omit empty tools")
        _require(header.get("adapterDefaults") != {}, "request/header must omit empty adapterDefaults")
        if "startsSeries" in data:
            _require(data["startsSeries"] is True, "startsSeries must be true or absent")
    if kind == "request/context":
        _require(_string(data.get("provider")) and _string(data.get("model")),
                 "request/context requires provider/model")
    if kind == "session/end-seed" and "inherited" in data:
        _require(data["inherited"] is True, "session/end-seed.inherited must be true or absent")


def _fold_surface(events: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[int] = []
    replacements: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        kind, preserved = _validate_envelope(event)
        _require(event["seq"] == index, f"noncontiguous seq: expected {index}, got {event['seq']}")
        # Epoch clock samples are not sequence numbers: clock adjustment is legal.
        if preserved:
            continue  # log-only and ignorable records never change the surface
        if kind not in SURFACE_EVENT_TYPES:
            _require("surfaceOp" not in event and "sourceEventSeqs" not in event,
                     f"{kind} is not surface-eligible")
            continue
        op = event.get("surfaceOp")
        sources = event.get("sourceEventSeqs", [])
        if "sourceEventSeqs" in event:
            _require(kind != "assistant/message", "assistant/message cannot cite sourceEventSeqs")
            _require(isinstance(sources, list) and bool(sources)
                     and all(_count(s) and s < index for s in sources),
                     "sourceEventSeqs must cite nonempty earlier event sequences")
            _require(len(set(sources)) == len(sources), "duplicate sourceEventSeqs")
        if kind == "developer/message":
            data = event["data"]
            additions = [b for b in data["message"]["content"] if b["type"] == "tool-addition"]
            _require(bool(additions) == ("headerSeq" in data),
                     "developer headerSeq required exactly when tool additions are present")
            if additions:
                seq = data["headerSeq"]
                _require(_count(seq) and seq < index and events[seq]["type"] == "request/header",
                         "developer headerSeq must reference an earlier request/header")
                tools = events[seq]["data"]["header"].get("tools", [])
                for block in additions:
                    matches = [t for t in tools if t.get("name") == block["toolName"]]
                    _require(len(matches) == 1 and isinstance(matches[0].get("description"), str)
                             and isinstance(matches[0].get("parameters"), dict),
                             "developer tool-addition must resolve exactly one tool schema")
        if op == "append":
            nodes.append(index)
            continue
        _require(isinstance(op, dict) and set(op) == {"op", "startSeq", "endSeq"}
                 and op.get("op") == "replace" and _count(op.get("startSeq"))
                 and _count(op.get("endSeq")), "missing or invalid surfaceOp")
        _require(op["startSeq"] in nodes and op["endSeq"] in nodes,
                 "surface replacement endpoints must be current nodes")
        start, end = nodes.index(op["startSeq"]), nodes.index(op["endSeq"])
        _require(start <= end, "surface replacement range is reversed")
        shadowed = nodes[start:end + 1]
        _require(set(shadowed) <= set(sources), "replacement must cite every shadowed surface node")
        if start == 0 and events[nodes[0]]["type"] == "system/message":
            _require(kind == "system/message" and len(shadowed) == 1,
                     "system head may only be replaced by one system/message")
        if kind == "tool/result":
            _require(len(shadowed) == 1 and events[shadowed[0]]["type"] == "tool/result",
                     "tool/result replacement must target exactly one tool/result")
            old, new = deepcopy(events[shadowed[0]]["data"]), deepcopy(event["data"])
            old["message"]["content"] = new["message"]["content"] = None
            _require(old == new, "tool/result replacement may change only content")
        nodes[start:end + 1] = [index]
        replacements.append({"seq": index, "start": op["startSeq"], "end": op["endSeq"],
                             "shadowedSeqs": shadowed})
    messages = []
    for seq in nodes:
        event = events[seq]
        message = event["data"] if event["type"] == "user/message" else event["data"]["message"]
        if event["type"] in {"system/message", "developer/message", "assistant/message"} and not message["content"]:
            continue
        messages.append({"seq": seq, "message": message})
    return {"nodes": nodes, "replacements": replacements, "messages": messages}


def preserve_unmapped_event(event_index: int, event: dict[str, Any], preserved: str) -> dict[str, Any]:
    return {"eventIndex": event_index, "type": event["type"], "class": preserved,
            "raw": deepcopy(event)}


def map_known_session_event(event: dict[str, Any]) -> NormalizedEvent:
    kind, preserved = _validate_envelope(event)
    return NormalizedEvent(event["seq"], preserved or kind, event, preserved)


def _render(content: list[dict[str, Any]], *, assistant: bool = False) -> tuple[str, str | None]:
    text, reasoning = [], []
    for block in content:
        kind = block["type"]
        if kind == "text":
            text.append(block["text"])
        elif assistant and kind == "reasoning":
            reasoning.append(block["text"])
        elif assistant and kind == "tool-call":
            continue  # tool/call is the execution record, never duplicate this request
        else:
            # Attachment handles are NOT filenames or URLs. Keep structured blocks
            # visibly as JSON, as well as verbatim in the raw log and surface.
            text.append(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
    return "".join(text), "".join(reasoning) if reasoning else None


def _usage_values(usage: Any) -> dict[str, int | None] | None:
    """Official token-meter semantics: cache input is disjoint, reasoning is output."""
    if not isinstance(usage, dict) or not all(_count(usage.get(k)) for k in ("inputTokens", "outputTokens")):
        return None
    for key in ("cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "totalTokens"):
        if key in usage and not _count(usage[key]):
            return None
    if usage.get("reasoningTokens", 0) > usage["outputTokens"]:
        return None
    known_prompt = usage["inputTokens"] + usage.get("cacheReadTokens", 0) + usage.get("cacheWriteTokens", 0)
    all_cache = "cacheReadTokens" in usage and "cacheWriteTokens" in usage
    total = usage.get("totalTokens")
    if total is not None:
        prompt = total - usage["outputTokens"]
        if prompt < known_prompt or (all_cache and prompt != known_prompt):
            return None
    elif all_cache:
        prompt = known_prompt
        total = prompt + usage["outputTokens"]
    else:
        prompt = None  # absent cache buckets are unknown, not zero
    if known_prompt > MAX_SAFE_INTEGER or (total is not None and total > MAX_SAFE_INTEGER):
        return None
    return {"prompt": prompt, "output": usage["outputTokens"], "total": total,
            "cached": usage.get("cacheReadTokens")}


def reduce_dsh_events(
    header: dict[str, Any], events: list[dict[str, Any]],
) -> tuple[list[NormalizedInteraction], list[dict[str, Any]], ConversionIssues]:
    _validate_header(header)
    _fold_surface(events)
    issues = ConversionIssues()
    interactions: list[NormalizedInteraction] = []
    groups: dict[tuple[int, int], NormalizedInteraction] = {}
    extras: list[dict[str, Any]] = []
    config = None
    for event in events:
        normalized = map_known_session_event(event)
        kind, data, index = normalized.kind, event["data"], event["seq"]
        if normalized.preserved:
            extras.append(preserve_unmapped_event(index, event, normalized.preserved))
            continue
        if kind == "request/header":
            config = data["header"]["config"]
            continue
        if kind in SURFACE_EVENT_TYPES and event["surfaceOp"] != "append":
            continue  # reconstruction retained separately; not another execution
        if kind in {"user/message", "system/message", "developer/message"}:
            message = data if kind == "user/message" else data["message"]
            text, _ = _render(message["content"])
            interactions.append(NormalizedInteraction(
                source="user" if kind == "user/message" else "system",
                message_text=text, raw_events=[normalized],
                turn=data.get("turn"), step=data.get("step"),
            ))
        elif kind in {"assistant/message", "tool/call", "tool/result"}:
            key = (data["turn"], data["step"])
            if key not in groups:
                groups[key] = NormalizedInteraction(source="agent", turn=key[0], step=key[1], config=config)
                interactions.append(groups[key])
            current = groups[key]
            current.raw_events.append(normalized)
            if kind == "assistant/message":
                if current.message_text is not None:
                    issues.unmapped_events.append(f"event[{index}]: multiple assistant messages for {key}")
                text, reasoning = _render(data["message"]["content"], assistant=True)
                current.message_text = (current.message_text or "") + text
                if reasoning is not None:
                    current.reasoning_text = (current.reasoning_text or "") + reasoning
                current.config = config
                if "usage" in data:
                    if _usage_values(data["usage"]) is None:
                        issues.invalid_usage.append(f"event[{index}]: invalid assistant usage")
                    else:
                        current.usage = data["usage"]
            elif kind == "tool/call":
                raw = data["arguments"]
                try:
                    arguments = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments are not an object")
                except (ValueError, TypeError):
                    arguments = None
                    issues.malformed_arguments.append(f"event[{index}]: invalid JSON-object arguments for {data['callId']!r}")
                current.tool_calls.append({**data, "arguments": arguments, "rawArguments": raw, "index": index})
            else:
                message = data["message"]
                current.tool_results.append({"callId": message["toolCallId"], "sourceCallId": message["source"]["callId"],
                                             "content": _render(message["content"])[0], "index": index,
                                             "raw": data})
    return interactions, extras, issues


def resolve_tool_call_relations(interactions: list[NormalizedInteraction], issues: ConversionIssues) -> None:
    """Only a unique, earlier call in the same turn AND step proves a relation."""
    for interaction in interactions:
        calls = Counter(c["callId"] for c in interaction.tool_calls)
        results = Counter(r["callId"] for r in interaction.tool_results)
        for call_id, count in calls.items():
            if count > 1:
                issues.duplicate_call_ids.append(f"duplicate tool/call callId {call_id!r} in ({interaction.turn}, {interaction.step})")
        for result in interaction.tool_results:
            call_id = result["callId"]
            matching = [c for c in interaction.tool_calls if c["callId"] == call_id and c["index"] < result["index"]]
            matched = (calls[call_id] == 1 and len(matching) == 1 and results[call_id] == 1
                       and result["sourceCallId"] == call_id)
            result["matched"] = matched
            if not matching:
                issues.orphan_tool_results.append(f"event[{result['index']}]: no earlier same-turn/step call {call_id!r}")
            elif not matched:
                issues.ambiguous_relations.append(f"event[{result['index']}]: ambiguous tool result {call_id!r}")


def normalized_interactions_to_atif_steps(interactions: list[NormalizedInteraction]) -> list[Step]:
    steps = []
    for interaction in interactions:
        calls = [ToolCall(
            tool_call_id=c["callId"], function_name=c["name"], arguments=c["arguments"] or {},
            extra={"dsh": {"seq": c["index"], "rawArguments": c["rawArguments"],
                           "argumentsDecoded": c["arguments"] is not None}},
        ) for c in interaction.tool_calls]
        results = [ObservationResult(
            source_call_id=r["callId"] if r.get("matched") else None,
            content=r["content"], extra={"dsh": {"seq": r["index"], "data": r["raw"],
                                                 "relation": "matched" if r.get("matched") else "unresolved"}},
        ) for r in interaction.tool_results]
        values = _usage_values(interaction.usage)
        config = interaction.config or {}
        steps.append(Step(
            step_id=len(steps) + 1, source=interaction.source,
            timestamp=_iso(interaction.raw_events[0].payload["time"]) if interaction.raw_events else None,
            message=interaction.message_text or "", reasoning_content=interaction.reasoning_text,
            model_name=config.get("model"), reasoning_effort=config.get("reasoningEffort"),
            tool_calls=calls or None, observation=Observation(results=results) if results else None,
            metrics=Metrics(prompt_tokens=values["prompt"], completion_tokens=values["output"],
                            cached_tokens=values["cached"], extra={"dsh": {"usage": interaction.usage}}) if values else None,
            extra={"dsh": {"eventSeqs": [e.index for e in interaction.raw_events],
                           "turn": interaction.turn, "step": interaction.step}},
        ))
    return steps


# Records the pinned build emits that are not event types at all but
# provably carry no model work: the session header has no seq and no data.
# Anything else that is neither a known official event nor known token-free
# fails closed, because it could hide a model call.
TOKEN_FREE_NON_EVENT_TYPES = frozenset({"session"})

# Event types that can represent model work whose usage this reducer cannot
# see. Every other known official type is lifecycle/surface bookkeeping
# (permission/preset, sandbox/mode, approval/policy, agent/inbox/spliced,
# session/title, tool/result, ...) and cannot have consumed tokens.
MODEL_WORK_EVENT_TYPES = frozenset({
    "assistant/attempt",
    "session/title-llm-request",
    "web/deepseek-search-llm-request",
    "llm/retry",
    "llm/retry-started",
    "compaction/start",
    "compaction/summary",
    "compaction/end",
})

# Gateway rejections the broker returns BEFORE dispatching to any provider:
# the request provably consumed zero tokens (no upstream request was made).
PRE_DISPATCH_REJECTION_CODES = frozenset({
    "AEVAL_LEASE_CLOSED",
    "AEVAL_LEASE_BUSY",
    "AEVAL_AUXILIARY_REFUSED",
    "AEVAL_BUDGET_EXHAUSTED",
})

# Written by the sandbox transport next to the bundle descriptor.
GATEWAY_REJECTION_LOG = "gateway_refusals.jsonl"

# An auxiliary model call the owner's policy does not allow the runtime to
# make on its own; the session records the request, not its outcome.
AUXILIARY_REQUEST_EVENT_TYPES = frozenset({"session/title-llm-request"})


def _blocks_usage_exactness(event: dict[str, Any]) -> bool:
    """True when a record can hide model work this reducer cannot see."""
    kind = event.get("type")
    if kind == "assistant/message":
        return False
    if kind in MODEL_WORK_EVENT_TYPES:
        return True
    return kind not in OFFICIAL_EVENT_TYPES and kind not in TOKEN_FREE_NON_EVENT_TYPES


def count_pre_dispatch_auxiliary_rejections(logs_dir: Path) -> int:
    """Auxiliary requests the gateway rejected before dispatch.

    The in-sandbox transport appends one JSON object per rejection to
    ``gateway_refusals.jsonl`` beside the bundle descriptor. Only rejections
    the broker returns before dispatching count, and only for a request that
    declared a purpose. Missing, oversized, or malformed evidence counts as
    zero: an unexplained auxiliary request stays unaccounted and the verdict
    stays ``partial`` instead of silently scoring an unknown run.
    """
    path = Path(logs_dir) / GATEWAY_REJECTION_LOG
    try:
        if not path.is_file() or path.stat().st_size > 1 << 20:
            return 0
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if (record.get("code") in PRE_DISPATCH_REJECTION_CODES
                and isinstance(record.get("purpose"), str) and record["purpose"]):
            count += 1
    return count


def _usage_summary(
    events: list[dict[str, Any]], inherited: int,
    zero_token_auxiliary_rejections: int = 0,
) -> dict[str, Any]:
    live = events[inherited:]
    samples = [e for e in live if e["type"] == "assistant/message" and e["surfaceOp"] == "append"]
    values = [_usage_values(e["data"].get("usage")) for e in samples]
    exact = bool(samples) and all(v is not None and v["total"] is not None for v in values)
    # Attempts and unfinished/missing lifecycle brackets can carry unaccounted work.
    starts = Counter((e["data"]["turn"], e["data"]["step"]) for e in live if e["type"] == "step/start")
    ends = Counter((e["data"]["turn"], e["data"]["step"]) for e in live if e["type"] == "step/end")
    settlements = Counter((e["data"]["turn"], e["data"]["step"]) for e in samples)
    exact = exact and starts == ends == settlements and all(n == 1 for n in settlements.values())
    # A record defeats exactness only if it can hide model work: known
    # official lifecycle events (and the session header) provably cannot,
    # while unknown/opaque types and real attempts still fail closed.
    blocking = [e for e in live if _blocks_usage_exactness(e)]
    auxiliary = [e for e in blocking if e["type"] in AUXILIARY_REQUEST_EVENT_TYPES]
    # An auxiliary request the broker rejected before dispatch is provably
    # zero-token, and the transport recorded that rejection with the request
    # purpose. Each record can cover at most one request.
    covered = min(len(auxiliary), max(0, int(zero_token_auxiliary_rejections)))
    exact = exact and not [e for e in blocking if e["type"] not in AUXILIARY_REQUEST_EVENT_TYPES] and len(auxiliary) == covered
    exact = exact and derive_stop_reason(events, inherited) in {"agent_claimed_done", "budget_exhausted"}
    valid = [v for v in values if v is not None]
    total = sum(v["total"] for v in valid if v["total"] is not None)
    return {"status": "ok" if exact and total <= MAX_SAFE_INTEGER else "partial",
            "scope": "live assistant/message usage only; attempts and opaque events are not inferred",
            "eventSeqs": [e["seq"] for e in samples],
            "totalTokens": total if exact and total <= MAX_SAFE_INTEGER else None,
            "reportedTotalTokensSubtotal": total if valid else None,
            "promptTokens": sum(v["prompt"] for v in valid) if valid and all(v["prompt"] is not None for v in valid) else None,
            "outputTokens": sum(v["output"] for v in valid) if valid else None,
            "cachedTokens": sum(v["cached"] for v in valid) if valid and all(v["cached"] is not None for v in valid) else None}


def convert_dsh_read_to_atif(
    response: DshReaderResponse, *, zero_token_auxiliary_rejections: int = 0,
) -> Trajectory:
    _validate_header(response.header)
    _require(isinstance(response.events, list), "events must be a complete log array")
    _require(response.event_state in {"detached", "shared-frozen"}, "invalid eventState")
    inherited = response.inherited_event_count
    _require(_count(inherited) and inherited <= len(response.events), "invalid inheritedEventCount")
    _require(response.header["isSeeded"] or inherited == 0, "unseeded session cannot inherit events")
    events = deepcopy(response.events)
    surface = _fold_surface(events)
    tagged = [e["seq"] for e in events if e["type"] == "session/end-seed" and e["data"].get("inherited") is True]
    if response.header["isSeeded"]:
        _require(bool(tagged) and tagged[-1] == inherited, "seeded session requires tagged marker at inheritedEventCount")
    else:
        _require(not tagged, "unseeded session cannot carry inherited marker")
    interactions, preserved, issues = reduce_dsh_events(response.header, events)
    resolve_tool_call_relations(interactions, issues)
    steps = normalized_interactions_to_atif_steps(interactions)
    if not steps:
        steps = [Step(step_id=1, source="system", message="", timestamp=_iso(response.header["createdAt"]),
                      extra={"dsh": {"synthetic": True, "reason": "no append-origin messages or calls"}})]
    for step in steps:
        seqs = step.extra["dsh"].get("eventSeqs", [])
        if seqs and all(seq < inherited for seq in seqs):
            step.is_copied_context = True
    observed = [{"seq": e["seq"], "reason": e["data"]["reason"],
                 "config": e["data"]["header"]["config"], "inherited": e["seq"] < inherited}
                for e in events if e["type"] == "request/header"]
    usage = _usage_summary(events, inherited, zero_token_auxiliary_rejections)
    extra: dict[str, Any] = {DSH_EXTRA_KEY: {
        "mapperVersion": MAPPER_VERSION, "header": deepcopy(response.header),
        "events": events, "eventState": response.event_state, "inheritedEventCount": inherited,
        "trajectoryView": "append-origin events grouped by turn/step; replacements only in surface",
        "surface": surface, "observedModels": observed, "usage": usage,
        "conversionIssues": issues.to_dict(),
    }}
    if preserved:
        extra[DSH_PRESERVED_EVENT_EXTRA_KEY] = preserved
    trajectory = Trajectory(
        agent=Agent(name="dsh", version=DSH_VERSION, model_name=observed[-1]["config"]["model"] if observed else None),
        session_id=response.header["id"], steps=steps, extra=extra,
        notes="Metrics summarize live reported usage only; copied context is excluded. See extra.dsh.usage for completeness.",
        final_metrics=FinalMetrics(total_prompt_tokens=usage["promptTokens"],
                                   total_completion_tokens=usage["outputTokens"], total_cached_tokens=usage["cachedTokens"],
                                   total_steps=len(steps), extra={"dsh": usage}),
    )
    validate_canonical_transcript(trajectory)
    return trajectory


def validate_canonical_transcript(trajectory: Trajectory) -> None:
    validator = TrajectoryValidator()
    validator.validate(trajectory.model_dump(mode="json", exclude_none=True))
    if validator.errors:
        raise DshAtifConversionError("mapped trajectory failed ATIF validation: " + "; ".join(validator.errors))


def derive_stop_reason(events: list[dict[str, Any]], inherited_event_count: int = 0) -> StopReason:
    """Only the latest live, matched turn/end can report completion.

    Restore/fork boundaries invalidate older lifecycle proof; forked/interrupted
    closers and any later unfinished turn fail closed. Exit codes are irrelevant.
    """
    if not _count(inherited_event_count) or inherited_event_count > len(events):
        return "infra_error"
    active = None
    result: StopReason = "infra_error"
    for event in events[inherited_event_count:]:
        kind, data = event.get("type"), event.get("data")
        if not isinstance(data, dict):
            return "infra_error"
        if kind == "session/end-seed":
            active, result = None, "infra_error"
        elif kind == "turn/start":
            active, result = data.get("turn"), "infra_error"
        elif kind == "turn/end":
            reason = data.get("reason")
            result = STOP_REASON_MAP.get(reason.get("kind"), "infra_error") if (
                _count(active) and active == data.get("turn") and isinstance(reason, dict)
            ) else "infra_error"
            active = None
        elif kind in {"step/start", "assistant/message", "tool/call", "assistant/attempt"} and active is None:
            result = "infra_error"
    return result if active is None else "infra_error"


def build_canonical_transcript(atif: Trajectory, evidence: EvidenceBundle | None, stop_reason: StopReason) -> CanonicalTranscript:
    """Evidence presence is not proof of complete token accounting."""
    dsh = (atif.extra or {}).get(DSH_EXTRA_KEY, {})
    status = dsh.get("usage", {}).get("status", "partial")
    completeness = CompletenessRecord(fields=[
        FieldCompleteness(field="events", status="ok"),
        FieldCompleteness(field="token_usage", status="ok" if status == "ok" else "partial",
                          reason=None if status == "ok" else "DSH live usage coverage or exact cache/total accounting is unknown"),
    ])
    return CanonicalTranscript.build(atif=atif, stop_reason=stop_reason, evidence_uri=None, completeness=completeness)
