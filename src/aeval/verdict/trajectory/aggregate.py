"""Fold metric outcomes into one GradeResult.

The rules, in order:

1. **Integrity violation ⇒ ``fail``.** Any metric declared
   ``severity: integrity`` (v5 Step 2: the *declaration* is the source;
   ``integrity_metrics`` carries those ids) with ``status ==
   "violated"`` fails the trajectory layer with a valid 0.0 score.
   Free-form modules without a metric library fall back to the
   implementation's ``category == "integrity"``. Combined with a
   ``veto: true`` suite declaration this overturns an outcome ``pass``
   (see ``decide_final_verdict`` in ``aeval.verdict.base``).
2. **Required metric skipped ⇒ ``cannot_judge``.** A metric the suite
   marked ``required`` (e.g. anti-cheat screening) that could not be
   evaluated makes the trial unjudgeable at this layer — never graded
   without it.
3. **Nothing evaluated ⇒ ``cannot_judge``.** Every metric skipped
   means the trajectory carries no judgeable signal.
4. **Otherwise ``pass`` with the weighted mean** of evaluated metrics
   (``ok`` and ``degraded``); skipped metrics are named in the reasons
   and contribute nothing. Efficiency and robustness losses degrade
   the score — they never flip the status.
"""

from __future__ import annotations

from typing import Sequence

from aeval.contracts import GradeResult, MetricOutcome, Score

__all__ = ["fold_outcomes"]


def fold_outcomes(
    outcomes: Sequence[MetricOutcome],
    *,
    grader_id: str,
    grader_version: str,
    veto: bool,
    layer: str = "trajectory",
    integrity_metrics: frozenset[str] | None = None,
) -> GradeResult:
    outcomes = list(outcomes)

    def _result(
        status: str,
        score: Score,
        reasons: list[str],
    ) -> GradeResult:
        return GradeResult(
            grader_id=grader_id,
            grader_version=grader_version,
            layer=layer,  # type: ignore[arg-type]
            veto=veto,
            score=score,
            status=status,  # type: ignore[arg-type]
            reasons=reasons,
            metrics=outcomes,
        )

    # Rule 1 — integrity violations fail the trial。两条来源，各有其主：
    # 声明路径（族/守卫用 METRICS 注册表装配，``integrity_metrics`` 来自
    # ``severity: integrity``）以声明为准；整词表守卫（模块自己构造
    # TrajectoryGrader，没有声明层）以指标实现自己的
    # ``MetricOutcome.category`` 为准——那是该模块自己的契约，不是第二份
    # 声明。
    def _is_integrity(outcome: MetricOutcome) -> bool:
        if integrity_metrics is not None:
            return outcome.name in integrity_metrics
        return outcome.category == "integrity"

    violated = [o for o in outcomes if _is_integrity(o) and o.status == "violated"]
    if violated:
        reasons: list[str] = [
            f"trajectory integrity violated by {len(violated)} metric(s): "
            + ", ".join(o.name for o in violated)
        ]
        for outcome in violated:
            reasons.extend(f"[{outcome.name}] {r}" for r in outcome.reasons)
        return _result("fail", Score(value=0.0, valid=True), reasons)

    # Rule 2 — a required metric that could not evaluate.
    required_skipped = [o for o in outcomes if o.required and o.status == "skipped"]
    if required_skipped:
        reasons = [
            f"required metric {o.name!r} could not be evaluated — refusing to "
            "judge without it"
            for o in required_skipped
        ]
        return _result(
            "cannot_judge",
            Score(value=None, valid=False, invalid_reasons=list(reasons)),
            reasons,
        )

    evaluated = [o for o in outcomes if o.status in ("ok", "degraded")]
    skipped = [o for o in outcomes if o.status == "skipped"]

    # Rule 3 — nothing judgeable.
    if not evaluated:
        reasons = [
            "no trajectory metric could be evaluated from the sealed evidence"
        ] + [f"[{o.name}] {r}" for o in skipped for r in o.reasons]
        return _result(
            "cannot_judge",
            Score(value=None, valid=False, invalid_reasons=[
                "no judgeable trajectory metric"
            ]),
            reasons,
        )

    # Rule 4 — weighted mean over evaluated metrics.
    total_weight = sum(o.weight for o in evaluated)
    value = sum((o.score or 0.0) * o.weight for o in evaluated) / total_weight
    degraded = [o for o in evaluated if o.status == "degraded"]
    reasons = [f"{len(evaluated)} trajectory metric(s) evaluated"]
    if degraded:
        reasons.append(
            "degraded: " + ", ".join(o.name for o in degraded)
        )
        for outcome in degraded:
            reasons.extend(f"[{outcome.name}] {r}" for r in outcome.reasons)
    if skipped:
        reasons.append(
            "skipped (not judgeable): " + ", ".join(o.name for o in skipped)
        )
    return _result("pass", Score(value=round(value, 4), valid=True), reasons)
