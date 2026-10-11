"""Grading pipeline: evidence in, classified record out.

This is the production path the plugin drives once a trial's evidence
gate has passed: load the suite's versioned graders, grade the sealed
record, validate returned identities and veto declarations, decide the
final verdict, and persist record + grades in one atomic transaction.

Fail-closed rules:

- a grader that cannot be loaded, crashes, or returns a mismatched
  identity classifies the trial ``infra_invalid`` — never a lucky pass;
- ``judge_finished`` is only marked after every grader returned a
  validated result;
- missing required transcript fields short-circuit to ``cannot_judge``
  without executing the grader (handled by ``grade_trial``).
"""

from __future__ import annotations

from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

from aeval.contracts import (
    AdapterSpec,
    BudgetSnapshot,
    ArtifactRef,
    EvidenceBundle,
    GradeResult,
    RequirementBitmap,
    TrialCoordinates,
    TrialRecord,
    VersionsBundle,
)
from aeval.hooks.evidence import evaluate_requirements
from aeval.suite_models import ResolvedSuite
from aeval.store.sqlite import StoreConflictError, TrialStore
from aeval.verdict.base import ResolvedGrader, decide_final_verdict
from aeval.verdict.executor import grade_trial
from aeval.verdict.loader import (
    GraderLoadError,
    load_grader,
    load_grader_module,
    split_impl,
)
from aeval.verdict.trajectory.assembly import (
    check_declared_metric_contract,
    metric_library,
    metric_registry,
    probe_metric_factory,
)
from aeval.verdict.progress import RequirementProgress

__all__ = [
    "GradingPipelineError",
    "logical_artifacts",
    "load_suite_graders",
    "resolve_grader_identities",
    "validate_metric_contracts",
    "build_trial_record",
    "grade_and_record",
]


class GradingPipelineError(RuntimeError):
    """The grading pipeline could not produce a valid record."""


def logical_artifacts(evidence: EvidenceBundle) -> dict[str, ArtifactRef]:
    """Re-key sealed artifacts by their logical collect names.

    The evidence bundle keys by on-disk path (fixed-path discipline);
    the grader-facing contract keys by LOGICAL name — ``observable:result``,
    ``canonical_transcript``, … — so versioned suite graders stay
    decoupled from the trial-dir layout. The collection manifest is the
    only authoritative path→name mapping: an artifact it does not name,
    or a path it maps twice, fails closed instead of being graded under
    a guessed key.

    A bundle without a manifest (direct API use; production bundles
    always carry one — the evidence gate enforces it) passes through
    with its caller-authored keys unchanged.
    """
    manifest = evidence.collection_manifest
    if manifest is None:
        return dict(evidence.artifacts)
    by_path: dict[str, str] = {}
    for outcome in manifest.outcomes:
        if outcome.output_path in by_path:
            raise GradingPipelineError(
                f"collection manifest maps {outcome.output_path!r} twice — "
                "logical artifact names are ambiguous"
            )
        by_path[outcome.output_path] = outcome.name
    logical: dict[str, ArtifactRef] = {}
    for path, ref in evidence.artifacts.items():
        name = by_path.get(path)
        if name is None:
            raise GradingPipelineError(
                f"sealed artifact {path!r} has no logical name in the "
                "collection manifest — refusing to grade under a guessed key"
            )
        logical[name] = ref
    return logical


def _family_bindings(
    suite: ResolvedSuite, task_id: str | None
) -> dict[str, str]:
    """grader_id → 族键（装配期注入的运行期上下文）。

    一个引擎模块可被多个族复用（用例集在该引擎上不相交，加载期
    校验），判分时它需要知道自己本次代表哪个族——族键决定它从密
    封锚点副本里读哪一份。task_id 为 None（全体展开）时按族声明
    序取每引擎首个族（诊断视图，不参与判分）。
    """
    bindings: dict[str, str] = {}
    seen_impls: set[str] = set()
    for key, spec in suite.families.items():
        if task_id is not None and not spec.binds(task_id):
            continue
        if spec.engine.impl in seen_impls:
            continue  # impl 去重后的有效集合里每引擎只跑一次
        seen_impls.add(spec.engine.impl)
        bindings.setdefault(f"__impl__:{spec.engine.impl}", key)
    return bindings


def _family_specs(
    suite: ResolvedSuite, task_id: str | None
) -> dict[str, Any]:
    """impl → 族声明（与 :func:`_family_bindings` 同一去重口径）。

    v5 装配需要族声明本身（指标清单/阈值），不只是族键：声明是判分真
    源，装配不能只看 impl。同一 impl 被多族复用时取声明序首个族（与
    运行期绑定一致）。
    """
    specs: dict[str, Any] = {}
    for key, spec in suite.families.items():
        if task_id is not None and not spec.binds(task_id):
            continue
        specs.setdefault(spec.engine.impl, spec)
    return specs


