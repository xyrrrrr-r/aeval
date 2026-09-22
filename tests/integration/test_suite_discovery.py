"""Suite discovery integration tests (plan §7 row 2): root-to-report path."""

from __future__ import annotations

from pathlib import Path

from aeval.suite_loader.loader import (
    assert_unique_suite_identity,
    discover_suites,
    load_suite,
    render_suite_explanation,
    resolve_harbor_inputs,
)
from aeval.suite_loader.validation import (
    validate_harbor_job_shape,
    validate_task_provenance,
    validate_thin_overlay,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_full_discovery_to_validated_suite():
    suites = discover_suites([FIXTURES / "suites"])
    assert len(suites) == 1
    resolved_suites = [load_suite(s) for s in suites]
    assert_unique_suite_identity(resolved_suites)

    suite = resolved_suites[0]
    inputs = resolve_harbor_inputs(suite)
    assert len(inputs.dataset_digest) == 64
    assert len(inputs.job_digest) == 64

    import yaml

    dataset = yaml.safe_load((suite.suite_dir / inputs.dataset).read_text(encoding="utf-8"))
    job = yaml.safe_load((suite.suite_dir / inputs.job).read_text(encoding="utf-8"))
    validate_thin_overlay(suite, dataset, job)
    validate_harbor_job_shape(job, suite.suite_dir / inputs.job)
    for task in dataset.get("tasks", []):
        validate_task_provenance(task, task.get("id", "?"))


def test_explanation_is_a_rendered_artifact_not_input():
    suite = load_suite(discover_suites([FIXTURES / "suites"])[0])
    text = render_suite_explanation(suite)
    assert "refund-policy" in text
    assert "1.4.0" in text
