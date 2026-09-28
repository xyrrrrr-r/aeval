"""Preset grader builders (§4) — how suites inherit the top-level design.

Suites do not subclass or reimplement anything: a suite grader module
is a thin re-export around a builder::

    # suites/<suite>/graders/<name>.py
    from aeval.verdict.trajectory.presets import build_terminalbench_grader

    GRADER_ID = "terminalbench-trajectory"
    GRADER_VERSION = "v1"
    LAYER = "trajectory"
    REQUIRED_FIELDS = ["events", "token_usage"]

    _IMPL = build_terminalbench_grader(veto=True)

    async def grade(record):
        return await _IMPL.grade(record)

Identity rules the wrapper must obey (enforced by the loader and the
executor): the module's ``GRADER_ID``/``GRADER_VERSION`` must match the
suite declaration, and the builder's ``veto`` must equal the suite's
``veto`` flag or the returned result is rejected as a lying grader.
"""

from __future__ import annotations

from aeval.verdict.trajectory.base import TrajectoryGrader
from aeval.verdict.trajectory.metrics import (
    BudgetAdherence,
    ForbiddenAccess,
    LoopDetection,
    RedundantActions,
    RecoveryAbility,
    ScopeDiscipline,
    StepEfficiency,
    TokenEfficiency,
    ToolErrorRate,
)

__all__ = [
    "T_BENCH_FORBIDDEN_PATTERNS",
    "T_BENCH_ALLOWED_PREFIXES",
    "build_standard_grader",
    "build_terminalbench_grader",
]


def build_standard_grader(
    grader_id: str,
    grader_version: str,
    *,
    veto: bool = False,
    max_steps: int | None = None,
    max_tokens: int | None = None,
    error_tolerance: float = 0.25,
    error_patterns: tuple[str, ...] = (),
    max_repeat: int = 3,
) -> TrajectoryGrader:
    """The common runtime rubric: efficiency + robustness + governance.

    No integrity metric — a suite that needs rule enforcement layers
    :func:`build_terminalbench_grader` or adds its own
    ``ForbiddenAccess``/``ScopeDiscipline`` declarations.
    """
    metrics = [
        StepEfficiency(max_steps=max_steps),
        TokenEfficiency(max_tokens=max_tokens),
        ToolErrorRate(tolerance=error_tolerance, extra_patterns=error_patterns),
        LoopDetection(max_repeat=max_repeat),
        RecoveryAbility(),
        RedundantActions(),
        BudgetAdherence(),
    ]
    return TrajectoryGrader(grader_id, grader_version, metrics, veto=veto)


#: Terminal-Bench anti-cheat baseline: the verifier's tests directory,
#: its runner, and the reference solution must not be read by the
#: agent. Conservative on purpose; suites override via
#: ``forbidden_patterns`` rather than weakening in place.
T_BENCH_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"/tests(?:/|\b)",
    r"(?<![\w./-])tests/\S*\.py",
    r"\bsolution\.(?:sh|py|md)\b",
    r"\brun-tests\.sh\b",
)

#: Explicit write targets must stay inside the task workspace (and
#: scratch space).
T_BENCH_ALLOWED_PREFIXES: tuple[str, ...] = ("/root", "/workspace", "/tmp", "/dev/shm")


def build_terminalbench_grader(
    grader_id: str = "terminalbench-trajectory",
    grader_version: str = "v1",
    *,
    veto: bool = True,
    max_steps: int | None = None,
    max_tokens: int | None = None,
    forbidden_patterns: tuple[str, ...] = T_BENCH_FORBIDDEN_PATTERNS,
    allowed_prefixes: tuple[str, ...] = T_BENCH_ALLOWED_PREFIXES,
    error_tolerance: float = 0.25,
    error_patterns: tuple[str, ...] = (),
    max_repeat: int = 3,
) -> TrajectoryGrader:
    """Terminal-Bench trajectory grader — inherits the standard rubric.

    Adds the two integrity gates, both ``required``: anti-cheat
    screening (never silently skipped) and workspace scope discipline.
    ``veto`` defaults to ``True``: a pass obtained by reading the tests
    or the oracle solution is overturned by ``decide_final_verdict``.
    """
    metrics = [
        StepEfficiency(max_steps=max_steps),
        TokenEfficiency(max_tokens=max_tokens),
        ToolErrorRate(tolerance=error_tolerance, extra_patterns=error_patterns),
        LoopDetection(max_repeat=max_repeat),
        RecoveryAbility(),
        RedundantActions(),
        BudgetAdherence(),
        ForbiddenAccess(patterns=forbidden_patterns, required=True),
        ScopeDiscipline(allowed_prefixes=allowed_prefixes, required=True),
    ]
    return TrajectoryGrader(grader_id, grader_version, metrics, veto=veto)
