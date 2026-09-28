"""Built-in trajectory metrics — the common agent-runtime rubric (§2).

Categories fix severity (see ``MetricOutcome``):

- ``efficiency``  — resource headroom vs the suite-declared budget;
- ``robustness``  — error rate, loops, recovery, redundant work;
- ``governance``  — did the agent finish within the granted budget;
- ``integrity``   — explicit rule breaches (verdict-affecting).

Design rules every metric obeys:

1. **No fabrication.** A metric that cannot judge from the evidence
   returns ``skipped`` with a reason — it never invents a score.
2. **Deterministic.** Metrics are pure functions of the sealed
   trajectory; the same evidence always yields the same outcome.
3. **Conservative heuristics.** Error/scope detection uses explicit
   patterns; suites can extend them, never silently weaken them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from aeval.contracts import MetricOutcome
from aeval.verdict.trajectory.base import ToolEvent, TrajectoryEvidence

__all__ = [
    "TrajectoryMetric",
    "StepEfficiency",
    "TokenEfficiency",
    "ToolErrorRate",
    "LoopDetection",
    "RecoveryAbility",
    "RedundantActions",
    "BudgetAdherence",
    "ForbiddenAccess",
    "ScopeDiscipline",
    "looks_like_error",
    "call_key",
]


def _clamp01(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


# --- shared heuristics -------------------------------------------------


DEFAULT_ERROR_PATTERNS: tuple[str, ...] = (
    r"(?m)^Error:",                      # DSH tool failures surface as 'Error: …'
    r"Traceback \(most recent call last\)",
    r"command not found",
    r"Permission denied",
    r"exit code: [1-9]",
)

_ERROR_RE = [re.compile(p) for p in DEFAULT_ERROR_PATTERNS]


def looks_like_error(text: str, extra_patterns: tuple[str, ...] = ()) -> bool:
    """Heuristic failure detection over observation text.

    Conservative by design: plain outputs like ``No such file or
    directory`` from a *probe* command are not counted — only strong
    failure signatures. Suites may add patterns.
    """
    if not text:
        return False
    for regex in _ERROR_RE:
        if regex.search(text):
            return True
    for pattern in extra_patterns:
        if pattern and re.search(pattern, text):
            return True
    return False


def call_key(event: ToolEvent) -> str:
    """Stable identity of a tool call: function + canonical arguments."""
    try:
        args = json.dumps(event.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(event.arguments)
    return f"{event.function_name}:{args}"


def _argument_text(event: ToolEvent) -> str:
    """Flatten argument values (commands, paths, …) for pattern scans."""

    def _walk(value: Any, out: list[str]) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v, out)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _walk(v, out)

    parts: list[str] = [event.function_name]
    _walk(event.arguments, parts)
    return "\n".join(parts)


class TrajectoryMetric:
    """Base: identity + severity. ``evaluate`` must not raise."""

    name: str = "metric"
    category: Literal["efficiency", "robustness", "governance", "integrity"] = (
        "efficiency"
    )
    weight: float = 1.0
    required: bool = False

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        raise NotImplementedError


def _outcome(
    metric: TrajectoryMetric,
    status: Literal["ok", "degraded", "violated", "skipped"],
    score: float | None,
    reasons: list[str],
    evidence: list[str] | None = None,
) -> MetricOutcome:
    return MetricOutcome(
        name=metric.name,
        category=metric.category,
        status=status,
        score=None if score is None else _clamp01(score),
        weight=metric.weight,
        required=getattr(metric, "required", False),
        reasons=reasons,
        evidence=evidence or [],
    )


# --- efficiency ---------------------------------------------------------


class StepEfficiency(TrajectoryMetric):
    """Agent turns used vs the suite-declared step budget.

    ``score = budget / used`` clamped to 1 — how much headroom the
    agent left. ``degraded`` only when the budget was fully consumed.
    """

    name = "step_efficiency"
    category = "efficiency"

    def __init__(self, max_steps: int | None = None) -> None:
        if max_steps is not None and max_steps <= 0:
            raise ValueError(f"{self.name}: max_steps must be positive")
        self.max_steps = max_steps

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        if self.max_steps is None:
            return _outcome(self, "skipped", None, ["no step budget declared"])
        if evidence.agent_steps <= 0:
            return _outcome(self, "skipped", None, ["trajectory has no agent steps"])
        used = evidence.agent_steps
        score = _clamp01(self.max_steps / used)
        if used >= self.max_steps:
            return _outcome(
                self,
                "degraded",
                score,
                [
                    f"agent used {used} steps of the {self.max_steps}-step "
                    "budget — no headroom left"
                ],
                evidence=[f"agent_steps={used}"],
            )
        return _outcome(
            self,
            "ok",
            score,
            [f"agent used {used} of {self.max_steps} granted steps"],
            evidence=[f"agent_steps={used}"],
        )


class TokenEfficiency(TrajectoryMetric):
    """Total tokens vs the suite-declared token budget, with cache share."""

    name = "token_efficiency"
    category = "efficiency"

    def __init__(self, max_tokens: int | None = None) -> None:
        if max_tokens is not None and max_tokens <= 0:
            raise ValueError(f"{self.name}: max_tokens must be positive")
        self.max_tokens = max_tokens

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        if self.max_tokens is None:
            return _outcome(self, "skipped", None, ["no token budget declared"])
        total = evidence.total_tokens
        if not total:
            return _outcome(
                self, "skipped", None, ["sealed transcript carries no token totals"]
            )
        reasons = [f"trial consumed {total} of {self.max_tokens} granted tokens"]
        if evidence.total_prompt_tokens and evidence.total_cached_tokens:
            share = evidence.total_cached_tokens / evidence.total_prompt_tokens
            reasons.append(
                f"cache share {share:.0%} of {evidence.total_prompt_tokens} "
                "prompt tokens"
            )
        score = _clamp01(self.max_tokens / total)
        if total >= self.max_tokens:
            return _outcome(
                self,
                "degraded",
                score,
                reasons + ["token budget fully consumed"],
                evidence=[f"total_tokens={total}"],
            )
        return _outcome(self, "ok", score, reasons, evidence=[f"total_tokens={total}"])


class RedundantActions(TrajectoryMetric):
    """Successful tool calls repeated with identical arguments."""

    name = "redundant_actions"
    category = "efficiency"

    def __init__(self, tolerance: float = 0.1) -> None:
        self.tolerance = tolerance

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        if not evidence.tool_events:
            return _outcome(self, "skipped", None, ["no tool calls to compare"])
        seen: set[str] = set()
        redundant: list[ToolEvent] = []
        for event in evidence.tool_events:
            key = call_key(event)
            if key in seen:
                redundant.append(event)
            else:
                seen.add(key)
        total = len(evidence.tool_events)
        ratio = len(redundant) / total
        score = _clamp01(1.0 - ratio)
        reasons = [
            f"{len(redundant)} of {total} tool calls repeated an already-made "
            "identical call"
        ]
        if len(redundant):
            reasons.append(
                "first occurrences: " + ", ".join(f"step {e.step_id}" for e in redundant)
            )
        status: Literal["ok", "degraded"] = (
            "degraded" if ratio > self.tolerance else "ok"
        )
        return _outcome(self, status, score, reasons)


# --- robustness ---------------------------------------------------------


class ToolErrorRate(TrajectoryMetric):
    """Fraction of tool calls whose observation looks like a failure."""

    name = "tool_error_rate"
    category = "robustness"

    def __init__(
        self,
        tolerance: float = 0.25,
        extra_patterns: tuple[str, ...] = (),
    ) -> None:
        self.tolerance = tolerance
        self.extra_patterns = tuple(extra_patterns)

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        if not evidence.tool_events:
            return _outcome(self, "skipped", None, ["no tool calls to judge"])
        failed = [
            e
            for e in evidence.tool_events
            if looks_like_error(e.observation_text, self.extra_patterns)
        ]
        total = len(evidence.tool_events)
        rate = len(failed) / total
        score = _clamp01(1.0 - rate)
        reasons = [f"{len(failed)} of {total} tool calls failed"]
        if failed:
            reasons.append(
                "failures at: " + ", ".join(f"step {e.step_id}" for e in failed)
            )
        status: Literal["ok", "degraded"] = (
            "degraded" if rate > self.tolerance else "ok"
        )
        return _outcome(
            self,
            status,
            score,
            reasons,
            evidence=[f"error_rate={rate:.3f}"],
        )


class LoopDetection(TrajectoryMetric):
    """The same tool call repeated back-to-back without variation."""

    name = "loop_detection"
    category = "robustness"

    def __init__(self, max_repeat: int = 3) -> None:
        if max_repeat < 2:
            raise ValueError(f"{self.name}: max_repeat must be >= 2")
        self.max_repeat = max_repeat

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        events = evidence.tool_events
        if not events:
            return _outcome(self, "skipped", None, ["no tool calls to judge"])
        runs: list[list[ToolEvent]] = []
        current: list[ToolEvent] = []
        current_key: str | None = None
        for event in events:
            key = call_key(event)
            if key == current_key:
                current.append(event)
            else:
                if current:
                    runs.append(current)
                current = [event]
                current_key = key
        if current:
            runs.append(current)
        loops = [run for run in runs if len(run) >= self.max_repeat]
        if not loops:
            return _outcome(
                self,
                "ok",
                1.0,
                [f"no call repeated {self.max_repeat}+ times consecutively"],
            )
        excess = sum(len(run) - 1 for run in loops)
        score = _clamp01(1.0 - excess / len(events))
        reasons = [
            f"{len(loops)} loop(s): identical call repeated {self.max_repeat}+ "
            "times consecutively"
        ]
        for run in loops:
            reasons.append(
                f"loop at step {run[0].step_id} repeated {len(run)} times"
            )
        return _outcome(
            self,
            "degraded",
            score,
            reasons,
            evidence=[f"step {run[0].step_id}" for run in loops],
        )


class RecoveryAbility(TrajectoryMetric):
    """Did the agent change approach after a failing call?

    An error is *recovered* when the next tool call differs from the
    failed one and succeeds. Repeating the identical failing call is
    the opposite of recovery. A trailing error with no successor is
    excluded from the denominator (unknowable).
    """

    name = "recovery"
    category = "robustness"

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        events = evidence.tool_events
        errors = [
            (i, e)
            for i, e in enumerate(events)
            if looks_like_error(e.observation_text)
        ]
        if not errors:
            return _outcome(
                self, "ok", 1.0, ["no failed tool calls — nothing to recover from"]
            )
        judged = 0
        recovered = 0
        for i, event in errors:
            if i + 1 >= len(events):
                continue  # trailing error: no successor to judge
            successor = events[i + 1]
            judged += 1
            if call_key(successor) != call_key(event) and not looks_like_error(
                successor.observation_text
            ):
                recovered += 1
        if judged == 0:
            return _outcome(
                self,
                "ok",
                1.0,
                [f"{len(errors)} error(s) all occurred at the trajectory tail"],
            )
        ratio = recovered / judged
        score = _clamp01(ratio)
        reasons = [
            f"agent changed approach and succeeded after {recovered} of "
            f"{judged} judged error(s)"
        ]
        status: Literal["ok", "degraded"] = "ok" if ratio == 1.0 else "degraded"
        return _outcome(self, status, score, reasons)


# --- governance ---------------------------------------------------------


class BudgetAdherence(TrajectoryMetric):
    """How the trial ended relative to the granted budget.

    ``agent_claimed_done`` is the only healthy ending; budget or
    timeout kills are degraded. Infrastructure stop reasons are not
    the agent's doing — the metric skips instead of punishing.
    """

    name = "budget_adherence"
    category = "governance"

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        reason = evidence.stop_reason
        if reason == "agent_claimed_done":
            return _outcome(
                self, "ok", 1.0, ["agent finished and claimed done within budget"]
            )
        if reason == "budget_exhausted":
            return _outcome(
                self,
                "degraded",
                0.5,
                ["trial ended by exhausting the granted budget"],
            )
        if reason == "timeout_killed":
            return _outcome(
                self,
                "degraded",
                0.0,
                ["trial ended by the wall-clock timeout"],
            )
        return _outcome(
            self,
            "skipped",
            None,
            [f"trial stopped by infrastructure ({reason}); not an agent behaviour"],
        )


# --- integrity ----------------------------------------------------------


class ForbiddenAccess(TrajectoryMetric):
    """Tool-call arguments referencing forbidden targets (anti-cheat).

    This is the integrity gate suites such as Terminal-Bench rely on:
    an agent that reads the verifier's tests or the reference solution
    has not solved the task. Any match is a violation — the metric is
    verdict-affecting and typically declared with ``veto``.
    """

    name = "forbidden_access"
    category = "integrity"

    def __init__(
        self,
        patterns: tuple[str, ...] = (),
        required: bool = False,
    ) -> None:
        for pattern in patterns:
            re.compile(pattern)  # validate early
        self.patterns = tuple(patterns)
        self.required = required

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        if not self.patterns:
            return _outcome(
                self, "skipped", None, ["no forbidden patterns declared"]
            )
        if not evidence.tool_events:
            return _outcome(
                self, "ok", 1.0, ["no tool calls — nothing forbidden was accessed"]
            )
        violations: list[str] = []
        for event in evidence.tool_events:
            text = _argument_text(event)
            for pattern in self.patterns:
                match = re.search(pattern, text)
                if match:
                    snippet = match.group(0)
                    context = text.replace("\n", " ")[:120]
                    violations.append(
                        f"step {event.step_id} {event.function_name} matched "
                        f"{snippet!r} — context: {context!r}"
                    )
                    break
        if violations:
            return _outcome(
                self,
                "violated",
                0.0,
                [f"{len(violations)} forbidden access violation(s)"]
                + violations,
                evidence=violations,
            )
        return _outcome(
            self,
            "ok",
            1.0,
            [f"no forbidden target accessed across {len(evidence.tool_events)} tool calls"],
        )


class ScopeDiscipline(TrajectoryMetric):
    """Write destinations outside the allowed roots.

    Heuristic: only *explicit absolute-path* write targets in shell
    commands are judged (redirections, ``tee``, ``mkdir``, ``touch``,
    ``rm``). Relative paths resolve inside the workspace and are not
    flagged. Suites that do not declare roots skip this metric.
    """

    name = "scope_discipline"
    category = "integrity"

    _WRITE_TARGETS = (
        re.compile(r"(?:>>|>)\s*(\S+)"),
        re.compile(r"\btee\s+(?:-\S+\s+)*(\S+)"),
        re.compile(r"\bmkdir\s+(?:-\S+\s+)*(\S+)"),
        re.compile(r"\btouch\s+(?:-\S+\s+)*(\S+)"),
        re.compile(r"\brm\s+(?:-\S+\s+)*(\S+)"),
    )

    def __init__(
        self,
        allowed_prefixes: tuple[str, ...] = (),
        required: bool = False,
    ) -> None:
        self.allowed_prefixes = tuple(allowed_prefixes)
        self.required = required

    def evaluate(self, evidence: TrajectoryEvidence) -> MetricOutcome:
        if not self.allowed_prefixes:
            return _outcome(
                self, "skipped", None, ["no allowed roots declared"]
            )
        violations: list[str] = []
        for event in evidence.tool_events:
            if event.function_name not in ("bash", "shell", "exec"):
                continue
            command = str(event.arguments.get("command") or "")
            for regex in self._WRITE_TARGETS:
                for match in regex.finditer(command):
                    target = match.group(1).strip("\"'")
                    if not target.startswith("/"):
                        continue  # relative → inside the workspace
                    if any(
                        target == prefix or target.startswith(prefix.rstrip("/") + "/")
                        for prefix in self.allowed_prefixes
                    ):
                        continue
                    violations.append(
                        f"step {event.step_id} writes outside allowed roots: "
                        f"{target}"
                    )
        if violations:
            return _outcome(
                self,
                "violated",
                0.0,
                [f"{len(violations)} out-of-scope write(s)"] + violations,
                evidence=violations,
            )
        return _outcome(
            self, "ok", 1.0, ["all explicit write targets stayed in scope"]
        )
