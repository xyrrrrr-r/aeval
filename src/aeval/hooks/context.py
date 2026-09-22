"""Evaluation context shared by all aeval hooks for one job."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aeval.contracts import RuntimeLock
from aeval.suite_models import ResolvedSuite


@dataclass
class TrialState:
    """Mutable per-trial audit state accumulated across hook events."""

    trial_id: str
    baseline_ok: bool = True
    baseline_failures: list[str] = field(default_factory=list)
    evidence_ok: bool = True
    evidence_issues: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    infra_invalid_reasons: list[str] = field(default_factory=list)

    def mark_infra_invalid(self, reason: str) -> None:
        self.infra_invalid_reasons.append(reason)
        self.stop_reason = "infra_error"


@dataclass
class EvaluationContext:
    """Everything the hooks need: lock, suite, paths, per-trial state.

    The context deliberately owns no trajectory data and no grading —
    hooks only audit and gate; grading happens later in the core
    process over sealed evidence.
    """

    run_id: str
    runtime_lock: RuntimeLock
    suite: ResolvedSuite
    run_dir: Path
    store_path: Path
    trials: dict[str, TrialState] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def trial_state(self, trial_id: str) -> TrialState:
        if trial_id not in self.trials:
            self.trials[trial_id] = TrialState(trial_id=trial_id)
        return self.trials[trial_id]

    def exclusion_lines(self) -> list[str]:
        lines: list[str] = []
        for state in self.trials.values():
            for reason in state.infra_invalid_reasons:
                lines.append(f"{state.trial_id}: {reason}")
        return lines
