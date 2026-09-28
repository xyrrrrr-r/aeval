"""Trajectory grading framework — the top-level design (§1).

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
    "SealedTranscriptError",
    "ToolEvent",
    "TrajectoryEvidence",
    "TrajectoryGrader",
    "load_sealed_transcript",
    "build_evidence",
]


class SealedTranscriptError(RuntimeError):
    """The sealed canonical transcript cannot be graded from.

    Raised for a missing artifact reference, a missing runtime base
    directory, path traversal, IO errors, digest mismatch, or a parse
    failure. Callers translate this into ``cannot_judge`` — never into
    a fabricated score.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        prompt = self.total_prompt_tokens
        completion = self.total_completion_tokens
        if prompt is None or completion is None:
            return None
        return prompt + completion


def load_sealed_transcript(record: TrialRecord) -> CanonicalTranscript:
    """Load and verify the trial's sealed canonical transcript.

    The artifact is resolved against the runtime-only ``artifact_base``
    the grading pipeline injects; its sha256 must match the sealed
    ``ArtifactRef`` or the evidence is rejected.
    """
    ref = record.artifacts.get("canonical_transcript")
    if ref is None:
        raise SealedTranscriptError(
            "record carries no 'canonical_transcript' artifact — trajectory "
            "evidence was never sealed"
        )
    if not record.artifact_base:
        raise SealedTranscriptError(
            "record carries no artifact_base; sealed artifact paths cannot "
            "be resolved (pipeline must inject the trial directory)"
        )
    rel = Path(ref.path)
    if rel.is_absolute() or ".." in rel.parts:
        raise SealedTranscriptError(
            f"sealed artifact path is not portable: {ref.path!r}"
        )
    path = Path(record.artifact_base) / rel
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SealedTranscriptError(
            f"sealed transcript unreadable at {rel}: {exc}"
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != ref.sha256:
        raise SealedTranscriptError(
            f"sealed transcript digest mismatch for {rel}: recorded "
            f"{ref.sha256[:16]}… but file hashes {digest[:16]}…"
        )
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

    return TrajectoryEvidence(
        transcript=transcript,
        stop_reason=stop_reason,
        total_steps=len(steps),
        agent_steps=sum(1 for s in steps if (s.source or "") == "agent"),
        tool_events=tuple(tool_events),
        total_prompt_tokens=prompt,
        total_completion_tokens=completion,
        total_cached_tokens=cached,
        extras=extras,
    )


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
