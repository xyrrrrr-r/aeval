"""The in-sandbox facade's per-call metering ledger, folded into trial evidence.

The deepAgent control stack's facade (``deepagents-eval-control``) proxies every
model call to the aeval broker under the trial's lease. With
``AEVAL_FACADE_CALL_LOG`` set it writes one JSONL line per call: the broker's
metered usage, the tool names the model asked for, and the call's wall-clock
window.

Why aeval reads it. The broker meters every call, but that accounting lives in
host-side lease state and never reaches the sealed bundle; the ACP server this
family drives emits no ``usage_update`` either. Trial evidence therefore had
``final_metrics.total_*_tokens = null``, ``step.timestamp = null`` and a
``mock_call_log`` that said "no calls observed" — honestly reported as
"unavailable", never as a fabricated 0. The facade is the one component that
sees BOTH the broker's metered usage and the response the agent was served, so
its ledger is the evidence of record for those fields.

Honest rules (each one refuses to overclaim):

* an undeclared or missing ledger changes NOTHING — the adapter's declared
  ``token_usage = partial`` stands and every field stays empty;
* a malformed line is skipped and reported, never guessed at;
* trial totals are written only when EVERY metered call reported usage: a
  partial sum would understate spend, which is worse than "unavailable";
* per-step usage and timestamps are attached only under a VERIFIED one-to-one
  mapping between metered calls and agent steps (equal counts AND equal
  tool-name sequences); otherwise every step keeps its nulls;
* cost is never invented — the wire carries no price, so ``total_cost_usd``
  stays null and a cost metric keeps skipping for lack of a price rather than
  reporting a made-up number.

Nothing here can fail a trial: the ledger is a downgrade-able evidence
improvement, so every failure path degrades to the pre-ledger behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics

from aeval.contracts import (
    CanonicalTranscript,
    CompletenessRecord,
    CompletenessStatus,
    FieldCompleteness,
)

__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "CallLedger",
    "LedgerError",
    "MeteredCall",
    "TokenTotals",
    "enrich_transcript_with_call_ledger",
    "parse_call_ledger",
]

#: The only ledger schema this reader knows; another version is unusable
#: evidence rather than a best-effort parse (a future format could mean
#: something different by the same keys).
LEDGER_SCHEMA_VERSION = 1

_MAX_SAFE_INTEGER = 2**53 - 1


class LedgerError(ValueError):
    """One ledger line that cannot be trusted as metering evidence."""


def _count(value: Any) -> bool:
    """A non-negative safe integer, and not a bool (``True`` is not a count)."""
    return type(value) is int and 0 <= value <= _MAX_SAFE_INTEGER


def _opt_count(value: Any) -> int | None:
    if value is None:
        return None
    if not _count(value):
        raise LedgerError(f"expected a non-negative integer count, got {value!r}")
    return value


def _timestamp(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{field_name} must be a non-empty ISO 8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"{field_name} is not an ISO 8601 timestamp: {value!r}") from exc


@dataclass(frozen=True)
class TokenTotals:
    """Billed token totals across the metered calls of one trial."""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class MeteredToolCall:
    """One tool call the model asked for, as the ledger recorded it.

    ``arguments`` is the parsed payload when it was representable; a call whose
    payload could not be parsed keeps ``raw_arguments`` instead, and is then
    NOT usable to pair a call with a trajectory step (the recorded step names
    the ACP tool kind, so the payload is the only reliable link).
    """

    name: str
    arguments: Mapping[str, Any] | None
    raw_arguments: str | None

    @property
    def verifiable(self) -> bool:
        return self.arguments is not None

    @classmethod
    def from_record(cls, record: Any) -> MeteredToolCall:
        if isinstance(record, str):
            # A name-only entry: usable for a family whose recorded step names
            # match the wire names, never usable as payload evidence.
            return cls(name=record, arguments=None, raw_arguments=None)
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise LedgerError("each tool_calls entry must be a name or {name, arguments}")
        arguments = record.get("arguments")
        raw = record.get("arguments_raw")
        if arguments is not None and not isinstance(arguments, dict):
            # A non-object payload (list, scalar) cannot be compared with a
            # recorded step's argument dict; keep the fact that it existed.
            return cls(name=record["name"], arguments=None, raw_arguments=json.dumps(arguments))
        if arguments is None and raw is not None and not isinstance(raw, str):
            raise LedgerError("arguments_raw must be a string or absent")
        return cls(name=record["name"], arguments=arguments, raw_arguments=raw)


@dataclass(frozen=True)
class MeteredCall:
    """One model call as the facade's ledger recorded it."""

    call_id: str
    protocol: str
    model: str
    provider: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    status: str
    error_code: str | None
    tool_calls: tuple[MeteredToolCall, ...]
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    raw: Mapping[str, Any] = field(repr=False)
    #: Wall time from the call's start to the model's first output delta.
    #: None means the ledger did not record it (an older ledger, or a call that
    #: produced no output) — never a fabricated zero.
    first_token_ms: int | None = None
    first_output_at: datetime | None = None

    @property
    def metered(self) -> bool:
        """Whether the broker reported usage for this call."""
        return self.prompt_tokens is not None and self.completion_tokens is not None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> MeteredCall:
        version = record.get("schema_version")
        if version != LEDGER_SCHEMA_VERSION:
            raise LedgerError(
                f"unknown ledger schema_version {version!r} "
                f"(this reader knows {LEDGER_SCHEMA_VERSION})"
            )
        usage = record.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise LedgerError("usage must be an object or absent")
        usage = usage or {}
        raw_tool_calls = record.get("tool_calls")
        if not isinstance(raw_tool_calls, list):
            raise LedgerError("tool_calls must be a list")
        tool_calls = tuple(MeteredToolCall.from_record(entry) for entry in raw_tool_calls)
        error_code = record.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            raise LedgerError("error_code must be a string or absent")
        return cls(
            call_id=str(record.get("call_id") or ""),
            protocol=str(record.get("protocol") or ""),
            model=str(record.get("model") or ""),
            provider=str(record.get("provider") or ""),
            started_at=_timestamp(record.get("started_at"), field_name="started_at"),
            ended_at=_timestamp(record.get("ended_at"), field_name="ended_at"),
            duration_ms=_opt_count(record.get("duration_ms")) or 0,
            status=str(record.get("status") or ""),
            error_code=error_code,
            tool_calls=tool_calls,
            prompt_tokens=_opt_count(usage.get("prompt_tokens")),
            completion_tokens=_opt_count(usage.get("completion_tokens")),
            cached_tokens=_opt_count(usage.get("cached_tokens")),
            first_token_ms=_opt_count(record.get("first_token_ms")),
            first_output_at=(
                _timestamp(record.get("first_output_at"), field_name="first_output_at")
                if record.get("first_output_at") is not None
                else None
            ),
            raw=dict(record),
        )


