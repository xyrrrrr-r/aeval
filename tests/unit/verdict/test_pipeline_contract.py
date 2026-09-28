"""Pipeline contract tests: logical artifact names + metrics persistence.

The grader-facing ``TrialRecord.artifacts`` dict is keyed by LOGICAL
collect names (``observable:result``, ``canonical_transcript``), not by
on-disk paths — the collection manifest is the only authoritative
path→name mapping, and an artifact the manifest does not name fails
closed. ``GradeResult.metrics`` (the trajectory metric outcomes) must
round-trip through the store.
"""

from __future__ import annotations

import json

import pytest

from aeval.contracts import (
    ArtifactRef,
    CollectionManifest,
    CollectOutcome,
    EvidenceBundle,
    GradeResult,
    MetricOutcome,
    OverlayIdentity,
    RunManifest,
    Score,
    VersionsBundle,
)
from aeval.store.sqlite import TrialStore
from aeval.verdict.pipeline import GradingPipelineError, logical_artifacts


def _ref(path: str) -> ArtifactRef:
    return ArtifactRef(
        media_type="application/json",
        sha256="a" * 64,
        size_bytes=1,
        path=path,
    )


def _manifest(pairs: list[tuple[str, str]]) -> CollectionManifest:
    return CollectionManifest(
        trial_id="t1",
        outcomes=[
            CollectOutcome(
                name=name,
                command=f"collect {name}",
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:01+00:00",
                exit_code=0,
                output_path=path,
                sha256="a" * 64,
            )
            for name, path in pairs
        ],
        artifacts=[_ref(path) for _, path in pairs],
        runtime_lock_digest="b" * 64,
    )


def _bundle(pairs: list[tuple[str, str]], *, extra_artifact: str | None = None):
    artifacts = {path: _ref(path) for _, path in pairs}
    if extra_artifact:
        artifacts[extra_artifact] = _ref(extra_artifact)
    return EvidenceBundle(
        trial_id="t1",
        stop_reason="agent_claimed_done",
        artifacts=artifacts,
        collection_manifest=_manifest(pairs),
    )


def test_logical_artifacts_rekeys_paths_to_names():
    bundle = _bundle(
        [
            ("canonical_transcript", "canonical_transcript.json"),
            ("observable:result", "observables/result.json"),
        ]
    )
    logical = logical_artifacts(bundle)
    assert sorted(logical) == ["canonical_transcript", "observable:result"]
    assert logical["observable:result"].path == "observables/result.json"


def test_logical_artifacts_fails_closed_on_unnamed_artifact():
    bundle = _bundle(
        [("canonical_transcript", "canonical_transcript.json")],
        extra_artifact="sneaky.json",
    )
    with pytest.raises(GradingPipelineError, match="no logical name"):
        logical_artifacts(bundle)


def test_logical_artifacts_passthrough_without_manifest():
    # Direct API use: no manifest → caller-authored keys stand as-is
    # (production bundles always carry one; the evidence gate enforces it).
    bundle = EvidenceBundle(
        trial_id="t1",
        stop_reason="agent_claimed_done",
        artifacts={"canonical_transcript": _ref("canonical_transcript.json")},
        collection_manifest=None,
    )
    assert logical_artifacts(bundle) == bundle.artifacts



def _seed_run(store: TrialStore, runtime_lock, run_id: str = "r") -> None:
    store.create_run(
        RunManifest(
            run_id=run_id,
            runtime_lock=runtime_lock,
            overlay=OverlayIdentity(
                suite_id="s",
                suite_version="0",
                overlay_digest="d" * 64,
                source_commit="9" * 40,
            ),
            versions=VersionsBundle(aeval_version="0.1.0"),
        )
    )


def test_store_roundtrips_metrics(tmp_path, runtime_lock):
    store = TrialStore(tmp_path / "store.sqlite3")
    _seed_run(store, runtime_lock)
    metrics = [
        MetricOutcome(
            name="step_efficiency",
            category="efficiency",
            status="ok",
            score=1.0,
            reasons=["within budget"],
        ),
        MetricOutcome(
            name="forbidden_access",
            category="integrity",
            status="violated",
            score=0.0,
            required=True,
            reasons=["read the tests"],
        ),
    ]
    result = GradeResult(
        grader_id="hello-trajectory",
        grader_version="v1",
        layer="trajectory",
        veto=True,
        score=Score(value=0.0),
        status="fail",
        reasons=["trajectory integrity violated"],
        metrics=metrics,
    )
    from aeval.contracts import RequirementBitmap, TrialCoordinates, TrialRecord

    record = TrialRecord(
        trial_id="t1",
        coordinates=TrialCoordinates(
            run_id="r", suite_id="s", suite_version="0", task_id="task", trial_index=0
        ),
        stop_reason="agent_claimed_done",
        requirements=RequirementBitmap(),
        artifacts={},
    )
    store.persist_trial_with_grades(record, [result])
    loaded = store.load_trial("t1")
    assert loaded is not None
    assert loaded.grades[0].metrics is not None
    assert [m.name for m in loaded.grades[0].metrics] == [
        "step_efficiency",
        "forbidden_access",
    ]
    assert loaded.grades[0].metrics[1].required is True
    store.close()


def test_store_roundtrips_metrics_none(tmp_path, runtime_lock):
    store = TrialStore(tmp_path / "store.sqlite3")
    _seed_run(store, runtime_lock)
    result = GradeResult(
        grader_id="hello-outcome",
        grader_version="v1",
        layer="outcome",
        score=Score(value=1.0),
        status="pass",
        reasons=["ok"],
    )
    from aeval.contracts import RequirementBitmap, TrialCoordinates, TrialRecord

    record = TrialRecord(
        trial_id="t2",
        coordinates=TrialCoordinates(
            run_id="r", suite_id="s", suite_version="0", task_id="task", trial_index=0
        ),
        stop_reason="agent_claimed_done",
        requirements=RequirementBitmap(),
        artifacts={},
    )
    store.persist_trial_with_grades(record, [result])
    loaded = store.load_trial("t2")
    assert loaded is not None
    assert loaded.grades[0].metrics is None
    store.close()
