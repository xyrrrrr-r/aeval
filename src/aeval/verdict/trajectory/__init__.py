"""Trajectory grading framework — the top-level design.

Composition::

    TrajectoryGrader (base)      sealed transcript, metric loop, identity
      └── presets.build_standard_grader     common runtime rubric
      └── presets.build_terminalbench_grader  standard + integrity gates

Layers:
    base.py      sealed-evidence loading (sha256-verified), evidence views
    metrics.py   the built-in metric library (efficiency / robustness /
                 governance / integrity)
    aggregate.py folding rules: integrity ⇒ fail, efficiency ⇒ score,
                 unjudgeable ⇒ cannot_judge
    presets.py   suite-ready builders (inherit, don't reimplement)
"""

from aeval.contracts import MetricOutcome
from aeval.verdict.trajectory.aggregate import fold_outcomes
from aeval.verdict.trajectory.base import (
    SealedTranscriptError,
    ToolEvent,
    TrajectoryEvidence,
    TrajectoryGrader,
    build_evidence,
    load_sealed_transcript,
)
from aeval.verdict.trajectory.metrics import (
    BudgetAdherence,
    ForbiddenAccess,
    LoopDetection,
    RedundantActions,
    RecoveryAbility,
    ScopeDiscipline,
    StepEfficiency,
    TokenEfficiency,
    TrajectoryMetric,
    ToolErrorRate,
    looks_like_error,
)
from aeval.verdict.trajectory.presets import (
    T_BENCH_ALLOWED_PREFIXES,
    T_BENCH_FORBIDDEN_PATTERNS,
    build_standard_grader,
    build_terminalbench_grader,
)

__all__ = [
    "SealedTranscriptError",
    "ToolEvent",
    "TrajectoryEvidence",
    "TrajectoryGrader",
    "TrajectoryMetric",
    "MetricOutcome",
    "BudgetAdherence",
    "ForbiddenAccess",
    "LoopDetection",
    "RedundantActions",
    "RecoveryAbility",
    "ScopeDiscipline",
    "StepEfficiency",
    "TokenEfficiency",
    "ToolErrorRate",
    "build_evidence",
    "build_standard_grader",
    "build_terminalbench_grader",
    "fold_outcomes",
    "load_sealed_transcript",
    "looks_like_error",
    "T_BENCH_FORBIDDEN_PATTERNS",
    "T_BENCH_ALLOWED_PREFIXES",
]
