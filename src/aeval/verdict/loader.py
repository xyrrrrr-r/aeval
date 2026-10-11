"""Versioned grader loading.

A suite declares graders as ``<path>.py@<version>``. Loading is a
fail-closed contract check, not an import-and-hope: the module must
declare its own identity, the declared version must match, and the
entry point must be a coroutine. A grader that lies about who it is
never executes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aeval.contracts import GradeResult, TrialRecord
from aeval.suite_models import GraderDeclaration
from aeval.verdict.base import Grader, GraderLoadError, ResolvedGrader
from aeval.verdict.trajectory.assembly import (
    DeclaredGuardPlan,
    DeclaredMetricPlan,
    build_declared_family_grader,
    build_declared_guard_grader,
    metric_registry,
)

__all__ = [
    "GraderLoadError",
    "LoadedModuleGrader",
    "load_grader",
    "load_grader_module",
    "split_impl",
]


def load_grader_module(
    reference: Path, declared: GraderDeclaration
) -> Any:
    """Load and verify a grader module, returning the module itself.

    轨迹分析（turn 切面）需要的不止 ``grade``——套件模块可能声明
    ``turn_metrics(task_id)`` 之类的分析入口。身份校验与
    :func:`load_grader` 完全一致（模块必须自证 id/version），只是
    返回模块对象而非协议包装。

    The pinned version is authoritative: ``GraderDeclaration.version``
    when explicitly set, otherwise the ``@version`` suffix of the impl
    reference (``FamilyEngine`` declarations carry no separate field —
    the pin lives in the impl string). A module that self-identifies
    with any other version never loads.
    """
    reference = Path(reference)
    module = _load_module(reference)
    grader_id = _required_text(module, "GRADER_ID")
    grader_version = _required_text(module, "GRADER_VERSION")
    pinned = getattr(declared, "version", None)
    if pinned is None:
        _, pinned = split_impl(declared.impl)
    if grader_version != pinned:
        raise GraderLoadError(
            f"grader module {grader_id} declares version {grader_version!r} "
            f"but the suite pinned {pinned!r}"
        )
    return module


# impl references are ``<relative path>.py@<version>``
_IMPL_PATTERN = re.compile(r"^(?P<path>.+\.py)@(?P<version>[A-Za-z0-9][\w.-]*)$")

_ALLOWED_LAYERS = ("outcome", "trajectory", "both")


def split_impl(impl: str) -> tuple[str, str]:
    """Split ``path.py@version``; raise ``GraderLoadError`` on any other shape."""
    match = _IMPL_PATTERN.fullmatch(impl.strip())
    if not match:
        raise GraderLoadError(
            f"grader impl must be '<path>.py@<version>': {impl!r}"
        )
    return match.group("path"), match.group("version")


@dataclass(frozen=True)
class LoadedModuleGrader:
    """A module-level grader bound to its verified identity."""

    grader_id: str
    grader_version: str
    layer: str
    required_fields: tuple[str, ...]
    grade: Callable[[TrialRecord], Any]

    async def call(self, record: TrialRecord) -> GradeResult:
        return await self.grade(record)  # type: ignore[misc]


def _load_module(reference: Path) -> Any:
    if not reference.is_file():
        raise GraderLoadError(f"grader file does not exist: {reference}")
    module_name = f"aeval_grader_{abs(hash(str(reference)))}_{reference.stem}"
    spec = importlib.util.spec_from_file_location(module_name, reference)
    if spec is None or spec.loader is None:
        raise GraderLoadError(f"grader file is not importable: {reference}")
    module = importlib.util.module_from_spec(spec)
    # Grader modules are plain data-in/data-out implementations; they must
    # not register themselves anywhere global.
    sys.modules[module_name] = module
    # Importing a grader must not write into the suite: ``__pycache__`` beside
    # graders/ turns a sealed suite into a dirty one, and the next run is then
    # refused by the provenance gate ("Suite ... has uncommitted changes") —
    # found on the real target host by the first real run of the generic facade
    # flavor, because grading the trial is what created the directory. The flag
    # is process-wide, so it is restored immediately after this one import.
    cache_flag = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    except Exception as exc:
        raise GraderLoadError(f"grader module raised during import: {exc}") from exc
    finally:
        sys.dont_write_bytecode = cache_flag
        sys.modules.pop(module_name, None)
    return module


def _required_text(module: Any, attribute: str) -> str:
    value = getattr(module, attribute, None)
    if not isinstance(value, str) or not value.strip():
        raise GraderLoadError(
            f"grader module must declare a non-empty string {attribute}"
        )
    return value.strip()


def load_grader(
    reference: Path,
    declared: GraderDeclaration,
    *,
    family_key: str | None = None,
    family_spec: Any | None = None,
) -> ResolvedGrader:
    """Load and verify one declared grader.

    The declaration is authoritative for the version and layer; the module
    must agree. The returned ``Grader`` protocol object carries the module
    identity, so a result from another grader can never be attributed to
    this one (checked again by :func:`aeval.verdict.executor.grade_trial`).

    ``family_key`` (gradeCategory v4) is the family this load is FOR: the
    wrapper injects it into the record as ``grade_bindings[grader_id]``
    right before ``grade`` runs, so a family engine can read its own
    anchors from the sealed copy (``{task_id: {family: anchors}}``).
    v5: a module exposing ``METRICS`` is a metric LIBRARY, not a family
    engine — the grader is built from the declaration's metric plan
    (``family_spec.metrics``, judging order) plus the module's
    ``id → factory`` registry, so the module keeps no second copy of the
    metric list, the threshold or the veto flag. ``family_spec`` is
    mandatory on that path (fail-closed).
    """
    reference = Path(reference)
    module = _load_module(reference)

    grader_id = _required_text(module, "GRADER_ID")
    grader_version = _required_text(module, "GRADER_VERSION")
    module_layer = getattr(module, "LAYER", None)
    if module_layer is not None and module_layer != declared.layer:
        raise GraderLoadError(
            f"grader {grader_id}@{grader_version} declares layer "
            f"{module_layer!r} but the suite declared {declared.layer!r}"
        )
    if declared.layer not in _ALLOWED_LAYERS:
        raise GraderLoadError(f"grader layer must be one of {_ALLOWED_LAYERS}")

    if family_spec is not None and family_spec.metrics:
        return _load_declared_family_grader(
            module,
            declared,
            grader_id=grader_id,
            grader_version=grader_version,
            family_key=family_key,
            family_spec=family_spec,
        )

    # v5 A：守卫声明了 trace 指标清单 → 走声明驱动装配（模块须是指标
    # 库）。只声明 impl/layer/veto 的守卫保持 legacy 路径，逐字节兼容。
    if getattr(declared, "metrics", None) and family_spec is None:
        return _load_declared_guard_grader(
            module,
            declared,
            grader_id=grader_id,
            grader_version=grader_version,
        )

    required = getattr(module, "REQUIRED_FIELDS", None)
    if required is None:
        required_fields: tuple[str, ...] = ()
    elif (
        isinstance(required, (list, tuple))
        and all(isinstance(f, str) and f.strip() for f in required)
    ):
        required_fields = tuple(required)
    else:
        raise GraderLoadError(
            "grader REQUIRED_FIELDS must be a list of non-empty strings"
        )

    grade = getattr(module, "grade", None)
    if not asyncio.iscoroutinefunction(grade):
        raise GraderLoadError(
            f"grader {grader_id}@{grader_version} must define 'async def grade(record)'"
        )

    loaded = LoadedModuleGrader(
        grader_id=grader_id,
        grader_version=grader_version,
        layer=declared.layer,
        required_fields=required_fields,
        grade=grade,
    )

    # v5：模块不再声明锚点词汇表/解析器——inputs 由声明逐指标绑定，
    # 引擎没有锚点契约面（锚点通道是套件级 verdict.anchors: task_anchors）。
    _module_family_key = family_key

    class _ModuleGrader:
        # Protocol view over the verified module identity.
        id = loaded.grader_id
        version = loaded.grader_version
        layer = loaded.layer  # type: ignore[assignment]
        family_key = _module_family_key

        async def grade(self, record: TrialRecord) -> GradeResult:
            if _module_family_key is not None:
                # Runtime-only binding: which family this engine instance
                # is judging FOR. Copied per call — the sealed record the
                # pipeline persists never carries it.
                record = record.model_copy(
                    update={
                        "grade_bindings": {
                            **(record.grade_bindings or {}),
                            loaded.grader_id: _module_family_key,
                        }
                    }
                )
            return await loaded.call(record)

    return ResolvedGrader(
        grader=_ModuleGrader(),  # type: ignore[arg-type]
        execution="pure",
        veto=declared.veto,
        requires_fields=list(required_fields),
    )

def _load_declared_family_grader(
    module: Any,
    declared: GraderDeclaration,
    *,
    grader_id: str,
    grader_version: str,
    family_key: str | None,
    family_spec: Any | None,
) -> ResolvedGrader:
    """Assembly for a metric-library module (v5 declaration-driven path).

    The family declaration is the truth: its metric list (judging order),
    threshold and veto flag drive the built grader. A family load without
    a spec is refused — the assembler must never guess which metrics to
    judge.
    """
    registry = metric_registry(module)
    if registry is None:
        raise GraderLoadError(
            f"{grader_id}@{grader_version} is declared as a family engine but "
            "exposes no METRICS registry — a family's metrics are declared, so "
            "the engine must be a metric library (there is no second, coded "
            "metric list any more)"
        )
    plan = DeclaredMetricPlan(
        grader_id=grader_id,
        grader_version=grader_version,
        layer=declared.layer,
        threshold=float(family_spec.threshold),
        veto=bool(declared.veto),
        metric_ids=tuple(metric.id for metric in family_spec.metrics),
        declared_severities={
            metric.id: metric.severity for metric in family_spec.metrics
        },
        declared_inputs={
            metric.id: (next(iter(metric.inputs.values())) if metric.inputs else None)
            for metric in family_spec.metrics
        },
        family_key=family_key or family_spec.name,
        metric_specs=tuple(family_spec.metrics),
        consumes_inputs=any(
            bool(metric.inputs) for metric in family_spec.metrics
        ),
    )
    return build_declared_family_grader(module, plan)

def _load_declared_guard_grader(
    module: Any,
    declared: GraderDeclaration,
    *,
    grader_id: str,
    grader_version: str,
) -> ResolvedGrader:
    """Assembly for a guard that declares its trace metric list (v5 A).

    声明的清单是判分真源：按声明序逐 id 从模块的 ``METRICS`` 取工厂，
    ``severity`` 与实现 ``category`` 对表；``veto`` 仍来自声明（守卫的
    门是套件策略，不是模块常量）。守卫不吃逐任务输入、不设阈值。
    """
    declared_metrics = list(getattr(declared, "metrics", None) or [])
    plan = DeclaredGuardPlan(
        grader_id=grader_id,
        grader_version=grader_version,
        layer=declared.layer,
        veto=bool(declared.veto),
        metric_ids=tuple(metric.id for metric in declared_metrics),
        declared_severities={
            metric.id: metric.severity for metric in declared_metrics
        },
        guard_key=declared.impl,
    )
    return build_declared_guard_grader(module, plan)

