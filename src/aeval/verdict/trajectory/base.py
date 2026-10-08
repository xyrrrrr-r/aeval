"""Trajectory grading framework.

A trajectory grader judges *how* the agent ran, not just *what* it
produced. This module owns the shared skeleton so concrete graders
(hello, terminal-bench, …) only declare metrics:

- **Sealed evidence only.** The transcript is read from the trial's
  sealed ``canonical_transcript`` artifact and its sha256 is verified
  against the recorded ``ArtifactRef`` before parsing. A missing,
  unreadable, corrupt, or digest-mismatched artifact is never graded
  from — the grader returns ``cannot_judge`` with reasons.
- **Metric evaluation.** Each metric is a small pure object over a
  :class:`TrajectoryEvidence` view. A metric that cannot judge from
  this evidence must skip itself with a reason — fabrication is the
  one forbidden move.
- **Verdict semantics** (see :mod:`aeval.verdict.trajectory.aggregate`):
  ``integrity`` violations fail the trial; ``efficiency`` /
  ``robustness`` / ``governance`` outcomes only move the score.
  A trajectory grader never returns ``fail`` because the agent was
  merely slow or wasteful.

The class satisfies the aeval ``Grader`` protocol (``id``, ``version``,
``layer``, ``async grade``); suite grader modules are thin re-exports
around a :func:`build_grader` preset (see
:mod:`aeval.verdict.trajectory.presets`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from aeval.contracts import CanonicalTranscript, TrialRecord

__all__ = [
    "SealedArtifactError",
    "SealedTranscriptError",
    "ToolEvent",
    "TrajectoryEvidence",
    "TrajectoryGrader",
    "TrajectoryMessage",
    "load_sealed_anchors",
    "load_sealed_transcript",
    "build_evidence",
    "agent_replies_from_steps",
    "user_messages_from_steps",
]


class SealedArtifactError(RuntimeError):
    """A sealed artifact cannot be graded from.

    Raised for a missing artifact reference, a missing runtime base
    directory, path traversal, IO errors, digest mismatch, or a parse
    failure. Callers translate this into ``cannot_judge`` — never into
    a fabricated score.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SealedTranscriptError(SealedArtifactError):
    """The sealed canonical transcript cannot be graded from.

    Kept as its own name for the transcript's established callers; the
    shared failure mode (and the ``reason`` attribute) lives on the
    sealed-artifact base.
    """


@dataclass(frozen=True)
class ToolEvent:
    """One tool call paired with its observation, if any."""

    step_id: int
    call_id: str
    function_name: str
    arguments: dict[str, Any]
    observation_text: str
    observation_present: bool


@dataclass(frozen=True)
class TrajectoryMessage:
    """One conversation-surface message, positioned in the trajectory."""

    step_id: int
    source: str          # "agent" | "user" | "system" | "developer"
    text: str
    turn: int | None = None
    # 该步是否 fork 复制上下文（记忆基底）——系统面消息携带它以
    # 区分「随父会话带入的提示」与「本会话自己的提示」；user/agent
    # 面不使用（其 copied 语义挂在 Turn 上）。
    copied: bool = False


@dataclass(frozen=True)
class TrajectoryEvidence:
    """Precomputed, read-only views over one sealed trajectory."""

    transcript: CanonicalTranscript
    stop_reason: str
    total_steps: int
    agent_steps: int
    tool_events: tuple[ToolEvent, ...] = ()
    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    # --- conversation-surface and timing views ---
    # Additive with defaults: the base metrics read ``transcript`` directly,
    # so existing graders keep working unchanged.
    agent_messages: tuple[TrajectoryMessage, ...] = ()
    user_message_texts: tuple[str, ...] = ()
    step_timestamps: tuple[str | None, ...] = ()
    turn_count: int | None = None
    wall_clock_seconds: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        prompt = self.total_prompt_tokens
        completion = self.total_completion_tokens
        if prompt is None or completion is None:
            return None
        return prompt + completion


