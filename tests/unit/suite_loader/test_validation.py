"""Suite loader / thin-overlay validation tests.

Duplicate identities, Harbor-owned restatements, loosened image pins,
missing mandatory sections and provenance gates must all fail loudly,
naming the suite and the field — and never silently pick a winner.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tomllib

from harbor.models.job.config import JobConfig
from harbor.models.task.config import TaskConfig
from pydantic import ValidationError
import pytest
import yaml

from aeval.suite_loader.loader import (
    assert_unique_suite_identity,
    discover_suites,
    load_suite,
    overlay_digest,
    render_suite_explanation,
    resolve_harbor_inputs,
)
from aeval.suite_loader.validation import (
    validate_harbor_job_shape,
    validate_task_provenance,
    validate_thin_overlay,
)
from aeval.suite_models import (
    BaselineAssertion,
    ImagePinAction,
    ProvenanceInfo,
    SuiteError,
    VerdictSpec,
)

DEMO = Path(__file__).resolve().parents[2] / "fixtures" / "suites" / "demo"


def _copy_demo(tmp_path: Path) -> Path:
    suite = tmp_path / "suite"
    suite.mkdir(parents=True)
    for rel in ("suite.yaml", "datasets/refund-policy.yaml", "jobs/refund-policy.yaml"):
        src = DEMO / rel
        dst = suite / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    return suite


def _edit(suite_dir: Path, mutate) -> None:
    path = suite_dir / "suite.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _harbor_task(suite_dir: Path) -> dict:
    path = suite_dir / "datasets" / "refund-policy.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _harbor_job(suite_dir: Path) -> dict:
    path = suite_dir / "jobs" / "refund-policy.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_demo_fixture_loads_and_digest_is_stable(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    suite = load_suite(suite_dir)
    assert suite.id == "refund-policy"
    assert suite.version == "1.4.0"
    # identity() carries the chain digest (resolved overlay + source files);
    # a suite without `extends` still has exactly one source.
    assert suite.identity() == (suite.id, suite.version, suite.overlay_chain_digest)
    assert suite.extends == [] and [s.role for s in suite.sources] == ["child"]
    # the raw-bytes digest keeps its old meaning, so sealed evidence recomputes
    assert overlay_digest(suite_dir / "suite.yaml") == suite.suite_yaml_digest
    # same bytes → same digests (reproducibility)
    again = load_suite(suite_dir)
    assert again.suite_yaml_digest == suite.suite_yaml_digest
    assert again.overlay_chain_digest == suite.overlay_chain_digest


def test_discover_missing_root_is_error(tmp_path):
    with pytest.raises(SuiteError, match="does not exist"):
        discover_suites([tmp_path / "nowhere"])


def test_discover_finds_nested_suites(tmp_path):
    a = _copy_demo(tmp_path / "a")
    nested = _copy_demo(tmp_path / "b" / "nested")
    found = discover_suites([tmp_path])
    assert set(found) == {a, nested}


def test_load_suite_requires_suite_yaml(tmp_path):
    with pytest.raises(SuiteError, match="missing suite.yaml"):
        load_suite(tmp_path)


def test_restating_harbor_owned_fact_rejected(tmp_path):
    """Harbor-owned facts stay Harbor's. NOTE: budget is deliberately not one of
    them — Harbor's JobConfig has no budget field, so a spend cap is an aeval fact
    declared in the suite overlay (see test_suite_budget_cap)."""
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(trials={"k": 3}))
    with pytest.raises(SuiteError, match="trials"):
        load_suite(suite_dir)


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda d: d.pop("baselines"), "baselines"),
        (lambda d: d.update(baselines=[]), "baselines"),
        (lambda d: d.pop("observables"), "observables"),
        (lambda d: d.update(observables=[]), "observables"),
        (lambda d: d.pop("verdict"), "verdict"),
        (lambda d: d.pop("provenance"), "provenance"),
        (lambda d: d.pop("clock"), "clock"),
        (lambda d: d.update(schema_version=3), "schema_version"),
    ],
)
def test_missing_mandatory_section_rejected(tmp_path, mutate, field):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, mutate)
    with pytest.raises(SuiteError, match=field):
        load_suite(suite_dir)


def test_same_identity_different_content_rejected(tmp_path):
    a = _copy_demo(tmp_path / "a")
    b = _copy_demo(tmp_path / "b")
    _edit(b, lambda d: d.update(version="1.4.0", id="refund-policy",
                                clock={"mode": "real", "epoch": None}))
    suites = [load_suite(a), load_suite(b)]
    with pytest.raises(SuiteError, match="different content"):
        assert_unique_suite_identity(suites)


def test_same_identity_same_content_also_rejected(tmp_path):
    a = _copy_demo(tmp_path / "a")
    b = _copy_demo(tmp_path / "b")
    suites = [load_suite(a), load_suite(b)]
    with pytest.raises(SuiteError, match="duplicate suite identity"):
        assert_unique_suite_identity(suites)


def test_same_id_different_versions_rejected(tmp_path):
    a = _copy_demo(tmp_path / "a")
    b = _copy_demo(tmp_path / "b")
    _edit(b, lambda d: d.update(version="1.5.0"))
    with pytest.raises(SuiteError, match="duplicate suite identity"):
        assert_unique_suite_identity([load_suite(a), load_suite(b)])


def test_different_ids_coexist(tmp_path):
    a = _copy_demo(tmp_path / "a")
    b = _copy_demo(tmp_path / "b")
    _edit(b, lambda d: d.update(id="another-suite"))
    assert_unique_suite_identity([load_suite(a), load_suite(b)])


def test_harbor_reference_escape_rejected(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("tasks: []\n", encoding="utf-8")
    _edit(suite_dir, lambda d: d["harbor"].update(dataset="../outside.yaml"))
    suite = load_suite(suite_dir)
    with pytest.raises(SuiteError, match="escapes the suite directory"):
        resolve_harbor_inputs(suite)


def test_harbor_reference_missing_file_rejected(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d["harbor"].update(job="jobs/nope.yaml"))
    suite = load_suite(suite_dir)
    with pytest.raises(SuiteError, match="not found"):
        resolve_harbor_inputs(suite)


def test_task_with_no_provenance_rejected(tmp_path):
    with pytest.raises(SuiteError, match="no provenance block"):
        validate_task_provenance({"id": "t1"}, source="t.yaml")


def test_imported_data_without_license_rejected():
    with pytest.raises(SuiteError, match="data_imported"):
        validate_task_provenance(
            {"id": "t1", "provenance": {
                "source": "public-benchmark", "license": "NONE_DECLARED",
                "data_imported": True,
            }},
            source="t.yaml",
        )


def test_imported_data_with_license_passes():
    validate_task_provenance(
        {"id": "t1", "provenance": {
            "source": "public-benchmark", "license": "CC0",
            "data_imported": True, "rewritten_by_us": True,
        }},
        source="t.yaml",
    )


def test_unknown_requirement_name_rejected(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d["verdict"].update(
        requirements=["input_complete", "made_up_requirement"]))
    suite = load_suite(suite_dir)
    with pytest.raises(SuiteError, match="made_up_requirement"):
        validate_thin_overlay(suite, _harbor_task(suite_dir), _harbor_job(suite_dir))


def test_image_narrowing_undeclared_image_rejected(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(image={
        "task": {"pin": "repo/app@sha256:" + "b" * 64}}))
    suite = load_suite(suite_dir)
    with pytest.raises(SuiteError, match="does not declare"):
        validate_thin_overlay(suite, _harbor_task(suite_dir), _harbor_job(suite_dir))


def test_image_narrowing_declared_environment_image(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    pin = "repo/app@sha256:" + "b" * 64
    _edit(suite_dir, lambda d: d.update(image={"environment": {"pin": pin}}))
    task = {"environment": {"docker_image": "repo/app:latest"}}
    TaskConfig.model_validate(task, extra="forbid")
    suite = load_suite(suite_dir)
    validate_thin_overlay(suite, task, _harbor_job(suite_dir))


@pytest.mark.parametrize("name", ["environment", "verifier"])
def test_image_pin_equal_to_harbor_value_is_restatement(tmp_path, name):
    suite_dir = _copy_demo(tmp_path)
    pin = "repo/app@sha256:" + "b" * 64
    _edit(suite_dir, lambda d: d.update(image={name: {"pin": pin}}))
    task = {"environment": {"docker_image": pin}}
    if name == "verifier":
        task = {"verifier": task}
    TaskConfig.model_validate(task, extra="forbid")
    suite = load_suite(suite_dir)
    with pytest.raises(SuiteError, match="not a narrowing"):
        validate_thin_overlay(suite, task, _harbor_job(suite_dir))


def test_job_restating_aeval_fact_rejected(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    suite = load_suite(suite_dir)
    job = _harbor_job(suite_dir)
    job["baselines"] = [{"id": "x"}]
    with pytest.raises(SuiteError, match="restates aeval-owned fact 'baselines'"):
        validate_thin_overlay(suite, _harbor_task(suite_dir), job)


def test_job_shape_requires_attempts_and_concurrency():
    with pytest.raises(SuiteError, match="n_attempts"):
        validate_harbor_job_shape({"n_concurrent_trials": 2})
    with pytest.raises(SuiteError, match="n_concurrent_trials"):
        validate_harbor_job_shape({"n_attempts": 5})
    validate_harbor_job_shape({"n_attempts": 5, "n_concurrent_trials": 2})


@pytest.mark.parametrize("license", ["NONE_DECLARED", "UNKNOWN"])
@pytest.mark.parametrize("data_imported", [True, "true"])
def test_license_gate_runs_after_all_provenance_fields(tmp_path, license, data_imported):
    provenance = {
        "source": "public-benchmark", "license": license,
        "data_imported": data_imported, "rewritten_by_us": True,
    }
    with pytest.raises(SuiteError, match="data_imported"):
        ProvenanceInfo.model_validate(provenance)
    with pytest.raises(SuiteError, match="data_imported"):
        validate_task_provenance({"provenance": provenance})
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(provenance=provenance))
    with pytest.raises(SuiteError, match="data_imported"):
        load_suite(suite_dir)


@pytest.mark.parametrize("license", ["NONE_DECLARED", "UNKNOWN"])
def test_unlicensed_skeleton_provenance_allowed(license):
    provenance = {"source": "public-benchmark", "license": license}
    assert not ProvenanceInfo.model_validate(provenance).data_imported
    validate_task_provenance({"provenance": provenance})


def test_standard_cc0_license_alias_normalized(tmp_path):
    provenance = {
        "source": "public-benchmark", "license": "CC0-1.0", "data_imported": True,
    }
    assert ProvenanceInfo.model_validate(provenance).license == "CC0"
    validate_task_provenance({"provenance": provenance})
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(provenance=provenance))
    assert load_suite(suite_dir).overlay.provenance.license == "CC0"


@pytest.mark.parametrize("missing", ["source", "license"])
def test_provenance_fields_remain_required(tmp_path, missing):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d["provenance"].pop(missing))
    with pytest.raises(SuiteError, match=missing):
        load_suite(suite_dir)


def test_design_yaml_assert_and_extra_graders(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    expression = "approval_policy != 'ask'"
    _edit(suite_dir, lambda d: d.update(
        baselines=[{"id": "no_ask", "assert": expression}],
        verdict={
            "requirements": ["agent_finished"],
            "graders": {
                "extra": [
                    {"impl": "trajectory.py@v1", "layer": "trajectory", "veto": True},
                    {"impl": "both.py@v2", "layer": "both"},
                ],
                "default": {"impl": "outcome.py@v1"},
            },
        },
    ))
    suite = load_suite(suite_dir)
    baseline = suite.overlay.baselines[0]
    assert baseline.assert_expr == expression
    assert baseline.model_dump()["assert_expr"] == expression
    assert baseline.model_dump(by_alias=True)["assert"] == expression
    assert f"assert={expression!r}" in render_suite_explanation(suite)
    graders = suite.overlay.verdict.resolved_graders()
    assert [g.impl for g in graders] == [
        "outcome.py@v1", "trajectory.py@v1", "both.py@v2",
    ]
    assert graders[1].veto
    assert suite.overlay.verdict.resolved_graders() == graders
    assert BaselineAssertion(id="python", assert_expr=expression).assert_expr == expression


@pytest.mark.parametrize("declaration", [
    {},
    {"probe": "observable:count", "assert": "count == 1"},
    {"probe": "observable:count", "assert_expr": "count == 1"},
    {"probe": None, "assert": None},
])
def test_baseline_requires_exactly_one_form(declaration):
    with pytest.raises(SuiteError, match="exactly one"):
        BaselineAssertion.model_validate({"id": "count", **declaration})


@pytest.mark.parametrize("declaration", [
    {"probe": ""}, {"assert": ""},
    {"assert": "count == 1", "assert_expr": "count == 2"},
])
def test_empty_or_ambiguous_baseline_rejected(declaration):
    with pytest.raises(ValidationError):
        BaselineAssertion.model_validate({"id": "count", **declaration})


@pytest.mark.parametrize("graders, expected", [
    ({"second": {"impl": "b"}, "default": {"impl": "a"}}, ["a", "b"]),
    ({"second": {"impl": "b"}, "first": {"impl": "a"}}, ["b", "a"]),
    ([{"impl": "b"}, {"impl": "a"}], ["b", "a"]),
    ({"default": {"impl": "a"}, "extra": []}, ["a"]),
    ({"extra": {"impl": "legacy"}}, ["legacy"]),
    ({}, []),
])
def test_existing_grader_formats_preserve_order(graders, expected):
    verdict = VerdictSpec(requirements=["agent_finished"], graders=graders)
    assert [g.impl for g in verdict.resolved_graders()] == expected


def test_default_grader_cannot_be_a_list():
    with pytest.raises(SuiteError, match="default"):
        VerdictSpec(requirements=["agent_finished"], graders={"default": [{"impl": "a"}]})


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(typo=True),
    lambda d: d["harbor"].update(typo=True),
    lambda d: d["baselines"][0].update(typo=True),
    lambda d: d["clock"].update(typo=True),
    lambda d: d["observables"][0].update(typo=True),
    lambda d: d["verdict"].update(typo=True),
    lambda d: d["verdict"]["graders"]["default"].update(typo=True),
    lambda d: d["verdict"]["graders"].update(extra=[{"impl": "a", "typo": True}]),
    lambda d: d["metrics"][0].update(typo=True),
    lambda d: d["driver"].update(typo=True),
    lambda d: d["provenance"].update(typo=True),
    lambda d: d.update(image={"environment": {"rebuild": True, "typo": True}}),
])
def test_unknown_overlay_fields_are_not_dropped(tmp_path, mutate):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, mutate)
    with pytest.raises(SuiteError, match="typo"):
        load_suite(suite_dir)


def test_verdict_requirements_remain_nonempty(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d["verdict"].update(requirements=[]))
    with pytest.raises(SuiteError, match="requirements"):
        load_suite(suite_dir)


@pytest.mark.parametrize("action", [
    {}, {"rebuild": False}, {"pin": None},
    {"pin": "repo/app@sha256:" + "a" * 64, "rebuild": True},
])
def test_image_action_requires_exactly_one_choice(tmp_path, action):
    with pytest.raises(SuiteError, match="exactly one"):
        ImagePinAction.model_validate(action)
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(image={"environment": action}))
    with pytest.raises(SuiteError, match="exactly one"):
        load_suite(suite_dir)


@pytest.mark.parametrize("pin", [
    "repo/app:latest", "repo/app@sha256:abc", "repo/app@sha256:" + "a" * 63,
    "repo/app@sha256:" + "a" * 65, "repo/app@sha256:" + "g" * 64,
    "repo/app@sha256:" + "a" * 64 + "\n", "@sha256:" + "a" * 64,
])
def test_image_pin_requires_full_sha256(pin):
    with pytest.raises(SuiteError, match="full sha256"):
        ImagePinAction(pin=pin)


@pytest.mark.parametrize("name", ["environment", "verifier"])
@pytest.mark.parametrize("action", [
    {"pin": "localhost:5000/repo/app@sha256:" + "a" * 64}, {"rebuild": True},
])
def test_native_harbor_image_narrowing(tmp_path, name, action):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(image={name: action}))
    task = {"environment": {"docker_image": "repo/app:latest"}}
    if name == "verifier":
        task = {"verifier": task}
    TaskConfig.model_validate(task, extra="forbid")
    validate_thin_overlay(load_suite(suite_dir), task, _harbor_job(suite_dir))


@pytest.mark.parametrize("name, task", [
    ("task", {"docker_image": "repo/app:latest"}),
    ("environment", {"environment": {"image": "repo/app:latest"}}),
    ("verifier", {"verifier": {"image": "repo/app:latest"}}),
])
def test_non_native_image_fields_do_not_authorize_narrowing(tmp_path, name, task):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(image={name: {"rebuild": True}}))
    with pytest.raises(SuiteError, match="does not declare"):
        validate_thin_overlay(load_suite(suite_dir), task, _harbor_job(suite_dir))


@pytest.mark.parametrize("field", ["n_attempts", "n_concurrent_trials"])
@pytest.mark.parametrize("value", [None, 0, -1, True, False, 1.5, "2"])
def test_job_trial_counts_must_be_positive_explicit_integers(field, value):
    job = {"n_attempts": 5, "n_concurrent_trials": 2, field: value}
    with pytest.raises(SuiteError, match=field):
        validate_harbor_job_shape(job, "job.toml")


@pytest.mark.parametrize("extra", [
    {"n_attempt": 5}, {"parallel": 2}, {"plugins": []},
    {"job": {"n_attempts": 5, "n_concurrent_trials": 2}},
    {"orchestrator": {"n_concurrent_trials": 2}},
])
def test_unknown_job_keys_rejected(extra):
    with pytest.raises(SuiteError, match="unknown job keys"):
        validate_harbor_job_shape({"n_attempts": 5, "n_concurrent_trials": 2, **extra})


@pytest.mark.parametrize("extra, field", [
    ({"retry": {"max_retries": -1}}, "max_retries"),
    ({"retry": {"max_retrys": 3}}, "max_retrys"),
    ({"environment": {"type": "not-a-provider"}}, "environment"),
    ({"agents": [{"n_concurrent": 3}]}, "n_concurrent"),
    ({"agents": [{"modle_name": "typo"}]}, "modle_name"),
    ({"datasets": [{"path": "tasks", "name": "conflicting-source"}]}, "path"),
])
def test_job_shape_validates_native_harbor_schema(extra, field):
    with pytest.raises(SuiteError, match=field):
        validate_harbor_job_shape({"n_attempts": 5, "n_concurrent_trials": 2, **extra})


def test_native_job_shape_keeps_kwargs_and_input_unchanged():
    job = {
        "n_attempts": 5, "n_concurrent_trials": 2,
        "environment": {"type": "docker", "kwargs": {"provider_option": True}},
        "agents": [{"name": "oracle", "kwargs": {"custom_option": 3}}],
        "tasks": [{"path": "tasks/refund"}],
    }
    original = deepcopy(job)
    JobConfig.model_validate(job, extra="forbid")
    validate_harbor_job_shape(job)
    assert job == original


@pytest.mark.parametrize("job", [[], None, "n_attempts=5"])
def test_job_shape_requires_mapping(job):
    with pytest.raises(SuiteError, match="mapping"):
        validate_harbor_job_shape(job)


@pytest.mark.parametrize("field", ["dataset", "job"])
def test_sibling_prefix_traversal_rejected(tmp_path, field):
    suite_dir = _copy_demo(tmp_path)
    sibling = tmp_path / "suite-sibling"
    sibling.mkdir()
    (sibling / "outside.yaml").write_text("tasks: []\n", encoding="utf-8")
    _edit(suite_dir, lambda d: d["harbor"].update({field: "../suite-sibling/outside.yaml"}))
    with pytest.raises(SuiteError, match="escapes the suite directory"):
        resolve_harbor_inputs(load_suite(suite_dir))


@pytest.mark.parametrize("reference", [
    "", ".", "/outside.yaml", "C:/outside.yaml", "C:outside.yaml",
    "C:\\outside.yaml", "\\outside.yaml", "\\\\server\\share\\outside.yaml",
    "//server/share/outside.yaml", "datasets\\refund-policy.yaml",
    "datasets/refund-policy.yaml:stream", "datasets/NUL.yaml",
    "datasets/refund-policy.yaml.", "datasets/refund-policy.yaml ",
    "datasets/../datasets/refund-policy.yaml", "datasets\\..\\outside.yaml",
])
def test_harbor_references_must_be_portable_relative_paths(tmp_path, reference):
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d["harbor"].update(dataset=reference))
    with pytest.raises(SuiteError, match="Harbor reference"):
        resolve_harbor_inputs(load_suite(suite_dir))


def test_even_contained_absolute_reference_rejected(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    absolute = (suite_dir / "datasets" / "refund-policy.yaml").as_posix()
    _edit(suite_dir, lambda d: d["harbor"].update(dataset=absolute))
    with pytest.raises(SuiteError, match="relative path"):
        resolve_harbor_inputs(load_suite(suite_dir))


def test_resolved_sibling_prefix_escape_rejected_without_symlink_privilege(tmp_path, monkeypatch):
    suite_dir = _copy_demo(tmp_path)
    sibling = tmp_path / "suite-sibling"
    sibling.mkdir()
    outside = sibling / "outside.yaml"
    outside.write_text("tasks: []\n", encoding="utf-8")
    linked = suite_dir / "datasets" / "linked.yaml"
    _edit(suite_dir, lambda d: d["harbor"].update(dataset="datasets/linked.yaml"))
    resolve = Path.resolve

    def fake_resolve(path, *args, **kwargs):
        if path == linked:
            return outside
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    with pytest.raises(SuiteError, match="escapes the suite directory"):
        resolve_harbor_inputs(load_suite(suite_dir))


@pytest.mark.parametrize("outside", [True, False])
def test_symlink_reference_containment(tmp_path, outside):
    suite_dir = _copy_demo(tmp_path)
    if outside:
        target = tmp_path / "suite-sibling"
        target.mkdir()
        (target / "refund-policy.yaml").write_text("tasks: []\n", encoding="utf-8")
    else:
        target = suite_dir / "datasets"
    try:
        (suite_dir / "linked").symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    _edit(suite_dir, lambda d: d["harbor"].update(dataset="linked/refund-policy.yaml"))
    suite = load_suite(suite_dir)
    if outside:
        with pytest.raises(SuiteError, match="escapes the suite directory"):
            resolve_harbor_inputs(suite)
    else:
        inputs = resolve_harbor_inputs(suite)
        assert inputs.dataset_digest == sha256((target / "refund-policy.yaml").read_bytes()).hexdigest()


@pytest.mark.parametrize("suffix", ["yaml", "yml", "json", "toml"])
def test_native_harbor_mapping_formats_and_digests(tmp_path, suffix):
    suite_dir = _copy_demo(tmp_path)
    task = {
        "environment": {"docker_image": "repo/task:latest"},
        "verifier": {"environment": {"docker_image": "repo/verifier:latest"}},
    }
    job = {"n_attempts": 5, "n_concurrent_trials": 2}
    if suffix == "toml":
        task_text = (
            '[environment]\ndocker_image = "repo/task:latest"\n'
            '[verifier.environment]\ndocker_image = "repo/verifier:latest"\n'
        )
        job_text = "n_attempts = 5\nn_concurrent_trials = 2\n"
        assert tomllib.loads(task_text) == task
    elif suffix == "json":
        task_text, job_text = json.dumps(task), json.dumps(job)
    else:
        task_text, job_text = yaml.safe_dump(task), yaml.safe_dump(job)
    dataset_ref, job_ref = f"datasets/task.{suffix}", f"jobs/job.{suffix}"
    (suite_dir / dataset_ref).write_text(task_text, encoding="utf-8")
    (suite_dir / job_ref).write_text(job_text, encoding="utf-8")
    _edit(suite_dir, lambda d: d.update(
        harbor={"dataset": dataset_ref, "job": job_ref},
        image={
            "environment": {"pin": "repo/task@sha256:" + "a" * 64},
            "verifier": {"pin": "repo/verifier@sha256:" + "b" * 64},
        },
    ))
    suite = load_suite(suite_dir)
    inputs = resolve_harbor_inputs(suite)
    assert inputs.dataset == dataset_ref
    assert inputs.job == job_ref
    assert inputs.dataset_digest == sha256((suite_dir / dataset_ref).read_bytes()).hexdigest()
    assert inputs.job_digest == sha256((suite_dir / job_ref).read_bytes()).hexdigest()
    assert suite.overlay.harbor.dataset_digest is None
    TaskConfig.model_validate(task, extra="forbid")
    validate_thin_overlay(suite, task, job)
    validate_harbor_job_shape(job)


@pytest.mark.parametrize("field", ["dataset", "job"])
@pytest.mark.parametrize("suffix, text, message", [
    ("toml", "n_attempts = [", "Invalid Harbor file"),
    ("json", '{"n_attempts":', "Invalid Harbor file"),
    ("yaml", "tasks: [", "Invalid Harbor file"),
    ("json", "[]", "must be a mapping"),
    ("yaml", "- not-a-mapping", "must be a mapping"),
    ("yaml", "", "must be a mapping"),
])
def test_invalid_native_mapping_rejected(tmp_path, field, suffix, text, message):
    suite_dir = _copy_demo(tmp_path)
    reference = f"invalid.{suffix}"
    (suite_dir / reference).write_text(text, encoding="utf-8")
    _edit(suite_dir, lambda d: d["harbor"].update({field: reference}))
    with pytest.raises(SuiteError, match=message):
        resolve_harbor_inputs(load_suite(suite_dir))


def test_task_titles_merge_with_suite_side_priority(tmp_path):
    """中文任务显示名：注入器生成的 task_titles.cases.yaml 与套件自写
    的 task_titles.yaml 合并，套件侧优先（混合套件里命名权在套件）；
    两个文件都没有时为空。坏文件报错，不静默忽略。"""
    suite_dir = _copy_demo(tmp_path)
    assert load_suite(suite_dir).task_titles == {}
    (suite_dir / "task_titles.cases.yaml").write_text(
        "native-task: 注入名\nextra-task: 库属名\n", encoding="utf-8"
    )
    (suite_dir / "task_titles.yaml").write_text(
        "native-task: 套件名\n", encoding="utf-8"
    )
    suite = load_suite(suite_dir)
    assert suite.task_titles == {
        "native-task": "套件名", "extra-task": "库属名",
    }
    (suite_dir / "task_titles.yaml").write_text("- 不是映射\n", encoding="utf-8")
    with pytest.raises(SuiteError, match="flat task_id -> title"):
        load_suite(suite_dir)


def test_task_categories_merge_and_default(tmp_path):
    """类别聚合声明：注入器生成的 task_categories.cases.yaml 与套件
    自写的 task_categories.yaml 合并（套件侧优先），default 只认显式
    声明；坏结构报错。"""
    suite_dir = _copy_demo(tmp_path)
    (suite_dir / "task_categories.cases.yaml").write_text(
        "categories:\n  a2a: 注入名\n  native-cat: 库属类别\n",
        encoding="utf-8",
    )
    suite = load_suite(suite_dir)
    assert suite.category_names == {"a2a": "注入名", "native-cat": "库属类别"}
    assert suite.default_category is None
    (suite_dir / "task_categories.yaml").write_text(
        "categories:\n  a2a: 套件名\ndefault: a2a\n", encoding="utf-8"
    )
    suite = load_suite(suite_dir)
    assert suite.category_names["a2a"] == "套件名"  # 套件侧优先
    assert suite.default_category == "a2a"
    (suite_dir / "task_categories.yaml").write_text(
        "categories:\n  a2a:\n    - 不是映射\n", encoding="utf-8"
    )
    with pytest.raises(SuiteError, match="display name or a mapping"):
        load_suite(suite_dir)


def test_dimension_model_extended_schema(tmp_path):
    """维度模型详式：name/block/weight/threshold/
    redline + blocks + redline_tasks；简式只有显示名，其余取默认；坏
    参数报错。"""
    suite_dir = _copy_demo(tmp_path)
    (suite_dir / "task_categories.yaml").write_text(
        "categories:\n"
        "  a2a:\n"
        "    name: A2A 协议\n"
        "    block: orch\n"
        "    weight: 1.5\n"
        "    threshold: 0.85\n"
        "  error:\n"
        "    name: 异常处理\n"
        "    block: redline\n"
        "    redline: true\n"
        "blocks:\n"
        "  orch: 编排与协作\n"
        "  redline: 红线\n"
        "redline_tasks:\n"
        "  - a2a.duplicate_task_id\n"
        "default: a2a\n",
        encoding="utf-8",
    )
    suite = load_suite(suite_dir)
    model = suite.dimension_model
    assert model["categories"]["a2a"] == {
        "name": "A2A 协议", "block": "orch", "weight": 1.5,
        "threshold": 0.85, "redline": False,
    }
    # 未声明字段取默认（weight 1.0 / threshold 0.9 / 非红线）。
    assert model["categories"]["error"] == {
        "name": "异常处理", "block": "redline", "weight": 1.0,
        "threshold": 0.9, "redline": True,
    }
    assert model["blocks"] == {"orch": "编排与协作", "redline": "红线"}
    assert model["redline_tasks"] == ["a2a.duplicate_task_id"]
    assert model["default"] == "a2a"
    # 显示名投影（报告的「按类别结果」）来自同一份模型。
    assert suite.category_names == {"a2a": "A2A 协议", "error": "异常处理"}
    assert suite.default_category == "a2a"
    # 阈值必须在 (0, 1]——达标度是值/阈值，>1 的阈值没有意义。
    (suite_dir / "task_categories.yaml").write_text(
        "categories:\n  a2a:\n    name: X\n    threshold: 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="threshold must be in"):
        load_suite(suite_dir)
    # 权重必须为正。
    (suite_dir / "task_categories.yaml").write_text(
        "categories:\n  a2a:\n    name: X\n    weight: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="weight must be"):
        load_suite(suite_dir)
    # redline_tasks 必须是任务 id 列表。
    (suite_dir / "task_categories.yaml").write_text(
        "categories:\n  a2a: 名\nredline_tasks: not-a-list\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="redline_tasks"):
        load_suite(suite_dir)
