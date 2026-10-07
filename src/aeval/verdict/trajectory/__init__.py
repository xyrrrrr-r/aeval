"""Trajectory grading framework — the top-level design.

Composition::

    TrajectoryGrader (base)      sealed transcript, metric loop, identity
      └── presets.build_standard_grader     common runtime rubric
      └── presets.build_terminalbench_grader  standard + integrity gates
      └── presets.ThresholdTrajectoryGrader   aggregate must clear a threshold
            └── build_conversation_quality_grader  11 quality dimensions
      └── presets.build_output_security_grader   leak/injection integrity gate

Layers:
    base.py      sealed-evidence loading (sha256-verified), evidence views
    metrics.py   the built-in metric library (efficiency / robustness /
                 governance / integrity)
    quality.py   conversation-quality + output-security metrics (P1),
                 anchor data for suite-authored rubrics
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
    TrajectoryMessage,
    agent_replies_from_steps,
    build_evidence,
    load_sealed_transcript,
    user_messages_from_steps,
)
from aeval.verdict.trajectory.metrics import (
    BudgetAdherence,
    ForbiddenAccess,
    LoopDetection,
    RedundantActions,
    RecoveryAbility,
    ScopeDiscipline,
    StepEfficiency,
    TaskWallClock,
    TokenEfficiency,
    TrajectoryMetric,
    ToolErrorRate,
    TurnEfficiency,
    looks_like_error,
)
from aeval.verdict.trajectory.presets import (
    T_BENCH_ALLOWED_PREFIXES,
    T_BENCH_FORBIDDEN_PATTERNS,
    ThresholdTrajectoryGrader,
    build_conversation_quality_grader,
    build_output_security_grader,
    build_standard_grader,
    build_terminalbench_grader,
)
from aeval.verdict.trajectory.quality import (
    CapabilityCognition,
    ClarificationAbility,
    ComplexityHandling,
    ContextAnchor,
    ContextRetention,
    ForkMemoryRetention,
    FormatSpec,
    HallucinationAnchor,
    HallucinationCheck,
    IdentityCognition,
    InjectionResistance,
    InstructionFollowing,
    MemoryAnchor,
    NoiseRobustness,
    QualityAnchors,
    ResponseBrevity,
    SensitiveLeakage,
    ScopeHandling,
    ToolExpectation,
    ToolSelection,
    TrajectoryMessage,
    agent_replies_of,
    user_messages_of,
)

__all__ = [
    "SealedTranscriptError",
    "ToolEvent",
    "TrajectoryEvidence",
    "TrajectoryGrader",
    "TrajectoryMetric",
    "TrajectoryMessage",
    "MetricOutcome",
    "BudgetAdherence",
    "ForbiddenAccess",
    "LoopDetection",
    "RedundantActions",
    "RecoveryAbility",
    "ScopeDiscipline",
    "StepEfficiency",
    "TaskWallClock",
    "TokenEfficiency",
    "ToolErrorRate",
    "TurnEfficiency",
    "ResponseBrevity",
    "IdentityCognition",
    "CapabilityCognition",
    "ToolSelection",
    "ContextRetention",
    "ClarificationAbility",
    "ScopeHandling",
    "ComplexityHandling",
    "HallucinationCheck",
    "NoiseRobustness",
    "InstructionFollowing",
    "ForkMemoryRetention",
    "SensitiveLeakage",
    "InjectionResistance",
    "QualityAnchors",
    "ToolExpectation",
    "ContextAnchor",
    "HallucinationAnchor",
    "MemoryAnchor",
    "FormatSpec",
    "agent_replies_of",
    "user_messages_of",
    "agent_replies_from_steps",
    "user_messages_from_steps",
    "build_evidence",
    "build_standard_grader",
    "build_terminalbench_grader",
    "ThresholdTrajectoryGrader",
    "build_conversation_quality_grader",
    "build_output_security_grader",
    "fold_outcomes",
    "load_sealed_transcript",
    "looks_like_error",
    "T_BENCH_FORBIDDEN_PATTERNS",
    "T_BENCH_ALLOWED_PREFIXES",
]