def load_suite_graders(
    suite: ResolvedSuite, task_id: str | None = None
) -> list[ResolvedGrader]:
    """Load the trial's effective grader set against the suite root.

    Family suites (v4): the assembly for one trial is the official
    baseline + every guard + the engines of every family declaring that
    ``task_id`` (activation order, deduplicated by impl). Legacy suites
    keep today's whole-suite behavior (task_id is ignored).

    Fail-closed extras for family suites:

    - offline judging suites (no local dataset to scan against): the
      sealed trial's task_id must appear in the family case union — a
      trial this contract has no grading binding for is refused, never
      silently skipped (v4 rule);
    - an empty effective set (theoretical: the baseline is structural)
      raises :class:`GradingPipelineError`.
    """
    root = Path(suite.suite_dir)
    # baseline-only 套件（无族）没有族绑定可查：试次只吃基线 + 守卫。
    if suite.families and task_id is not None:
        dataset_ref = suite.overlay.harbor.dataset
        dataset_path = (
            root / dataset_ref
            if isinstance(dataset_ref, str) and dataset_ref
            else None
        )
        offline = dataset_path is None or not dataset_path.is_file()
        if offline and not any(
            spec.binds(task_id) for spec in suite.families.values()
        ):
            raise GradingPipelineError(
                f"sealed trial's task {task_id!r} is not declared by any "
                "grade category — this offline judging contract has no "
                "grading binding for it (not guessed, not silently skipped)"
            )
    graders: list[ResolvedGrader] = []
    family_by_impl = _family_bindings(suite, task_id)
    family_specs = _family_specs(suite, task_id)
    for declared in suite.resolved_graders(task_id):
        reference, _declared_version = split_impl(declared.impl)
        family_key = family_by_impl.get(f"__impl__:{declared.impl}")
        graders.append(
            load_grader(
                root / reference,
                declared,
                family_key=family_key,
                family_spec=family_specs.get(declared.impl),
            )
        )
    if not graders:
        raise GradingPipelineError(
            "effective grader set is empty — the official baseline is "
            "structurally mandatory in family suites"
        )
    return graders


def sealed_dimension_model(suite: ResolvedSuite) -> dict:
    """清单封存的维度模型（族模式补引擎自证身份）。

    族模式（v4）：``families`` 的每个引擎 dict、baseline 与
    guards 合并 resolved ``grader_id``/``grader_version``——报告 join
    只读密封映射，不 import 判分器模块（P4）。说谎/缺失的引擎在这
    里报错（fail-closed，不静默）。legacy 套件原样返回。
    """
    model = dict(suite.dimension_model or {})
    identities = resolve_grader_identities(suite)
    categories = {
        key: {
            **spec,
            "engine": {
                **(spec.get("engine") or {}),
                **identities.get((spec.get("engine") or {}).get("impl"), {}),
            },
        }
        for key, spec in (model.get("families") or {}).items()
    }
    baseline_impl = (model.get("baseline") or {}).get("impl")
    guards = [
        {**guard, **identities.get(guard.get("impl"), {})}
        for guard in (model.get("guards") or [])
    ]
    model.update(
        {
            "families": categories,
            "baseline": {
                **(model.get("baseline") or {}),
                **identities.get(baseline_impl, {}),
            },
            "guards": guards,
        }
    )
    return model


def resolve_grader_identities(suite: ResolvedSuite) -> dict[str, dict[str, str]]:
    """impl → {grader_id, grader_version}，供清单封存。

    加载每个声明（基线 + 守卫 + 族引擎）的模块一次，读取其自证身
    份。说谎/缺失的模块在这里报错——清单封存的 join 映射（族→用例
    →引擎，含 resolved grader_id）是报告侧唯一的家谱来源，报告不
    import 判分器模块（P4）。
    """
    root = Path(suite.suite_dir)
    identities: dict[str, dict[str, str]] = {}
    for declared in suite.resolved_graders(None):
        if declared.impl in identities:
            continue
        reference, _version = split_impl(declared.impl)
        from aeval.verdict.loader import load_grader_module

        try:
            module = load_grader_module(root / reference, declared)
        except GraderLoadError as exc:
            raise GradingPipelineError(
                f"cannot seal grader identities: {declared.impl}: {exc}"
            ) from exc
        identities[declared.impl] = {
            "grader_id": module.GRADER_ID,
            "grader_version": module.GRADER_VERSION,
        }
    return identities


