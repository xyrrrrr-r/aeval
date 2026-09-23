"""Thin-overlay validation (plan §1): refuse drift before it becomes a score.

Rules:
1. Restating a Harbor-owned fact → error (no precedence merging).
2. Only image.pin / image.rebuild narrowing may touch Harbor facts,
   and only in the tightening direction (tag → digest, rebuild).
3. Mandatory overlay sections must be present: baselines, clock,
   observables, verdict, metrics, driver, provenance.
4. Task provenance: no license → no imported data.
5. Requirement names must be a subset of the fixed six.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from harbor.models.job.config import JobConfig
from pydantic import ValidationError

from aeval.suite_models import (
    ProvenanceInfo,
    ResolvedSuite,
    SuiteError,
)

from aeval.contracts import REQUIREMENT_FIELDS

__all__ = [
    "validate_thin_overlay",
    "validate_task_provenance",
    "validate_harbor_job_shape",
]


def validate_thin_overlay(
    suite: ResolvedSuite,
    harbor_task_data: dict[str, Any],
    harbor_job_data: dict[str, Any],
) -> None:
    """Cross-check the overlay against the Harbor files it references."""
    overlay = suite.overlay

    # Rule 1: mandatory sections were validated by the pydantic model
    # (min_length on baselines/observables, verdict required, etc.).
    # Here we check the deeper semantic rules.

    # Rule 3: requirement names must be the fixed six.
    unknown = [r for r in overlay.verdict.requirements if r not in REQUIREMENT_FIELDS]
    if unknown:
        raise SuiteError(
            f"{suite.suite_dir}: verdict.requirements contains unknown "
            f"requirements {unknown}; allowed: {list(REQUIREMENT_FIELDS)}"
        )

    # Rule 2: image narrowing must actually narrow something Harbor
    # declares, and pin must be a digest (model already enforces digest).
    task_images = _harbor_task_images(harbor_task_data)
    for name, action in overlay.image.items():
        if action.pin is not None and action.rebuild:
            raise SuiteError(
                f"{suite.suite_dir}: image.{name} sets both pin and rebuild — "
                "choose one narrowing action"
            )
        if name not in task_images:
            raise SuiteError(
                f"{suite.suite_dir}: image.{name} narrows an image that the "
                f"Harbor task does not declare (declared: {sorted(task_images)})"
            )
        if action.pin is not None and task_images[name] == action.pin:
            raise SuiteError(
                f"{suite.suite_dir}: image.{name}.pin equals the Harbor value "
                f"{action.pin!r} — not a narrowing, a restatement"
            )

    # A job file that declares our overlay facts is an overlap too.
    for key in ("baselines", "clock", "observables", "verdict"):
        if key in harbor_job_data:
            raise SuiteError(
                f"{suite.suite_dir}: Harbor job file restates aeval-owned "
                f"fact {key!r} — aeval facts live in suite.yaml only"
            )


def _harbor_task_images(task_data: dict[str, Any]) -> dict[str, str]:
    images: dict[str, str] = {}
    env = task_data.get("environment")
    if isinstance(env, dict):
        img = env.get("docker_image")
        if isinstance(img, str):
            images["environment"] = img
    verifier = task_data.get("verifier")
    if isinstance(verifier, dict):
        verifier_env = verifier.get("environment")
        if isinstance(verifier_env, dict):
            img = verifier_env.get("docker_image")
            if isinstance(img, str):
                images["verifier"] = img
    return images


def validate_task_provenance(task_data: dict[str, Any], source: Path | str = "") -> None:
    """Every imported task must carry a provenance block (plan §9.5).

    A missing provenance block is a P0 failure — retrofitting it later
    leaves historical tasks with unknown origin.
    """
    where = f" ({source})" if source else ""
    prov = task_data.get("provenance")
    if not isinstance(prov, dict):
        raise SuiteError(
            f"task{where} has no provenance block — imported tasks must "
            "declare source/license; authored tasks write "
            "provenance: {source: authored-internally, license: MIT}"
        )
    try:
        info = ProvenanceInfo.model_validate(prov)
    except SuiteError:
        raise
    except Exception as exc:
        raise SuiteError(f"task{where}: invalid provenance: {exc}") from exc
    if info.license in ("NONE_DECLARED", "UNKNOWN") and info.data_imported:
        raise SuiteError(
            f"task{where}: license {info.license} forbids data_imported=true — "
            "import the format skeleton only and rewrite the data yourself"
        )


def validate_harbor_job_shape(job_data: dict[str, Any], source: Path | str = "") -> None:
    """Validate a native JobConfig with explicit positive trial counts.

    Harbor defaults must not hide undeclared denominator or isolation
    policy, and unknown fields must not silently discard a user's intent.
    """
    where = f" ({source})" if source else ""
    if not isinstance(job_data, dict):
        raise SuiteError(f"Harbor job{where} must be a mapping")
    unknown = set(job_data) - set(JobConfig.model_fields)
    if unknown:
        raise SuiteError(f"Harbor job{where} contains unknown job keys: {sorted(unknown, key=str)}")
    for field in ("n_attempts", "n_concurrent_trials"):
        if field not in job_data:
            raise SuiteError(f"Harbor job{where} does not declare {field}")
        value = job_data[field]
        if type(value) is not int or value <= 0:
            raise SuiteError(f"Harbor job{where}: {field} must be a positive integer")
    try:
        # Native migrations may mutate nested dictionaries; keep validation read-only.
        JobConfig.model_validate(deepcopy(job_data), extra="forbid")
    except (ValidationError, TypeError) as exc:
        raise SuiteError(f"Harbor job{where}: invalid JobConfig: {exc}") from exc
