"""Suite loader / thin-overlay validation tests (plan §7 row 2).

Duplicate identities, Harbor-owned restatements, loosened image pins,
missing mandatory sections and provenance gates must all fail loudly,
naming the suite and the field — and never silently pick a winner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aeval.suite_loader.loader import (
    assert_unique_suite_identity,
    discover_suites,
    load_suite,
    overlay_digest,
    resolve_harbor_inputs,
)
from aeval.suite_loader.validation import (
    validate_harbor_job_shape,
    validate_task_provenance,
    validate_thin_overlay,
)
from aeval.suite_models import SuiteError

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
    assert suite.identity() == (suite.id, suite.version, suite.suite_yaml_digest)
    assert overlay_digest(suite_dir / "suite.yaml") == suite.suite_yaml_digest
    # same bytes → same digest (reproducibility)
    again = load_suite(suite_dir)
    assert again.suite_yaml_digest == suite.suite_yaml_digest


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
    suite_dir = _copy_demo(tmp_path)
    _edit(suite_dir, lambda d: d.update(budget={"tokens": 1000}))
    with pytest.raises(SuiteError, match="budget"):
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


def test_different_versions_coexist(tmp_path):
    a = _copy_demo(tmp_path / "a")
    b = _copy_demo(tmp_path / "b")
    _edit(b, lambda d: d.update(version="1.5.0"))
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


def test_image_narrowing_undeclared_image_with_task_image(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    pin = "repo/app@sha256:" + "b" * 64
    _edit(suite_dir, lambda d: d.update(image={"task": {"pin": pin}}))
    task = _harbor_task(suite_dir)
    task["docker_image"] = "repo/app:latest"
    (suite_dir / "datasets" / "refund-policy.yaml").write_text(
        yaml.safe_dump(task), encoding="utf-8")
    suite = load_suite(suite_dir)
    validate_thin_overlay(suite, task, _harbor_job(suite_dir))


def test_image_pin_equal_to_harbor_value_is_restatement(tmp_path):
    suite_dir = _copy_demo(tmp_path)
    pin = "repo/app@sha256:" + "b" * 64
    _edit(suite_dir, lambda d: d.update(image={"task": {"pin": pin}}))
    task = _harbor_task(suite_dir)
    task["docker_image"] = pin  # already narrowed — no change
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
