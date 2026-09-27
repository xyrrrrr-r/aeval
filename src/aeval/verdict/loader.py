"""Versioned grader loading (P0-7).

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
from aeval.verdict.base import Grader, ResolvedGrader

__all__ = ["GraderLoadError", "LoadedModuleGrader", "load_grader", "split_impl"]


class GraderLoadError(RuntimeError):
    """The grader module violates the loading contract — never executed."""


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
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise GraderLoadError(f"grader module raised during import: {exc}") from exc
    finally:
        sys.modules.pop(module_name, None)
    return module


def _required_text(module: Any, attribute: str) -> str:
    value = getattr(module, attribute, None)
    if not isinstance(value, str) or not value.strip():
        raise GraderLoadError(
            f"grader module must declare a non-empty string {attribute}"
        )
    return value.strip()


def load_grader(reference: Path, declared: GraderDeclaration) -> ResolvedGrader:
    """Load and verify one declared grader.

    The declaration is authoritative for the version and layer; the module
    must agree. The returned ``Grader`` protocol object carries the module
    identity, so a result from another grader can never be attributed to
    this one (checked again by :func:`aeval.verdict.executor.grade_trial`).
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

    class _ModuleGrader:
        # Protocol view over the verified module identity.
        id = loaded.grader_id
        version = loaded.grader_version
        layer = loaded.layer  # type: ignore[assignment]

        async def grade(self, record: TrialRecord) -> GradeResult:
            return await loaded.call(record)

    return ResolvedGrader(
        grader=_ModuleGrader(),  # type: ignore[arg-type]
        execution="pure",
        veto=declared.veto,
        requires_fields=list(required_fields),
    )