@dataclass(frozen=True)
class CallLedger:
    """Every readable metered call of one trial, in the order they happened."""

    #: Where the ledger was read from (the adapter-declared sandbox path).
    source: str
    calls: tuple[MeteredCall, ...] = ()
    #: Lines that could not be trusted, with the line number and the reason.
    issues: tuple[str, ...] = ()
    #: True when the declared ledger was not there at all.
    missing: bool = False

    @property
    def usable(self) -> bool:
        """Whether this ledger may be cited as evidence at all."""
        return bool(self.calls)

    @property
    def ok_calls(self) -> tuple[MeteredCall, ...]:
        """Calls that completed — refused/aborted ones carry no usable usage."""
        return tuple(call for call in self.calls if call.status == "ok")

    def totals(self) -> TokenTotals | None:
        """Billed totals, or None when any metered call left usage unreported."""
        ok = self.ok_calls
        if not ok or any(not call.metered for call in ok):
            return None
        cached = (
            sum(call.cached_tokens or 0 for call in ok)
            if all(call.cached_tokens is not None for call in ok)
            else None
        )
        return TokenTotals(
            prompt_tokens=sum(call.prompt_tokens or 0 for call in ok),
            completion_tokens=sum(call.completion_tokens or 0 for call in ok),
            cached_tokens=cached,
        )

    def unmetered_ok_calls(self) -> tuple[MeteredCall, ...]:
        return tuple(call for call in self.ok_calls if not call.metered)

    def mock_call_records(self) -> list[dict[str, Any]]:
        """The ``mock_call_log`` lines: the ledger's own records, verbatim.

        The facade's records are already the evidence (usage, timing, tools),
        so they are passed through unchanged — a future reader must be able to
        re-derive every trial-level number from them. Parse issues ride along
        as an explicit event line rather than being silently dropped.
        """
        records = [dict(call.raw) for call in self.calls]
        if self.issues:
            records.append({
                "event": "ledger_lines_unreadable",
                "reason": (
                    "these ledger lines could not be trusted as metering "
                    "evidence, so they are excluded from every total"
                ),
                "issues": list(self.issues),
            })
        return records


