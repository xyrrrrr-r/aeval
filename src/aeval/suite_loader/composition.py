from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tomllib
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence

import yaml
from harbor.models.dataset.manifest import DatasetManifest
from harbor.models.job.config import DatasetConfig, JobConfig
from harbor.models.task.task import Task
from harbor.models.trial.config import TaskConfig
from harbor.utils.env import is_env_template, is_sensitive_env_key
from pydantic import BaseModel, ValidationError

from aeval.suite_loader.loader import resolve_harbor_inputs
from aeval.suite_loader.paths import suite_path
from aeval.suite_loader.validation import (
    validate_harbor_job_shape,
    validate_task_provenance,
    validate_thin_overlay,
)
from aeval.suite_models import ResolvedSuite, SuiteError


def read_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".toml":
            value = tomllib.loads(text)
        elif path.suffix.lower() == ".json":
            value = json.loads(text)
        elif path.suffix.lower() in (".yaml", ".yml"):
            value = yaml.safe_load(text)
        else:
            raise SuiteError(f"Unsupported declaration format: {path}")
    except (OSError, ValueError, UnicodeError, yaml.YAMLError) as exc:
        raise SuiteError(f"Cannot read declaration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SuiteError(f"Declaration must be a mapping: {path}")
    return value


def _check_env_templates(value: Any) -> None:
    if isinstance(value, dict):
        env = value.get("env")
        if isinstance(env, list):
            raise SuiteError("environment.env must be a mapping with host environment templates")
        if isinstance(env, dict):
            for key, item in env.items():
                if is_sensitive_env_key(str(key)) and (not isinstance(item, str) or not is_env_template(item)):
                    raise SuiteError(f"Sensitive env {key!r} must use a host environment template")
        for item in value.values():
            _check_env_templates(item)
    elif isinstance(value, list):
        for item in value:
            _check_env_templates(item)


def _resolve_model_paths(model: BaseModel, root: Path) -> None:
    for field in type(model).model_fields:
        if field == "jobs_dir":
            continue
        value = getattr(model, field)
        if isinstance(value, Path):
            path = suite_path(root, value)
            if not path.exists():
                raise SuiteError(f"Referenced input does not exist: {path}")
            setattr(model, field, path)
        elif isinstance(value, BaseModel):
            _resolve_model_paths(value, root)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if field == "skills" and isinstance(item, str):
                    local = "://" not in item and (
                        item.startswith((".", "/", "\\"))
                        or PureWindowsPath(item).drive
                        or (root / item).exists()
                    )
                    if local:
                        path = suite_path(root, item)
                        if not path.is_dir() or not (path / "SKILL.md").is_file():
                            raise SuiteError(f"Local skill must contain SKILL.md: {path}")
                        for entry in path.rglob("*"):
                            suite_path(root, entry.relative_to(root))
                        value[index] = str(path)
                elif isinstance(item, Path):
                    path = suite_path(root, item)
                    if not path.exists():
                        raise SuiteError(f"Referenced input does not exist: {path}")
                    if field in ("extra_instruction_paths", "extra_docker_compose") and not path.is_file():
                        raise SuiteError(f"Referenced {field} input must be a file: {path}")
                    value[index] = path
                elif isinstance(item, BaseModel):
                    _resolve_model_paths(item, root)


def _validate_local_task(suite: ResolvedSuite, path: Path, job_data: dict[str, Any]) -> None:
    root = Path(suite.suite_dir).resolve()
    suite_path(root, path.relative_to(root))
    if not (path / "environment").is_dir():
        raise SuiteError(f"Native Harbor task is missing environment/: {path}")
    for entry in path.rglob("*"):
        suite_path(root, entry.relative_to(root))
    try:
        data = read_mapping(path / "task.toml")
        _check_env_templates(data)
        task = Task(path)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SuiteError(f"Invalid native Harbor task {path}: {exc}") from exc
    try:
        type(task.config).model_validate(deepcopy(data), extra="forbid")
    except ValidationError as exc:
        raise SuiteError(f"Invalid native Harbor task {path}: {exc}") from exc
    metadata = task.config.metadata
    aeval = metadata.get("aeval", {})
    if not isinstance(aeval, dict):
        raise SuiteError(f"task metadata.aeval must be a mapping: {path}")
    if "provenance" in aeval and "provenance" in metadata:
        raise SuiteError(f"Task provenance is declared twice: {path}")
    provenance = aeval.get("provenance", metadata.get("provenance"))
    if provenance is None:
        provenance = suite.overlay.provenance.model_dump()
    validate_task_provenance({"provenance": provenance}, path)
    # P0-2: reject unsupported sandbox semantics up front. e2b
    # sandboxes have no host bind mounts (capabilities.mounted is
    # False; logs are downloaded, not mounted), and verifier log
    # filters can silently drop required evidence.
    environment = data.get("environment") or {}
    if isinstance(environment, dict) and environment.get("mounts"):
        raise SuiteError(
            f"Task {path} declares [environment] mounts — the e2b backend "
            "has no host bind mounts; mount-based evidence cannot be "
            "collected, so the task is refused at suite validation"
        )

    validate_thin_overlay(suite, data, job_data)
    # P0-6: the task must declare [[verifier.collect]] commands for
    # every required evidence output — at suite time, before any run.
    from aeval.hooks.evidence import (
        EvidenceIntegrityError,
        build_required_collect_plan,
        validate_collect_declarations,
    )

    try:
        validate_collect_declarations(
            task.config.verifier.collect
            if getattr(task.config, "verifier", None) is not None else [],
            build_required_collect_plan(suite),
        )
    except EvidenceIntegrityError as exc:
        raise SuiteError(f"Task {path} fails the evidence collect plan: {exc}") from exc


def compose_harbor_job(suite: ResolvedSuite) -> JobConfig:
    root = Path(suite.suite_dir).resolve()
    inputs = resolve_harbor_inputs(suite)
    dataset_path = suite_path(root, inputs.dataset)
    job_path = suite_path(root, inputs.job)
    data = read_mapping(dataset_path)
    job_data = read_mapping(job_path)
    _check_env_templates(job_data)
    validate_harbor_job_shape(job_data, job_path)
    if any(key in job_data for key in ("tasks", "datasets", "source_jobs")):
        raise SuiteError("Task selection belongs in harbor.dataset, not also in the job declaration")
    agents = job_data.get("agents")
    if not isinstance(agents, list) or not agents or any(
        not isinstance(agent, dict)
        or not any(isinstance(agent.get(key), str) and agent[key].strip() for key in ("name", "import_path"))
        for agent in agents
    ):
        raise SuiteError("Harbor job must explicitly select at least one agent")
    job = JobConfig.model_validate(deepcopy(job_data), extra="forbid")
    # P0-2: verifier log filters can silently drop required evidence
    # logs — an evaluation job must collect the full verifier log set.
    if job.verifier.include_logs or job.verifier.exclude_logs:
        raise SuiteError(
            "job declares verifier include_logs/exclude_logs — log "
            "filters can silently drop required evidence logs"
        )
    if job.install_only or job.verifier.disable:
        raise SuiteError("Evaluation suites cannot disable verification or use install_only")
    if suite.overlay.image:
        raise SuiteError(
            "Image narrowing needs a published native task revision; this adapter cannot "
            "apply image.pin/rebuild at runtime. Pin the image in task.toml and remove the overlay action."
        )
    validate_thin_overlay(suite, {}, job_data)
    graders = suite.overlay.verdict.resolved_graders()
    if not graders:
        raise SuiteError("Suite must select at least one versioned grader")
    for grader in graders:
        reference, separator, version = grader.impl.rpartition("@")
        if not separator or not version or not reference.endswith(".py"):
            raise SuiteError(f"Grader must reference a versioned Python file: {grader.impl!r}")
        if not suite_path(root, reference).is_file():
            raise SuiteError(f"Grader implementation does not exist: {reference}")
    _resolve_model_paths(job, root)
    for agent in job.agents:
        if agent.load_trajectory:
            path = suite_path(root, agent.load_trajectory)
            if not path.is_file():
                raise SuiteError(f"Missing trajectory input: {path}")
            agent.load_trajectory = str(path)
    try:
        if "dataset" in data:
            manifest = DatasetManifest.model_validate(data, extra="forbid")
            if manifest.files:
                raise SuiteError("Dataset-level files require Harbor package resolution; use a named DatasetConfig")
            if not manifest.tasks:
                raise SuiteError("Dataset manifest contains no tasks")
            identities = [(t.name, t.digest) for t in manifest.tasks]
            if len(set(identities)) != len(identities):
                raise SuiteError("Dataset manifest contains duplicate task references")
            job.tasks = [
                TaskConfig(name=task.name, ref=task.digest, source=manifest.dataset.name)
                for task in manifest.tasks
            ]
        else:
            dataset = DatasetConfig.model_validate(deepcopy(data), extra="forbid")
            if dataset.n_tasks is not None and (type(data.get("n_tasks")) is not int or dataset.n_tasks <= 0):
                raise SuiteError("Dataset n_tasks must be a positive integer")
            if dataset.is_local():
                dataset.path = suite_path(root, dataset.path)
                if not dataset.path.is_dir():
                    raise SuiteError(f"Local dataset directory does not exist: {dataset.path}")
                for path in sorted(dataset.path.iterdir()):
                    if (path / "task.toml").exists():
                        _validate_local_task(suite, path, job_data)
                job.tasks = asyncio.run(dataset.get_task_configs())
                if not job.tasks:
                    raise SuiteError("Local dataset selects no valid native Harbor tasks")
            else:
                if dataset.registry_path is not None and not dataset.is_repo():
                    dataset.registry_path = suite_path(root, dataset.registry_path)
                    if not dataset.registry_path.exists():
                        raise SuiteError(f"Registry input does not exist: {dataset.registry_path}")
                if dataset.download_dir is not None:
                    raise SuiteError("Keep dataset download_dir out of the portable suite declaration")
                job.datasets = [dataset]
    except ValidationError as exc:
        raise SuiteError(f"Invalid native Harbor dataset {dataset_path}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise SuiteError(f"Cannot resolve native Harbor dataset {dataset_path}: {exc}") from exc
    suite.overlay.harbor = inputs
    return job


def suite_source_commit(suite_dir: Path, extra_paths: Sequence[Path] = ()) -> str:
    """The committed source of a run: the suite AND every extended base file.

    Inheritance means part of a suite's effective configuration can live
    outside its own directory, so the git evidence must cover the whole
    chain — an untracked or dirty base would otherwise be reported as a
    clean, committed source. All queries stay read-only.
    """

    suite_dir = Path(suite_dir).resolve()

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(suite_dir), *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
        except OSError as exc:
            raise SuiteError("Run provenance requires Git and a committed suite directory") from exc
        if result.returncode:
            raise SuiteError("Run provenance requires a committed suite directory; import/probe do not require Git")
        return result.stdout.strip()

    # Pathspecs are resolved against the suite directory, so a base beside it
    # is named `../_base/<name>.base.yaml` — still inside the repository, and
    # anything outside it fails the tracked-file check below.
    extra = [os.path.relpath(Path(path).resolve(), suite_dir).replace(os.sep, "/") for path in extra_paths]
    git("ls-files", "--error-unmatch", "--", "suite.yaml", *extra)
    if git("status", "--porcelain", "--untracked-files=normal", "--", ".", *extra):
        raise SuiteError(
            "Suite or an extended base has uncommitted changes; commit the version before running"
        )
    if git("ls-files", "--others", "--ignored", "--exclude-standard", "--", "."):
        raise SuiteError("Suite contains ignored files absent from its source commit")
    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SuiteError("Suite source must resolve to a full 40-character Git commit")
    return commit