def validate_metric_contracts(suite: ResolvedSuite) -> None:
    """声明 × 引擎契约对账（组合期；v5 Step 6 起族与守卫合一）。

    族与守卫是同一件事的两种消费者——**维度**（进族行、有阈值口径）与
    **有效性门**（套件级、每试次折叠一次、无类别行）——所以对账也只有一
    段代码：

    - 声明了指标清单的消费者，必须能在自己模块的 ``METRICS`` 指标库里
      逐 id 找到实现（声明了却判不了的指标是声明错误，不是被跳过的
      行）；守卫的清单只允许轨迹层；
    - 声明的 ``severity`` 必须与实现自身的 ``category`` 一致（声明与实
      现互相矛盾的严重度会静默改变 veto 语义）；
    - 族的逐用例输入**按指标 id 绑定**（``cases[task].inputs[mid]``），每
      份绑定都必须能真正构造出那项指标——构造不出来的输入是声明错误
      （没有锚点词表这种代码侧第二真源，也没有静默兜底）。

    没声明清单的消费者（legacy 判分器/守卫）不强制改造，行为逐字节不变。
    """
    root = Path(suite.suite_dir)

    for key, spec in suite.families.items():
        if not spec.metrics:
            continue
        owner = f"family {key!r}"
        reference, _version = split_impl(spec.engine.impl)
        try:
            module = load_grader_module(root / reference, spec.engine)
        except GraderLoadError as exc:
            raise GradingPipelineError(
                f"family {key!r} engine cannot be loaded: {exc}"
            ) from exc
        registry = metric_registry(module)
        if registry is None:
            continue
        try:
            check_declared_metric_contract(
                registry,
                owner=owner,
                metric_ids=spec.metric_ids(),
                declared_severities={m.id: m.severity for m in spec.metrics},
                declared_inputs={
                    m.id: (next(iter(m.inputs.values())) if m.inputs else None)
                    for m in spec.metrics
                },
            )
        except GraderLoadError as exc:
            raise GradingPipelineError(f"{owner}: {exc}") from exc
        for task_id in spec.task_ids():
            for metric_id, metric_inputs in spec.inputs_for(task_id).items():
                factory = registry.get(metric_id)
                if factory is None:
                    # 模型层已拒绝"给未声明的指标写输入"，这里兜住声明/
                    # 实现错配的第二条路径。
                    continue
                try:
                    probe_metric_factory(metric_id, factory, metric_inputs)
                except GraderLoadError as exc:
                    raise GradingPipelineError(
                        f"family {key!r} task {task_id!r}: {exc}"
                    ) from exc

    for guard in suite.guards():
        declared_metrics = list(getattr(guard, "metrics", None) or [])
        if not declared_metrics:
            continue
        owner = f"guard {guard.impl!r}"
        reference, version = split_impl(guard.impl)
        try:
            module = load_grader_module(root / reference, guard)
        except GraderLoadError as exc:
            raise GradingPipelineError(
                f"{owner} cannot be loaded: {exc}"
            ) from exc
        try:
            registry = metric_library(
                module,
                grader_id=reference,
                grader_version=version or getattr(module, "GRADER_VERSION", "?"),
                owner=owner,
                layer=guard.layer,
                require_trajectory=True,
            )
            check_declared_metric_contract(
                registry,
                owner=owner,
                metric_ids=[m.id for m in declared_metrics],
                declared_severities={m.id: m.severity for m in declared_metrics},
            )
        except GraderLoadError as exc:
            raise GradingPipelineError(f"{owner}: {exc}") from exc


def build_trial_record(
    *,
    trial_id: str,
    coordinates: TrialCoordinates,
    stop_reason: str,
    baseline_ok: bool,
    progress: RequirementProgress,
    artifacts: dict[str, ArtifactRef],
    transcript_extra: dict[str, Any] | None,
    grader_versions: dict[str, str],
    artifact_base: str | None = None,
    adapter: AdapterSpec | None = None,
) -> TrialRecord:
    """Assemble the sealed TrialRecord from verified stage facts.

    The requirements bitmap comes from the staged progress tracker —
    never a default-true copy. Grading results and the verdict are added
    by :func:`grade_and_record` after grading actually ran.
    ``artifact_base`` is the runtime-only directory the sealed artifact
    paths resolve against (trajectory graders read the sealed canonical
    transcript from it); the store never persists it.
    ``adapter`` is the observed agent identity for this trial (which adapter
    actually produced it); absent means the record cannot say.
    """
    record = TrialRecord(
        trial_id=trial_id,
        coordinates=coordinates,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        baseline_ok=baseline_ok,
        requirements=progress.snapshot(),
        artifact_base=artifact_base,
        artifacts=dict(artifacts),
        transcript_extra=transcript_extra,
        versions=VersionsBundle(
            aeval_version=_dist_version("aeval-harbor"),
            grader_versions=dict(grader_versions),
        ),
        adapter=adapter,
    )
    # The enforcement point is a fact about this trial: where spend was actually
    # enforced. Usage numbers stay None unless measured — an invented cost is
    # worse than an unavailable one (reliability metrics skip such records).
    if adapter is not None:
        record.budget = BudgetSnapshot(enforcement_point=adapter.budget_enforcement)
    return record


