"""Offline validation of the e2e-hello suite skeleton (E2E phase prep).

Everything checkable without a real sandbox: suite validation, job
composition, grader identity + content-addressed grading, the pinned
arm64 digest, and the file-only observable contract.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from aeval.contracts import GradeResult
from aeval.hooks.evidence import build_required_collect_plan, FIXED_OUTPUT_PATHS
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.verdict.executor import grade_trial
from aeval.verdict.pipeline import load_suite_graders
from aeval.verdict.pipeline import build_trial_record
from aeval.verdict.progress import RequirementProgress

SUITE = Path(__file__).parents[2] / "suites" / "e2e-hello"
PINNED_DIGEST = "sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf"


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE)


def test_suite_loads_and_composes(suite):
    job = compose_harbor_job(suite)
    assert job.job_name == "e2e-hello"
    assert job.n_attempts == 5
    assert job.n_concurrent_trials == 1


def test_observables_are_file_sources_only(suite):
    """P0-2: db:/screenshot:/dom: probes fail closed — the e2e suite
    must declare file: observables exclusively."""
    for observable in suite.overlay.observables:
        assert observable.source.startswith("file:"), observable


def test_baselines_probe_only_declared_observables(suite):
    names = {o.name for o in suite.overlay.observables}
    for baseline in suite.overlay.baselines:
        assert baseline.probe is not None
        kind, _, name = baseline.probe.partition(":")
        assert kind == "observable", (
            f"baseline {baseline.id} must probe a suite-declared observable"
        )
        assert name in names, f"baseline {baseline.id} probes undeclared {name}"


def test_collect_plan_and_task_declarations_agree(suite):
    plan = build_required_collect_plan(suite)
    assert set(plan) == set(FIXED_OUTPUT_PATHS) | {
        f"observable:{o.name}" for o in suite.overlay.observables
    }
    # compose_harbor_job already refuses tasks that do not declare the
    # fixed outputs; assert the declaration text explicitly.
    task_toml = (SUITE / "tasks" / "hello" / "task.toml").read_text("utf-8")
    for name in FIXED_OUTPUT_PATHS:
        assert name in task_toml, f"task.toml must declare collect output {name}"


def test_image_is_digest_pinned_everywhere():
    """The sandbox image must be digest-pinned in task.toml AND the
    Dockerfile — mutable tags are forbidden (thin overlay cannot
    narrow at runtime)."""
    task_toml = (SUITE / "tasks" / "hello" / "task.toml").read_text("utf-8")
    dockerfile = (SUITE / "tasks" / "hello" / "environment" / "Dockerfile").read_text("utf-8")
    assert f"ubuntu@{PINNED_DIGEST}" in task_toml
    assert f"FROM ubuntu@{PINNED_DIGEST}" in dockerfile
    # no mutable tag reference anywhere
    assert '"ubuntu:24.04"' not in task_toml
    assert "FROM ubuntu:" not in dockerfile


def test_pinned_digest_is_the_arm64_manifest_digest():
    """The pinned digest is the multi-arch manifest digest of
    ubuntu:24.04 whose arm64 entry this suite builds on (fetched from
    the Docker Hub registry when the suite was authored; verified again
    at environment-build time by the image actually pulling)."""
    assert PINNED_DIGEST.startswith("sha256:")
    assert len(PINNED_DIGEST) == len("sha256:") + 64
    # The runtime lock must record platform arm64 for the sandbox image;
    # observed-identity binding (P0-2) compares against exactly this.
    from aeval.contracts import ImageIdentity

    image = ImageIdentity(
        reference=f"ubuntu@{PINNED_DIGEST}",
        digest=PINNED_DIGEST[len("sha256:"):],
        platform="arm64",
    )
    assert image.pinned is True


# --- grader identity + content-addressed grading ----------------------


@pytest.fixture(scope="module")
def graders(suite):
    resolved = load_suite_graders(suite)
    assert len(resolved) == 1
    return resolved


def test_grader_identity_is_versioned_and_layered(graders):
    grader = graders[0]
    assert grader.grader.id == "hello-outcome"
    assert grader.grader.version == "v1"
    assert grader.grader.layer == "outcome"
    assert grader.requires_fields == ["events", "token_usage"]
    # the suite declaration and the module agree on the version
    assert "graders/hello_outcome.py@v1" in SUITE.joinpath(
        "suite.yaml"
    ).read_text("utf-8")


def _record(tmp_path, *, result_value=None, with_result=True,
            completeness=("ok", "ok")):
    from aeval.contracts import ArtifactRef, TrialCoordinates

    artifacts = {}
    if with_result:
        content = json.dumps(
            {"name": "result", "value": result_value},
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
        (tmp_path / "artifacts").mkdir(exist_ok=True)
        (tmp_path / "artifacts" / "result.json").write_bytes(content)
        artifacts["observable:result"] = ArtifactRef(
            media_type="application/json",
            sha256=sha256(content).hexdigest(),
            size_bytes=len(content),
            path="artifacts/result.json",
        )
    events_status, usage_status = completeness
    extra = {"aeval": {"completeness": {"fields": [
        {"field": "events", "status": events_status},
        {"field": "token_usage", "status": usage_status},
    ]}}}
    progress = RequirementProgress()
    for bit in ("input_complete", "agent_finished", "integration_valid",
                "render_valid", "artifact_schema_ok"):
        progress.mark(bit)
    return build_trial_record(
        trial_id="trial-e2e-1",
        coordinates=TrialCoordinates(
            run_id="run-e2e", suite_id="e2e-hello",
            suite_version="0.1.0", task_id="hello", trial_index=0,
        ),
        stop_reason="agent_exit_0",
        baseline_ok=True,
        progress=progress,
        artifacts=artifacts,
        transcript_extra=extra,
        grader_versions={"hello-outcome": "v1"},
    )


async def test_grader_passes_on_exact_hello(tmp_path, graders):
    record = _record(tmp_path, result_value="hello")
    results = await grade_trial(record, graders)
    assert len(results) == 1
    assert results[0].status == "pass"
    assert results[0].score.value == 1.0
    assert results[0].grader_id == "hello-outcome"
    assert results[0].grader_version == "v1"


async def test_grader_fails_on_wrong_content(tmp_path, graders):
    record = _record(tmp_path, result_value="hello ")
    results = await grade_trial(record, graders)
    assert results[0].status == "fail"
    assert results[0].score.value == 0.0
    assert "content digest" in " ".join(results[0].reasons)
    assert "expected" in " ".join(results[0].reasons)


async def test_grader_fails_on_missing_artifact(tmp_path, graders):
    record = _record(tmp_path, with_result=False)
    results = await grade_trial(record, graders)
    assert results[0].status == "fail"
    assert "missing from the sealed record" in " ".join(results[0].reasons)


async def test_grader_cannot_judge_on_degraded_transcript(tmp_path, graders):
    """A partial token count must block grading (cannot_judge), never
    execute on degraded evidence."""
    record = _record(
        tmp_path, result_value="hello", completeness=("ok", "partial")
    )
    results = await grade_trial(record, graders)
    assert results[0].status == "cannot_judge"
    assert results[0].score.valid is False
    assert "token_usage" in " ".join(results[0].reasons)


async def test_grader_cannot_judge_on_missing_transcript_fields(tmp_path, graders):
    record = _record(tmp_path, result_value="hello", completeness=("ok", "ok"))
    record = record.model_copy(update={"transcript_extra": None})
    results = await grade_trial(record, graders)
    assert results[0].status == "cannot_judge"