def parse_call_ledger(text: str, *, source: str) -> CallLedger:
    """Parse ledger JSONL; unreadable lines are reported, never guessed at."""
    calls: list[MeteredCall] = []
    issues: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError as exc:
            issues.append(f"line {number}: not valid JSON ({exc})")
            continue
        if not isinstance(parsed, dict):
            issues.append(f"line {number}: expected a JSON object")
            continue
        try:
            calls.append(MeteredCall.from_record(parsed))
        except LedgerError as exc:
            issues.append(f"line {number}: {exc}")
    return CallLedger(source=source, calls=tuple(calls), issues=tuple(issues))


def missing_call_ledger(source: str) -> CallLedger:
    """The declared-but-absent ledger: no evidence, and it says so."""
    return CallLedger(source=source, missing=True)


# --- folding the ledger into the canonical transcript -----------------------


def _step_tool_calls(step: Any) -> tuple[tuple[str, Any], ...]:
    calls = getattr(step, "tool_calls", None) or []
    return tuple(
        (
            str(getattr(call, "function_name", "") or ""),
            getattr(call, "arguments", None),
        )
        for call in calls
    )


def _pair_is_verified(
    step_tools: Sequence[tuple[str, Any]], call: MeteredCall
) -> tuple[bool, str]:
    """Whether one metered call produced one recorded step.

    The link is the PAYLOAD, not the name: this family's trajectory records the
    ACP tool KIND (``edit``) while the model asked for the wire tool
    (``write_file``), so name equality would refuse a correct pairing. When
    either side's payload is unavailable the fallback is name equality —
    strictly weaker, and it fails rather than guesses when it cannot hold.
    """
    if len(step_tools) != len(call.tool_calls):
        return False, (
            f"{len(call.tool_calls)} requested tool call(s) vs "
            f"{len(step_tools)} recorded"
        )
    for (step_name, step_arguments), tool in zip(step_tools, call.tool_calls):
        if tool.verifiable and isinstance(step_arguments, dict):
            if tool.arguments != step_arguments:
                return False, (
                    f"tool call {tool.name!r} carried different arguments than "
                    f"the recorded step {step_name!r}"
                )
            continue
        if tool.name != step_name:
            return False, (
                f"wire tool {tool.name!r} does not match recorded tool "
                f"{step_name!r} and the payload could not verify it"
            )
    return True, ""


def _verify_mapping(
    steps: Sequence[Any], agent_indexes: Sequence[int], calls: Sequence[MeteredCall]
) -> tuple[bool, str]:
    """Whether metered calls and agent steps pair up one-to-one.

    The rule is deliberately strict: equal counts AND every pair verified by its
    tool payload (or, when no payload exists, by name). A looser rule (assign
    in order and hope) would attach one call's timing to another call's step,
    which is worse than the honest null this reader leaves behind.
    """
    if not calls:
        return False, "the ledger carried no completed model call"
    if len(calls) != len(agent_indexes):
        return False, (
            f"{len(calls)} metered call(s) vs {len(agent_indexes)} agent step(s) "
            "— a one-to-one mapping is not verifiable"
        )
    for position, index in enumerate(agent_indexes):
        ok, reason = _pair_is_verified(_step_tool_calls(steps[index]), calls[position])
        if not ok:
            return False, f"step {getattr(steps[index], 'step_id', index)}: {reason}"
    return True, ""


def _token_field_status(
    completeness: CompletenessRecord | None,
    status: CompletenessStatus,
    reason: str | None,
) -> CompletenessRecord:
    """The completeness record with ``token_usage`` restated for this trial."""
    fields = list(completeness.fields) if completeness is not None else []
    restated = [f for f in fields if f.field != "token_usage"]
    restated.append(FieldCompleteness(field="token_usage", status=status, reason=reason))
    return CompletenessRecord(fields=restated)