def _note_requirement_shortfall(
    extra: dict[str, Any] | None, unmet: list[str]
) -> dict[str, Any]:
    """Record which required facts were unmet, under the record's audit extra.

    Merges rather than replaces: whatever the trial already recorded stays
    readable, and the shortfall is added beside it so a reader of the sealed
    record can see *why* the verdict is ``cannot_judge``.
    """
    merged: dict[str, Any] = dict(extra or {})
    audit = merged.get("aeval")
    audit = dict(audit) if isinstance(audit, dict) else {}
    audit["requirement_shortfall"] = list(unmet)
    merged["aeval"] = audit
    return merged


async def grade_and_record(
    *,
    suite: ResolvedSuite,
    trial_id: str,
    coordinates: TrialCoordinates,
    stop_reason: str,
    baseline_ok: bool,
    progress: RequirementProgress,
    evidence: EvidenceBundle,
    transcript_extra: dict[str, Any] | None,
    store: TrialStore,
    artifact_base: str | None = None,
    adapter: AdapterSpec | None = None,
) -> TrialRecord:
    """Grade one sealed trial and atomically persist record + grades.

    On a grader-side failure the trial is classified ``infra_invalid``
    with ``judge_finished`` unset and still persisted (an unrecorded
    failure is worse than a recorded invalid one). A store conflict
    propagates to the caller — the store stays clean, never half full.
    """
    try:
        graders = load_suite_graders(suite, coordinates.task_id)
    except (GraderLoadError, GradingPipelineError) as exc:
        record = build_trial_record(
            trial_id=trial_id,
            coordinates=coordinates,
            stop_reason="infra_error",
            baseline_ok=baseline_ok,
            progress=progress,
            artifacts=logical_artifacts(evidence),
            transcript_extra=transcript_extra,
            grader_versions={},
            artifact_base=artifact_base,
            adapter=adapter,
        )
        record.verdict = "infra_invalid"
        record.grades = []
        store.persist_trial_with_grades(record, [])
        raise GradingPipelineError(f"grader loading failed: {exc}") from exc

    record = build_trial_record(
        trial_id=trial_id,
        coordinates=coordinates,
        stop_reason=stop_reason,
        baseline_ok=baseline_ok,
        progress=progress,
        artifacts=logical_artifacts(evidence),
        transcript_extra=transcript_extra,
        grader_versions={g.grader.id: g.grader.version for g in graders},
        artifact_base=artifact_base,
        adapter=adapter,
    )

    try:
        results = await grade_trial(record, graders)
    except Exception as exc:
        # A crashing or lying grader is our infrastructure fault.
        record.stop_reason = "infra_error"
        record.verdict = "infra_invalid"
        record.grades = []
        store.persist_trial_with_grades(record, [])
        raise GradingPipelineError(f"grader execution failed: {exc}") from exc

    progress.mark_judging_finished()
    # The sealed bitmap: the stage bits accumulated during the trial, finalized
    # here (judge_finished can only be set by this pipeline, and an
    # ``infra_error`` stop clears it again).
    record.requirements = evaluate_requirements(
        evidence, staged=progress.snapshot(), stop_reason=stop_reason
    )
    record.grades = results
    record.verdict = decide_final_verdict(results)
    # Requirement gate. A suite declares in ``verdict.requirements`` which facts
    # must hold for its trials to be judgeable at all. An unmet one means the
    # outcome cannot be trusted, so the trial is recorded ``cannot_judge`` — an
    # explicit, reasoned exclusion — rather than silently counting as a pass or
    # a failure. Grades are kept: the qualifying evidence is not thrown away,
    # only the verdict it would have produced. ``infra_invalid`` is left alone
    # (the run itself is soured, which the seal reports separately).
    unmet = record.requirements.missing(tuple(suite.overlay.verdict.requirements))
    if unmet and record.verdict != "infra_invalid":
        record.verdict = "cannot_judge"
        record.transcript_extra = _note_requirement_shortfall(
            record.transcript_extra, unmet
        )
    try:
        store.persist_trial_with_grades(record, results)
    except StoreConflictError:
        raise
    return record
