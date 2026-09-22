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

from pathlib import Path
from typing import Any

import yaml

from aeval.suite_models import (
    HARBOR_OWNED_TASK_KEYS,
    ProvenanceInfo,
    ResolvedSuite,
    SuiteError,
    load_suite_yaml,
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
    docker_image = task_data.get("docker_image")
    if isinstance(docker_image, str):
        images["task"] = docker_image
    env = task_data.get("environment")
    if isinstance(env, dict):
        img = env.get("image")
        if isinstance(img, str):
            images.setdefault("environment", img)
    verifier = task_data.get("verifier")
    if isinstance(verifier, dict):
        img = verifier.get("image")
        if isinstance(img, str):
            images.setdefault("verifier", img)
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
    """Minimal shape check on the referenced Harbor JobConfig.

    We do not re-validate Harbor's own schema (Harbor does that); we
    only assert the two facts aeval's denominator discipline needs:
    attempts (k) and concurrent trials are declared.
    """
    where = f" ({source})" if source else ""
    n_attempts = job_data.get("n_attempts") or (
        job_data.get("job", {}) or {}
    ).get("n_attempts")
    n_concurrent = job_data.get("n_concurrent_trials") or (
        job_data.get("job", {}) or {}
    ).get("n_concurrent_trials")
    if n_attempts is None:
        raise SuiteError(
            f"Harbor job{where} does not declare n_attempts (our k) — "
            "pass@k cannot be computed without it"
        )
    if n_concurrent is None:
        raise SuiteError(
            f"Harbor job{where} does not declare n_concurrent_trials — "
            "trial isolation policy is undeclared"
        )