def _load_sealed_artifact(record: TrialRecord, name: str) -> bytes:
    """Read one sealed artifact's bytes, verifying its recorded sha256.

    The shared discipline behind every sealed-evidence loader: resolve
    against the runtime-only ``artifact_base`` the grading pipeline
    injects, refuse non-portable paths, and reject any digest mismatch.
    """
    ref = record.artifacts.get(name)
    if ref is None:
        raise SealedArtifactError(
            f"record carries no {name!r} artifact — that evidence was "
            "never sealed"
        )
    if not record.artifact_base:
        raise SealedArtifactError(
            "record carries no artifact_base; sealed artifact paths cannot "
            "be resolved (pipeline must inject the trial directory)"
        )
    rel = Path(ref.path)
    if rel.is_absolute() or ".." in rel.parts:
        raise SealedArtifactError(
            f"sealed artifact path is not portable: {ref.path!r}"
        )
    path = Path(record.artifact_base) / rel
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SealedArtifactError(
            f"sealed artifact {name!r} unreadable at {rel}: {exc}"
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != ref.sha256:
        raise SealedArtifactError(
            f"sealed artifact {name!r} digest mismatch for {rel}: recorded "
            f"{ref.sha256[:16]}… but file hashes {digest[:16]}…"
        )
    return content


def load_sealed_transcript(record: TrialRecord) -> CanonicalTranscript:
    """Load and verify the trial's sealed canonical transcript.

    The artifact is resolved against the runtime-only ``artifact_base``
    the grading pipeline injects; its sha256 must match the sealed
    ``ArtifactRef`` or the evidence is rejected.
    """
    try:
        content = _load_sealed_artifact(record, "canonical_transcript")
    except SealedArtifactError as exc:
        raise SealedTranscriptError(exc.reason) from exc
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedTranscriptError(
            f"sealed transcript is not valid JSON: {exc}"
        ) from exc
    try:
        # The sealed file is the collector's exact serialization:
        # ``CanonicalTranscript.model_dump_json()`` (envelope shape with
        # the ATIF trajectory under the ``atif`` key). The flat
        # ``from_json_dict`` ATIF-document form is a different
        # serialization (used when the transcript is embedded in an
        # ATIF ``extra`` namespace) and does NOT describe this file.
        return CanonicalTranscript.model_validate(data)
    except Exception as exc:  # schema drift is an evidence problem
        raise SealedTranscriptError(
            f"sealed transcript does not match the canonical schema: {exc}"
        ) from exc


def load_sealed_anchors(record: TrialRecord) -> dict[str, Any]:
    """Load and verify the trial's sealed rubric anchors.

    Returns the parsed ``rubric/task_anchors.json`` mapping (suites key
    it by task_id); raises :class:`SealedArtifactError` when the anchors
    were never sealed, cannot be read, fail their digest check, or are
    not a JSON object — the caller translates that into
    ``cannot_judge``, never into a guessed rubric.
    """
    content = _load_sealed_artifact(record, "task_anchors")
    try:
        data = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedArtifactError(
            f"sealed task anchors are not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SealedArtifactError(
            "sealed task anchors are not a JSON object — the per-task "
            "rubric table has no usable shape"
        )
    return data


def _observation_text(observation: Any, call_id: str) -> tuple[str, bool]:
    """Join the observation results that answer ``call_id``."""
    if observation is None:
        return "", False
    results = getattr(observation, "results", None) or []
    parts = [
        result.content
        for result in results
        if getattr(result, "source_call_id", None) == call_id
        and getattr(result, "content", None)
    ]
    return "\n".join(str(p) for p in parts), bool(parts)


def build_evidence(
    transcript: CanonicalTranscript, stop_reason: str
) -> TrajectoryEvidence:
    """Project a canonical transcript into metric-ready views."""
    steps = list(transcript.atif.steps or [])
    tool_events: list[ToolEvent] = []
    for step in steps:
        for call in step.tool_calls or []:
            text, present = _observation_text(step.observation, call.tool_call_id)
            arguments = call.arguments if isinstance(call.arguments, dict) else {}
            tool_events.append(
                ToolEvent(
                    step_id=step.step_id,
                    call_id=call.tool_call_id,
                    function_name=call.function_name,
                    arguments=arguments,
                    observation_text=text,
                    observation_present=present,
                )
            )

    prompt = completion = cached = None
    final = getattr(transcript.atif, "final_metrics", None)
    if final is not None:
        prompt = final.total_prompt_tokens
        completion = final.total_completion_tokens
        cached = final.total_cached_tokens
    if prompt is None or completion is None:
        # Fallback: sum per-step metrics when the envelope was not
        # populated. Still honest — every value comes from the sealed
        # transcript.
        p = c = k = 0
        seen = False
        for step in steps:
            m = step.metrics
            if m is None:
                continue
            seen = True
            p += m.prompt_tokens or 0
            c += m.completion_tokens or 0
            k += m.cached_tokens or 0
        if seen:
            prompt = p if prompt is None else prompt
            completion = c if completion is None else completion
            cached = k

    extras: dict[str, Any] = {}
    atif_extra = getattr(transcript.atif, "extra", None)
    if isinstance(atif_extra, dict):
        extras = dict(atif_extra)

    # --- conversation-surface and timing views ---
    agent_messages = agent_replies_from_steps(steps)
    user_message_texts = user_message_texts_from_steps(steps)
    step_timestamps = tuple(
        getattr(step, "timestamp", None) for step in steps
    )
    turn_count = _turn_count(steps)
    wall_clock_seconds = _wall_clock_seconds(step_timestamps)

    return TrajectoryEvidence(
        transcript=transcript,
        stop_reason=stop_reason,
        total_steps=len(steps),
        agent_steps=sum(1 for s in steps if (s.source or "") == "agent"),
        tool_events=tuple(tool_events),
        total_prompt_tokens=prompt,
        total_completion_tokens=completion,
        total_cached_tokens=cached,
        agent_messages=agent_messages,
        user_message_texts=user_message_texts,
        step_timestamps=step_timestamps,
        turn_count=turn_count,
        wall_clock_seconds=wall_clock_seconds,
        extras=extras,
    )


def _turn_marker_of(step: Any) -> int | None:
    """The step's turn marker, adapter-anonymously.

    Adapters stash session facts in ``step.extra`` under their own key;
    the marker we need is the behavior — "an integer ``turn`` field in
    some ``extra`` sub-object" — so we scan for the shape instead of
    naming any concrete adapter (core code must stay agent-neutral).
    """
    extra = getattr(step, "extra", None)
    if not isinstance(extra, dict):
        return None
    for value in extra.values():
        if isinstance(value, dict):
            turn = value.get("turn")
            if isinstance(turn, int):
                return turn
    return None


def agent_replies_from_steps(
    steps: Sequence[Any],
) -> tuple[TrajectoryMessage, ...]:
    """Agent-surface messages, in step order, empty messages dropped.

    The text is kept verbatim (not stripped) — length-sensitive metrics
    must see what the adapter actually emitted.
    """
    out: list[TrajectoryMessage] = []
    for step in steps:
        if (step.source or "") != "agent":
            continue
        text = step.message or ""
        if not text.strip():
            continue
        out.append(
            TrajectoryMessage(
                step_id=step.step_id,
                source=step.source,
                text=text,
                turn=_turn_marker_of(step),
            )
        )
    return tuple(out)


def user_messages_from_steps(
    steps: Sequence[Any],
) -> tuple[TrajectoryMessage, ...]:
    """User-surface messages, in step order, empty ones dropped.

    Positioned (``step_id``-carrying) so probe-then-reply matching can
    find the first agent answer after each user turn.
    """
    out: list[TrajectoryMessage] = []
    for step in steps:
        if (step.source or "") != "user":
            continue
        text = step.message or ""
        if not text.strip():
            continue
        out.append(
            TrajectoryMessage(
                step_id=step.step_id,
                source=step.source,
                text=text,
                turn=_turn_marker_of(step),
            )
        )
    return tuple(out)


def user_message_texts_from_steps(steps: Sequence[Any]) -> tuple[str, ...]:
    """User-surface message texts, in step order, empty ones dropped."""
    return tuple(message.text for message in user_messages_from_steps(steps))


def _turn_count(steps: Sequence[Any]) -> int | None:
    """Distinct turn markers observed, or None when none were recorded.

    Counts the distinct marker values (sessions may interleave steps of
    the same turn); None is honest — the transcript simply carries no
    turn metadata, and turn-based metrics must skip rather than guess.
    """
    turns = {
        marker for marker in (_turn_marker_of(step) for step in steps)
        if marker is not None
    }
    return len(turns) if turns else None


def _wall_clock_seconds(
    step_timestamps: Sequence[str | None],
) -> float | None:
    """First-to-last-step wall time, or None when not computable.

    Requires BOTH endpoints as parseable ISO timestamps: a span measured
    from one endpoint only would be a fabrication. A negative span is
    rejected the same way (clocks ran backwards — the data is not
    trustworthy enough to score).
    """
    if not step_timestamps:
        return None
    from datetime import datetime

    def _parse(value: str | None) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    first = _parse(step_timestamps[0])
    last = _parse(step_timestamps[-1])
    if first is None or last is None:
        return None
    delta = (last - first).total_seconds()
    return delta if delta >= 0 else None


class TrajectoryGrader:
    """Base class for trajectory-layer graders.

    Subclasses (or presets) supply an ordered metric list; the grading
    flow — seal-verify, evaluate, fold — lives here exactly once.
    """

    layer: Literal["outcome", "trajectory", "both"] = "trajectory"

    def __init__(
        self,
        grader_id: str,
        grader_version: str,
        metrics: Sequence[Any],
        veto: bool = False,
    ) -> None:
        self.id = grader_id
        self.version = grader_version
        self.veto = veto
        self._metrics = list(metrics)

    @property
    def metrics(self) -> tuple[Any, ...]:
        """The declared metric objects (read-only view).

        轨迹分析（turn 切面）复用这同一批对象做逐条归因——判分器与
        分析面板之间不存在第二套指标构建。
        """
        return tuple(self._metrics)

    async def grade(self, record: TrialRecord):  # -> GradeResult
        from aeval.contracts import GradeResult, Score
        from aeval.verdict.trajectory.aggregate import fold_outcomes

        try:
            transcript = load_sealed_transcript(record)
        except SealedTranscriptError as exc:
            reason = f"sealed trajectory evidence unusable: {exc.reason}"
            return GradeResult(
                grader_id=self.id,
                grader_version=self.version,
                layer=self.layer,
                veto=self.veto,
                score=Score(value=None, valid=False, invalid_reasons=[reason]),
                status="cannot_judge",
                reasons=[reason],
                metrics=None,
            )
        evidence = build_evidence(transcript, record.stop_reason)
        outcomes = [metric.evaluate(evidence) for metric in self._metrics]
        return fold_outcomes(
            outcomes,
            grader_id=self.id,
            grader_version=self.version,
            veto=self.veto,
        )
