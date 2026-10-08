"""Offline regression coverage for composing portable, Harbor-native suites."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import socket
import subprocess
from unittest.mock import AsyncMock, Mock

from harbor.models.dataset.manifest import DatasetManifest
from harbor.models.job.config import DatasetConfig, JobConfig
from harbor.models.task.task import Task
from harbor.models.trial.config import TaskConfig
import pytest
import yaml

from aeval.suite_loader import composition
from aeval.suite_loader.composition import (
    compose_harbor_job,
    suite_path,
    suite_source_commit,
)
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError


DATASET = "declarations/dataset.yaml"
JOB = "declarations/job.yaml"
COMMIT = "0123456789abcdef" * 2 + "01234567"
DIGEST = "sha256:" + "a" * 64


def _write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _update_yaml(path: Path, **changes) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    _write_yaml(path, data)


def _write_task(root: Path, name: str = "example") -> Path:
    task = root / "tasks" / name
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n'
        '[environment]\ndocker_image = "alpine:3.20"\n'
        "[[verifier.collect]]\n"
        'command = "aeval-collect runtime_dump mock_call_log dsh_session canonical_transcript"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Return the example result.\n", encoding="utf-8")
    (task / "tests" / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert Task.is_valid_dir(task), "The fixture must be a real native Harbor task"
    return task


@pytest.fixture
def native_suite(tmp_path: Path) -> Path:
    root = tmp_path / "suite"
    _write_yaml(
        root / "suite.yaml",
        {
            "schema_version": 2,
            "id": "composition-regression",
            "version": "1.0.0",
            "harbor": {"dataset": DATASET, "job": JOB},
            "baselines": [{"id": "ready", "assert": "ready == True"}],
            "clock": {"mode": "real"},
            "observables": [{"name": "ready", "type": "boolean", "source": "file:ready.json"}],
            "verdict": {
                "requirements": ["input_complete"],
                "graders": {"default": {"impl": "graders/outcome.py@v1"}},
            },
            "metrics": [],
            "driver": {"require": []},
            "provenance": {"source": "authored-internally", "license": "MIT"},
        },
    )
    _write_yaml(root / DATASET, {"path": "tasks"})
    _write_yaml(
        root / JOB,
        {"n_attempts": 2, "n_concurrent_trials": 1, "agents": [{"name": "nop"}]},
    )
    (root / "graders").mkdir()
    (root / "graders" / "outcome.py").write_text('VERSION = "v1"\n', encoding="utf-8")
    _write_task(root)
    return root


@pytest.fixture
def no_remote_resolution(monkeypatch):
    """Fail before a remote resolver, subprocess, or network connection can run."""
    resolve = AsyncMock(side_effect=AssertionError("Composition must not resolve remote tasks"))
    network = Mock(side_effect=AssertionError("Composition must not use the network"))
    process = Mock(side_effect=AssertionError("Composition must not start subprocesses"))
    monkeypatch.setattr(DatasetConfig, "get_task_configs", resolve)
    monkeypatch.setattr(socket, "create_connection", network)
    monkeypatch.setattr(socket.socket, "connect", network)
    monkeypatch.setattr(socket, "getaddrinfo", network)
    monkeypatch.setattr(subprocess, "run", process)
    yield
    resolve.assert_not_called()
    network.assert_not_called()
    process.assert_not_called()


def _use_manifest(root: Path, text: str | None = None) -> Path:
    path = root / "declarations" / "dataset.toml"
    if text is None:
        text = (
            'schema_version = "1.0"\n'
            '[dataset]\nname = "regression/examples"\nversion = "1.0.0"\n'
            f'[[tasks]]\nname = "regression/example"\ndigest = "{DIGEST}"\n'
            f'[[tasks]]\nname = "regression/second"\ndigest = "sha256:{"b" * 64}"\n'
        )
    path.write_text(text, encoding="utf-8")
    _update_yaml(root / "suite.yaml", harbor={"dataset": "declarations/dataset.toml", "job": JOB})
    return path


def _snapshot(root: Path) -> dict:
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        if path.is_file() else None
        for path in root.rglob("*")
    }


def test_local_dataset_is_relative_to_suite_root_not_declaration_directory(native_suite):
    # A valid decoy makes resolving against declarations/ observably wrong.
    _write_task(native_suite / "declarations", "decoy")
    job = compose_harbor_job(load_suite(native_suite))
    assert isinstance(job, JobConfig)
    assert all(isinstance(task, TaskConfig) for task in job.tasks)
    assert [task.path for task in job.tasks] == [native_suite / "tasks" / "example"]
    assert job.tasks[0].source == "tasks"
    assert job.datasets == []
    assert job.n_attempts == 2
    assert job.n_concurrent_trials == 1
    assert [agent.name for agent in job.agents] == ["nop"]
    assert job.jobs_dir == Path("jobs")  # Output paths are not suite inputs.


def test_lease_model_is_stated_in_the_composed_agent_entry(native_suite):
    """The run's model is composed into the agent entry, once, for every adapter.

    Harbor passes it to the agent as ``model_name``; an adapter whose CLI would
    otherwise choose its own default turns it into a launch argument (the first
    real run of the generic facade flavor talked the Responses API to a facade
    that serves chat completions because nobody stated the model). Composing it
    here keeps the statement in one place instead of one job file per pairing.
    """
    plain = compose_harbor_job(load_suite(native_suite))
    assert all(agent.model_name is None for agent in plain.agents)

    job = compose_harbor_job(
        load_suite(native_suite), lease_model="deepseek/deepseek-chat"
    )
    assert [agent.model_name for agent in job.agents] == ["deepseek/deepseek-chat"]


def test_lease_model_name_reads_the_broker_identity():
    from types import SimpleNamespace

    from aeval.suite_loader.composition import lease_model_name

    assert (
        lease_model_name(SimpleNamespace(identity={"provider": "deepseek", "model": "deepseek-chat"}))
        == "deepseek/deepseek-chat"
    )
    assert lease_model_name(SimpleNamespace(identity={"model": "deepseek-chat"})) == "deepseek-chat"
    # nothing to state: no spec, no identity, no model
    assert lease_model_name(None) is None
    assert lease_model_name(SimpleNamespace(identity=None)) is None
    assert lease_model_name(SimpleNamespace(identity={"model": "  "})) is None


@pytest.mark.parametrize(
    "selection, expected",
    [
        ({"task_names": ["example", "keep-*"]}, {"example", "keep-one", "keep-two"}),
        ({"task_names": ["keep-*"], "exclude_task_names": ["*-two"]}, {"keep-one"}),
        ({"exclude_task_names": ["keep-*", "other"]}, {"example"}),
        ({"task_names": ["example", "exam*"]}, {"example"}),
    ],
)
def test_native_dataset_filters_and_exclusions(native_suite, selection, expected):
    for name in ("keep-one", "keep-two", "other"):
        _write_task(native_suite, name)
    _update_yaml(native_suite / DATASET, **selection)
    job = compose_harbor_job(load_suite(native_suite))
    assert {task.path.name for task in job.tasks} == expected
    assert len(job.tasks) == len(expected)


def test_native_task_limit_is_applied_after_filtering(native_suite):
    for name in ("keep-one", "keep-two", "other"):
        _write_task(native_suite, name)
    _update_yaml(native_suite / DATASET, task_names=["keep-*"], n_tasks=1)
    job = compose_harbor_job(load_suite(native_suite))
    assert len(job.tasks) == 1
    # Harbor uses filesystem order; do not assume which matching task is first.
    assert job.tasks[0].path.name in {"keep-one", "keep-two"}


@pytest.mark.parametrize("selection", [{"task_names": ["absent-*"]}, {"exclude_task_names": ["*"]}])
def test_empty_native_selection_is_rejected(native_suite, selection):
    _update_yaml(native_suite / DATASET, **selection)
    with pytest.raises(SuiteError, match="[Nn]o tasks|no valid native"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("empty_child", [False, True])
def test_empty_dataset_or_empty_task_directory_is_rejected(native_suite, empty_child):
    dataset = native_suite / "empty-tasks"
    dataset.mkdir()
    if empty_child:
        (dataset / "empty-example").mkdir()
    _write_yaml(native_suite / DATASET, {"path": "empty-tasks"})
    with pytest.raises(SuiteError, match="no valid native"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize(
    "missing, message",
    [("environment", "missing environment/"), ("instruction.md", "missing instruction.md"),
     ("tests/test.sh", "does not contain"), ("task.toml", "no valid native")],
)
def test_incomplete_native_task_is_rejected(native_suite, missing, message):
    path = native_suite / "tasks" / "example" / missing
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    with pytest.raises(SuiteError, match=message):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize(
    "text, message",
    [
        ("[environment\n", "Invalid native Harbor task"),
        ('[environment]\nos = "not-an-os"\n', "Invalid native Harbor task"),
        ('[environment]\ncpus = "not-an-integer"\n', "Invalid native Harbor task"),
        ('[metadata]\naeval = "not-a-mapping"\n', "metadata.aeval must be a mapping"),
    ],
)
def test_invalid_native_task_is_rejected_even_beside_a_valid_task(native_suite, text, message):
    invalid = _write_task(native_suite, "invalid")
    (invalid / "task.toml").write_text(text, encoding="utf-8")
    with pytest.raises(SuiteError, match=message):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize(
    "selection",
    [
        {"tasks": [{"path": "tasks/example"}]},
        {"datasets": [{"path": "tasks"}]},
        {"source_jobs": [{"action": "regrade", "type": "local", "path": "previous"}]},
        {"tasks": []},
        {"datasets": []},
        {"source_jobs": []},
    ],
)
def test_job_cannot_declare_a_second_task_selection(native_suite, selection):
    _update_yaml(native_suite / JOB, **selection)
    with pytest.raises(SuiteError, match="Task selection belongs in harbor.dataset"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("field", ["n_attempts", "n_concurrent_trials"])
@pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "2"])
def test_trial_counts_require_positive_integers(native_suite, field, value):
    _update_yaml(native_suite / JOB, **{field: value})
    with pytest.raises(SuiteError, match=field):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("field", ["n_attempts", "n_concurrent_trials"])
def test_trial_counts_must_be_explicit(native_suite, field):
    data = yaml.safe_load((native_suite / JOB).read_text(encoding="utf-8"))
    del data[field]
    _write_yaml(native_suite / JOB, data)
    with pytest.raises(SuiteError, match=field):
        compose_harbor_job(load_suite(native_suite))


def test_nondefault_attempts_and_concurrency_are_preserved(native_suite):
    _update_yaml(native_suite / JOB, n_attempts=7, n_concurrent_trials=3)
    job = compose_harbor_job(load_suite(native_suite))
    assert (job.n_attempts, job.n_concurrent_trials) == (7, 3)
    assert len(job.tasks) == 1  # Do not expand attempts into duplicate task entries.


@pytest.mark.parametrize("options", [{"install_only": True}, {"verifier": {"disable": True}}])
def test_evaluation_cannot_silently_skip_verification(native_suite, options):
    _update_yaml(native_suite / JOB, **options)
    with pytest.raises(SuiteError, match="disable verification|install_only"):
        compose_harbor_job(load_suite(native_suite))


def test_manifest_pinned_package_refs_become_tasks_without_networking(native_suite, no_remote_resolution):
    path = _use_manifest(native_suite)
    manifest = DatasetManifest.from_toml_file(path)
    job = compose_harbor_job(load_suite(native_suite))
    assert isinstance(job, JobConfig)
    assert job.datasets == []
    assert [(task.name, task.ref, task.source) for task in job.tasks] == [
        (task.name, task.digest, manifest.dataset.name) for task in manifest.tasks
    ]
    assert all(task.path is None and task.is_package_task() for task in job.tasks)
    assert (job.n_attempts, job.n_concurrent_trials) == (2, 1)


@pytest.mark.parametrize(
    "text, message",
    [
        ('[dataset]\nname = "regression/empty"\n', "no tasks"),
        (
            '[dataset]\nname = "regression/duplicate"\n'
            + f'[[tasks]]\nname = "regression/example"\ndigest = "{DIGEST}"\n' * 2,
            "duplicate task references",
        ),
        (
            '[dataset]\nname = "regression/files"\n'
            f'[[tasks]]\nname = "regression/example"\ndigest = "{DIGEST}"\n'
            '[[files]]\npath = "metric.py"\n',
            "Dataset-level files",
        ),
    ],
)
def test_manifest_cannot_silently_drop_or_duplicate_inputs(native_suite, no_remote_resolution, text, message):
    _use_manifest(native_suite, text)
    with pytest.raises(SuiteError, match=message):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize(
    "dataset",
    [
        {"name": "regression/examples", "ref": DIGEST, "task_names": ["regression/*"], "n_tasks": 2},
        {"name": "examples", "version": "1.0", "registry_url": "https://registry.invalid/index.json"},
        {"repo": "https://git.invalid/examples.git", "path": "tasks", "version": COMMIT},
        {"repo": "https://git.invalid/examples.git", "name": "examples", "registry_path": "registry.json"},
    ],
)
def test_remote_dataset_remains_delegated_without_networking(native_suite, no_remote_resolution, dataset):
    _write_yaml(native_suite / DATASET, dataset)
    expected = DatasetConfig.model_validate(deepcopy(dataset), extra="forbid")
    job = compose_harbor_job(load_suite(native_suite))
    assert job.tasks == []
    assert len(job.datasets) == 1
    assert isinstance(job.datasets[0], DatasetConfig)
    assert job.datasets[0] == expected


@pytest.mark.parametrize("n_tasks", [0, -1, True, 1.5, "2"])
def test_dataset_task_limit_is_a_positive_integer(native_suite, n_tasks):
    _update_yaml(native_suite / DATASET, n_tasks=n_tasks)
    with pytest.raises(SuiteError, match="n_tasks"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize(
    "field, data",
    [
        ("dataset", {}),
        ("dataset", {"path": "tasks", "typo": True}),
        ("dataset", {"path": "tasks", "name": "conflicting-source"}),
        ("dataset", {"path": "tasks", "task_names": "not-a-list"}),
        ("dataset", {"name": "regression/examples", "version": "1", "ref": DIGEST}),
        ("dataset", {"tasks": [{"path": "tasks/example"}]}),
        ("dataset", {"dataset": {"name": "regression/examples", "typo": True}}),
        ("dataset", {"dataset": {"name": "regression/examples"}, "tasks": [{"name": "bad", "digest": DIGEST}]}),
        ("dataset", {"dataset": {"name": "regression/examples"}, "tasks": [{"name": "regression/example", "digest": "latest"}]}),
        ("dataset", {"dataset": {"name": "regression/examples"}, "tasks": [{"name": "regression/example", "digest": DIGEST, "typo": True}]}),
        ("job", {"n_attempts": 2, "n_concurrent_trials": 1, "typo": True}),
        ("job", {"n_attempts": 2, "n_concurrent_trials": 1, "agents": [{"name": "nop", "typo": True}]}),
        ("job", {"n_attempts": 2, "n_concurrent_trials": 1, "retry": {"max_retrys": 2}}),
        ("job", {"n_attempts": 2, "n_concurrent_trials": 1, "environment": {"type": "not-a-provider"}}),
    ],
)
def test_unknown_or_invalid_native_declaration_schema_is_rejected(native_suite, field, data):
    _write_yaml(native_suite / (DATASET if field == "dataset" else JOB), data)
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("field", ["dataset", "job"])
@pytest.mark.parametrize(
    "suffix, text",
    [("yaml", "value: ["), ("json", '{"value":'), ("toml", "value = ["),
     ("yaml", ""), ("yaml", "- a list"), ("json", "[]"), ("txt", "path: tasks")],
)
def test_malformed_or_non_mapping_declarations_are_rejected(native_suite, field, suffix, text):
    reference = f"declarations/invalid.{suffix}"
    (native_suite / reference).write_text(text, encoding="utf-8")
    refs = {"dataset": DATASET, "job": JOB, field: reference}
    _update_yaml(native_suite / "suite.yaml", harbor=refs)
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("text", ['typo = true\n', '[environment]\ndocker_imgae = "alpine:3.20"\n'])
def test_unknown_native_task_fields_are_not_silently_discarded(native_suite, text):
    (native_suite / "tasks" / "example" / "task.toml").write_text(text, encoding="utf-8")
    with pytest.raises(SuiteError, match="typo|docker_imgae"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("reference", ["notes/policy.txt", Path("notes/policy.txt")])
def test_suite_path_accepts_portable_nested_strings_and_native_paths(native_suite, reference):
    assert suite_path(native_suite, reference) == native_suite / "notes" / "policy.txt"


@pytest.mark.parametrize(
    "reference",
    ["", ".", "../outside", "tasks/../tasks", "tasks/../../outside", "/outside",
     "C:/outside", "C:outside", "C:\\outside", "\\outside", "\\\\server\\share\\input",
     "//server/share/input", "tasks\\example", "tasks\\..\\outside", "file.txt:stream",
     "NUL", "notes/CON.txt", "notes/file.", "notes/file ", "notes/*.txt", "notes/a?.txt",
     "notes/<file>", "notes/a|b", 'notes/a"b', "notes/a\x00b", "notes/a\nb"],
)
def test_suite_path_rejects_traversal_and_nonportable_windows_paths(native_suite, reference):
    with pytest.raises(SuiteError, match="portable relative path|escapes"):
        suite_path(native_suite, reference)


def test_suite_path_rejects_even_an_absolute_path_inside_the_suite(native_suite):
    with pytest.raises(SuiteError, match="portable relative path"):
        suite_path(native_suite, (native_suite / "suite.yaml").as_posix())


def test_suite_path_checks_resolved_containment_not_a_string_prefix(native_suite, monkeypatch):
    candidate = native_suite / "linked-input"
    outside = native_suite.parent / "suite-sibling" / "input"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return outside if path == candidate else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(SuiteError, match="escapes"):
        suite_path(native_suite, "linked-input")


@pytest.mark.parametrize("link_method", ["is_symlink", "is_junction"])
def test_suite_path_rejects_linked_ancestors_without_requiring_link_privileges(native_suite, monkeypatch, link_method):
    linked = native_suite / "linked"
    original = getattr(Path, link_method)
    monkeypatch.setattr(Path, link_method, lambda path: path == linked or original(path))
    with pytest.raises(SuiteError, match="Linked suite inputs"):
        suite_path(native_suite, "linked/input.yaml")


@pytest.mark.parametrize("field", ["dataset", "job"])
def test_missing_native_declaration_references_fail(native_suite, field):
    refs = {"dataset": DATASET, "job": JOB, field: "declarations/missing.yaml"}
    _update_yaml(native_suite / "suite.yaml", harbor=refs)
    with pytest.raises(SuiteError, match="not found"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("field", ["dataset", "job"])
@pytest.mark.parametrize("reference", ["../outside.yaml", "C:/outside.yaml", "declarations\\job.yaml"])
def test_composition_rejects_unsafe_native_declaration_references(native_suite, field, reference):
    _update_yaml(native_suite / "suite.yaml", harbor={"dataset": DATASET, "job": JOB, field: reference})
    with pytest.raises(SuiteError, match="reference"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("path", ["missing-tasks", "declarations/dataset.yaml", "../tasks", "C:/tasks"])
def test_local_dataset_path_must_be_a_contained_existing_directory(native_suite, path):
    _write_yaml(native_suite / DATASET, {"path": path})
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


def test_local_registry_reference_is_resolved_without_reading_remote_tasks(native_suite, no_remote_resolution):
    registry = native_suite / "registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    _write_yaml(native_suite / DATASET, {"name": "examples", "registry_path": "registry.json"})
    job = compose_harbor_job(load_suite(native_suite))
    assert job.tasks == []
    assert job.datasets[0].registry_path == registry


@pytest.mark.parametrize(
    "dataset",
    [{"name": "examples", "registry_path": "missing.json"},
     {"name": "examples", "registry_path": "../outside.json"},
     {"name": "regression/examples", "download_dir": "cache"}],
)
def test_remote_dataset_rejects_missing_or_nonportable_local_inputs(native_suite, no_remote_resolution, dataset):
    _write_yaml(native_suite / DATASET, dataset)
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


def test_safe_extra_instructions_and_trajectory_resolve_against_suite_root(native_suite):
    notes = native_suite / "notes"
    notes.mkdir()
    policy = notes / "policy.txt"
    policy.write_text("Use only the fixture inputs.\n", encoding="utf-8")
    trajectory = native_suite / "trajectory.json"
    trajectory.write_text("{}\n", encoding="utf-8")
    _update_yaml(
        native_suite / JOB,
        extra_instruction_paths=["notes/policy.txt"],
        extra_instructions=["Keep this inline instruction."],
        agents=[{"name": "nop", "load_trajectory": "trajectory.json"}],
    )
    job = compose_harbor_job(load_suite(native_suite))
    assert job.extra_instruction_paths == [policy]
    assert job.extra_instructions == ["Keep this inline instruction."]
    assert job.agents[0].load_trajectory == str(trajectory)


@pytest.mark.parametrize(
    "options",
    [{"extra_instruction_paths": ["missing.txt"]},
     {"extra_instruction_paths": ["../outside.txt"]},
     {"extra_instruction_paths": ["C:/outside.txt"]},
     {"agents": [{"name": "nop", "load_trajectory": "missing.json"}]},
     {"agents": [{"name": "nop", "load_trajectory": "../outside.json"}]},
     {"environment": {"extra_docker_compose": ["missing.yaml"]}}],
)
def test_missing_or_unsafe_job_input_paths_fail(native_suite, options):
    _update_yaml(native_suite / JOB, **options)
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("reference", ["./skills/review", "skills/review"])
def test_local_skill_paths_resolve_from_suite_root(native_suite, reference):
    skill = native_suite / "skills/review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Synthetic review skill.\n", encoding="utf-8")
    _update_yaml(native_suite / JOB, agents=[{"name": "nop", "skills": [reference]}])
    job = compose_harbor_job(load_suite(native_suite))
    assert job.agents[0].skills == [str(skill)]


@pytest.mark.parametrize("reference", ["./missing", "../outside", "C:/outside"])
def test_missing_or_escaping_local_skill_is_rejected(native_suite, reference):
    _update_yaml(native_suite / JOB, agents=[{"name": "nop", "skills": [reference]}])
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


def test_remote_skill_specification_is_preserved(native_suite):
    reference = "example/skill@v1"
    _update_yaml(native_suite / JOB, agents=[{"name": "nop", "skills": [reference]}])
    assert compose_harbor_job(load_suite(native_suite)).agents[0].skills == [reference]


@pytest.mark.parametrize("agents", [None, [], [{}], [{"name": ""}], [{"name": "   "}]])
def test_agent_selection_must_be_explicit_before_native_defaults(native_suite, agents):
    data = yaml.safe_load((native_suite / JOB).read_text(encoding="utf-8"))
    if agents is None:
        data.pop("agents")
    else:
        data["agents"] = agents
    _write_yaml(native_suite / JOB, data)
    with pytest.raises(SuiteError, match="explicitly select"):
        compose_harbor_job(load_suite(native_suite))


def test_extra_instruction_path_must_be_a_file(native_suite, no_remote_resolution):
    # A package manifest isolates this check from native task path validation.
    _use_manifest(native_suite)
    _update_yaml(native_suite / JOB, extra_instruction_paths=["tasks"])
    with pytest.raises(SuiteError):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("manifest", [False, True], ids=["local", "manifest"])
def test_composition_only_adds_resolved_digests_in_memory(native_suite, manifest):
    dataset_path = _use_manifest(native_suite) if manifest else native_suite / DATASET
    before_files = _snapshot(native_suite)
    suite = load_suite(native_suite)
    expected = deepcopy(suite.model_dump())
    job = compose_harbor_job(suite)
    expected["overlay"]["harbor"].update(
        dataset_digest=sha256(dataset_path.read_bytes()).hexdigest(),
        job_digest=sha256((native_suite / JOB).read_bytes()).hexdigest(),
    )
    assert suite.model_dump() == expected
    assert _snapshot(native_suite) == before_files
    assert load_suite(native_suite).overlay.harbor.dataset_digest is None
    # A second composition must accept the same still-relative declarations.
    again = compose_harbor_job(suite)
    assert again.tasks == job.tasks
    assert suite.model_dump() == expected
    assert _snapshot(native_suite) == before_files


def test_native_migrations_do_not_mutate_parsed_input_mappings(native_suite, no_remote_resolution, monkeypatch):
    dataset = {"name": "examples", "registry": {"url": "https://registry.invalid/index.json"}}
    job_data = {"n_attempts": 2, "n_concurrent_trials": 1, "agents": [{"name": "nop"}],
                "environment": {"mounts_json": []}}
    _write_yaml(native_suite / DATASET, dataset)
    _write_yaml(native_suite / JOB, job_data)
    original_dataset, original_job = deepcopy(dataset), deepcopy(job_data)
    mappings = {native_suite / DATASET: dataset, native_suite / JOB: job_data}
    monkeypatch.setattr(composition, "read_mapping", mappings.__getitem__)
    with pytest.warns(DeprecationWarning):
        job = compose_harbor_job(load_suite(native_suite))
    assert job.environment.mounts == []
    assert job.datasets[0].registry_url == dataset["registry"]["url"]
    assert dataset == original_dataset
    assert job_data == original_job


@pytest.mark.parametrize("scope", ["agent", "environment", "verifier", "nested-kwargs"])
@pytest.mark.parametrize("value", ["literal-test-secret", "", "$HOST_TOKEN", "prefix-${HOST_TOKEN}", "${HOST_TOKEN}-suffix", "${}"])
def test_sensitive_job_env_requires_a_complete_host_template(native_suite, scope, value):
    env = {"API_TOKEN": value}
    if scope == "agent":
        options = {"agents": [{"name": "nop", "env": env}]}
    elif scope == "nested-kwargs":
        options = {"agents": [{"name": "nop", "kwargs": {"workers": [{"env": env}]}}]}
    else:
        options = {scope: {"env": env}}
    _update_yaml(native_suite / JOB, **options)
    with pytest.raises(SuiteError, match="API_TOKEN.*host environment template"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("template", ["${AEVAL_TEST_TOKEN}", "${AEVAL_TEST_TOKEN:-}"])
def test_env_templates_are_preserved_not_expanded_or_redacted(native_suite, no_remote_resolution, monkeypatch, template):
    _use_manifest(native_suite)
    monkeypatch.setenv("AEVAL_TEST_TOKEN", "host-value-that-must-not-be-copied")
    env = {"API_TOKEN": template, "MODE": "fixture", "MAX_TOKENS": "32"}
    _update_yaml(native_suite / JOB, agents=[{"name": "nop", "env": env}],
                 environment={"env": env}, verifier={"env": env})
    job = compose_harbor_job(load_suite(native_suite))
    assert job.agents[0].env == env
    assert job.environment.env == env
    assert job.verifier.env == env
    assert "host-value-that-must-not-be-copied" not in job.model_dump_json()


@pytest.mark.filterwarnings("ignore:List-style 'environment.env' is deprecated:DeprecationWarning")
def test_legacy_list_env_cannot_bypass_sensitive_template_policy(native_suite, no_remote_resolution):
    _use_manifest(native_suite)
    _update_yaml(native_suite / JOB, environment={"env": ["API_TOKEN=literal-test-secret"]})
    with pytest.raises(SuiteError, match="API_TOKEN|environment.env"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("scope", ["environment", "verifier", "solution"])
def test_sensitive_native_task_env_requires_host_templates(native_suite, scope):
    task = native_suite / "tasks" / "example" / "task.toml"
    text = task.read_text(encoding="utf-8")
    task.write_text(text + f'[{scope}.env]\nAPI_TOKEN = "literal-test-secret"\n', encoding="utf-8")
    with pytest.raises(SuiteError, match="API_TOKEN.*host environment template"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("action", [{"pin": "alpine@sha256:" + "a" * 64}, {"rebuild": True}])
def test_runtime_image_actions_raise_instead_of_being_ignored(native_suite, action):
    _update_yaml(native_suite / "suite.yaml", image={"environment": action})
    before = _snapshot(native_suite)
    with pytest.raises(SuiteError, match="cannot.*apply image.pin/rebuild at runtime"):
        compose_harbor_job(load_suite(native_suite))
    assert _snapshot(native_suite) == before


READ_ONLY_GIT = [
    ("ls-files", "--error-unmatch", "--", "suite.yaml"),
    ("status", "--porcelain", "--untracked-files=normal", "--", "."),
    ("ls-files", "--others", "--ignored", "--exclude-standard", "--", "."),
    ("rev-parse", "HEAD"),
]


def _mock_git(monkeypatch, root: Path, *, outputs=None, fail_at=None):
    values = {READ_ONLY_GIT[0]: "suite.yaml\n", READ_ONLY_GIT[1]: "",
              READ_ONLY_GIT[2]: "", READ_ONLY_GIT[3]: COMMIT + "\n"}
    values.update(outputs or {})

    def run(args, **kwargs):
        assert args[:3] == ["git", "-C", str(root.resolve())]
        command = tuple(args[3:])
        assert command in READ_ONLY_GIT, "Provenance may only issue read-only Git commands"
        assert kwargs == {"capture_output": True, "text": True, "encoding": "utf-8",
                          "errors": "replace", "check": False}
        return subprocess.CompletedProcess(args, 128 if command == fail_at else 0,
                                           stdout=values[command], stderr="")

    mocked = Mock(side_effect=run)
    monkeypatch.setattr(subprocess, "run", mocked)
    return mocked


def test_source_commit_preserves_full_hash_and_only_uses_read_only_git(tmp_path, monkeypatch):
    git = _mock_git(monkeypatch, tmp_path)
    assert suite_source_commit(tmp_path) == COMMIT
    assert [tuple(call.args[0][3:]) for call in git.call_args_list] == READ_ONLY_GIT
    assert list(tmp_path.iterdir()) == []  # No repository initialization or commits.


@pytest.mark.parametrize("fail_at", READ_ONLY_GIT)
def test_missing_source_commit_or_failed_git_query_is_rejected(tmp_path, monkeypatch, fail_at):
    git = _mock_git(monkeypatch, tmp_path, fail_at=fail_at)
    with pytest.raises(SuiteError, match="committed suite directory"):
        suite_source_commit(tmp_path)
    assert git.call_count == READ_ONLY_GIT.index(fail_at) + 1


def test_missing_git_executable_is_reported_as_suite_error(tmp_path, monkeypatch):
    git = Mock(side_effect=FileNotFoundError("git is unavailable"))
    monkeypatch.setattr(subprocess, "run", git)
    with pytest.raises(SuiteError, match="requires Git"):
        suite_source_commit(tmp_path)
    git.assert_called_once()


@pytest.mark.parametrize("status", [" M suite.yaml\n", "M  declarations/job.yaml\n", "?? tasks/new/task.toml\n"])
def test_dirty_or_untracked_suite_cannot_claim_a_source_commit(tmp_path, monkeypatch, status):
    git = _mock_git(monkeypatch, tmp_path, outputs={READ_ONLY_GIT[1]: status})
    with pytest.raises(SuiteError, match="uncommitted changes"):
        suite_source_commit(tmp_path)
    assert git.call_count == 2


def test_ignored_suite_inputs_cannot_claim_a_source_commit(tmp_path, monkeypatch):
    git = _mock_git(monkeypatch, tmp_path, outputs={READ_ONLY_GIT[2]: "tasks/example/ignored.txt\n"})
    with pytest.raises(SuiteError, match="ignored files"):
        suite_source_commit(tmp_path)
    assert git.call_count == 3


@pytest.mark.parametrize("commit", ["", COMMIT[:12], "g" * 40, COMMIT.upper(), COMMIT + "0", "HEAD"])
def test_source_commit_requires_a_full_lowercase_hex_hash(tmp_path, monkeypatch, commit):
    _mock_git(monkeypatch, tmp_path, outputs={READ_ONLY_GIT[3]: commit + "\n"})
    with pytest.raises(SuiteError, match="full 40-character Git commit"):
        suite_source_commit(tmp_path)


# --- unsupported sandbox semantics rejected at suite time ------


def test_task_mounts_are_rejected_at_suite_time(native_suite):
    """e2b sandboxes have no host bind mounts; a task that declares
    mounts cannot have its evidence collected — refuse at validation."""
    task = native_suite / "tasks" / "example" / "task.toml"
    text = task.read_text(encoding="utf-8")
    task.write_text(
        text + '\n[[environment.mounts]]\ntype = "bind"\nsource = "/host"\ntarget = "/data"\n',
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="mounts"):
        compose_harbor_job(load_suite(native_suite))


@pytest.mark.parametrize("filter_key", ["include_logs", "exclude_logs"])
def test_job_verifier_log_filters_are_rejected(native_suite, filter_key):
    """Job-level verifier log filters can silently drop required
    evidence logs — rejected at suite validation."""
    job = native_suite / JOB
    _write_yaml(job, {
        "n_attempts": 2, "n_concurrent_trials": 1,
        "agents": [{"name": "nop"}],
        "verifier": {filter_key: ["*.log"]},
    })
    with pytest.raises(SuiteError, match="include_logs/exclude_logs"):
        compose_harbor_job(load_suite(native_suite))


def test_task_toml_verifier_fields_are_strictly_typed(native_suite):
    """Task-level TOML admits no unknown verifier keys at all — Harbor's
    own strict validation rejects them before any aeval logic runs."""
    task = native_suite / "tasks" / "example" / "task.toml"
    text = task.read_text(encoding="utf-8")
    task.write_text(text + '\n[verifier]\ninclude_logs = ["*.log"]\n', encoding="utf-8")
    with pytest.raises(SuiteError, match="Invalid native Harbor task"):
        compose_harbor_job(load_suite(native_suite))