def enrich_transcript_with_call_ledger(
    transcript: CanonicalTranscript, ledger: CallLedger
) -> CanonicalTranscript:
    """Fold a call ledger into a transcript; identity when it adds nothing.

    Returns a COPY: the cached transcript the adapter hands out is never
    mutated, so a second read cannot observe a half-enriched object.
    """
    if not ledger.usable:
        if ledger.missing or not ledger.issues:
            return transcript
        # A ledger that exists but yields nothing readable is a real (if
        # disappointing) fact about the trial: record the degradation instead
        # of pretending the field was never there to begin with.
        return transcript.model_copy(update={
            "completeness": _token_field_status(
                transcript.completeness,
                "partial",
                "the facade call ledger "
                f"({ledger.source}) carried no readable metered call: "
                + "; ".join(ledger.issues[:3]),
            ),
        })

    atif = transcript.atif.model_copy(deep=True)
    ok_calls = ledger.ok_calls
    steps = list(atif.steps)
    agent_indexes = [
        index
        for index, step in enumerate(steps)
        if getattr(step, "source", None) == "agent"
    ]
    mapping_ok, mapping_reason = _verify_mapping(steps, agent_indexes, ok_calls)

    notes: list[str] = []
    if mapping_ok:
        first_token_calls = 0
        for position, index in enumerate(agent_indexes):
            call = ok_calls[position]
            steps[index].timestamp = call.started_at.isoformat(timespec="milliseconds")
            metrics = steps[index].metrics or Metrics()
            if call.metered:
                metrics = Metrics(
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                    cached_tokens=call.cached_tokens,
                    extra=metrics.extra,
                )
            if call.first_token_ms is not None:
                # Latency lives in Metrics.extra: the trajectory schema forbids
                # unknown top-level fields, and `extra` is its documented slot
                # for measurement the schema does not model.
                extra = dict(metrics.extra or {})
                extra["first_token_ms"] = call.first_token_ms
                if call.first_output_at is not None:
                    extra["first_output_at"] = call.first_output_at.isoformat(
                        timespec="milliseconds"
                    )
                metrics = metrics.model_copy(update={"extra": extra})
                first_token_calls += 1
            if call.metered or metrics.extra:
                steps[index].metrics = metrics
        # The first step is the instruction that started the session; the
        # first metered call's start is the closest observable proxy for when
        # it was submitted (nothing else in this harness timestamps it).
        if steps and getattr(steps[0], "source", None) == "user":
            steps[0].timestamp = ok_calls[0].started_at.isoformat(timespec="milliseconds")
        notes.append(
            f"Step timestamps and per-step usage come from the in-sandbox "
            f"facade call ledger ({ledger.source}); the {len(ok_calls)} metered "
            "call(s) and the agent steps were paired one-to-one by tool "
            "payload (the trajectory records the ACP tool kind, which can "
            "differ from the wire tool name). The first step's timestamp is "
            "the first metered call's start."
        )
        if first_token_calls:
            notes.append(
                "First-token latency was recorded by the facade for "
                f"{first_token_calls} of {len(ok_calls)} call(s): wall time "
                "from the call's start to the model's first output delta "
                "(text, reasoning or tool-argument delta), sealed in the "
                "step's metrics.extra.first_token_ms. Calls the ledger did "
                "not time carry no such field rather than a zero."
            )
    else:
        notes.append(
            f"Step timestamps were not attached from the facade call ledger "
            f"({ledger.source}): {mapping_reason}. Calls: "
            + ", ".join(
                f"{[tool.name for tool in call.tool_calls] or ['text']}"
                for call in ok_calls[:4]
            )
        )

    totals = ledger.totals()
    if totals is not None:
        final_metrics = atif.final_metrics or FinalMetrics()
        final_metrics.total_prompt_tokens = totals.prompt_tokens
        final_metrics.total_completion_tokens = totals.completion_tokens
        if totals.cached_tokens is not None:
            final_metrics.total_cached_tokens = totals.cached_tokens
        atif.final_metrics = final_metrics
        notes.append(
            f"Token totals are the broker's metered usage over "
            f"{len(ok_calls)} call(s), sealed from the facade call ledger: "
            f"{totals.prompt_tokens} prompt + {totals.completion_tokens} "
            f"completion tokens. Cost is not derivable from this evidence "
            "(the wire carries no price), so total_cost_usd stays null."
        )
        token_status: CompletenessStatus = "ok"
        token_reason = (
            "usage is the broker's own per-call accounting, relayed by the "
            "in-sandbox facade to the ledger this trial sealed"
        )
    else:
        unmetered = ledger.unmetered_ok_calls()
        token_status = "partial"
        token_reason = (
            f"{len(unmetered)} of {len(ok_calls)} metered call(s) reported no "
            "usage, so no trial total is stated"
            if unmetered
            else "the facade call ledger carried no completed model call"
        )
        notes.append(
            f"Token totals were not stated: {token_reason}."
        )

    atif.steps = steps
    existing = (atif.notes or "").strip()
    atif.notes = "\n".join([part for part in (existing, *notes) if part])
    return transcript.model_copy(update={
        "atif": atif,
        "completeness": _token_field_status(
            transcript.completeness, token_status, token_reason
        ),
    })
