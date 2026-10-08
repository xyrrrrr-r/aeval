"""Finalization gate tests.

The gate between "Harbor exited 0" and "the aeval chain completed":
every incompleteness class must refuse to seal, and only a fully
classified, store-backed run seals and survives strict recompute.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeval.bundle.finalize import FinalizeError, finalize_run
from aeval.bundle.manifest import (
    ManifestTamperError,
    intent_digest,
    seal_run_manifest,
    write_intent_manifest,
)
from aeval.contracts import (
    ExclusionSummary,
    GradeResult,
    OverlayIdentity,
    RunManifest,
    Score,
    TrialCoordinates,
    TrialRecord,
    VersionsBundle,
)
from aeval.store.sqlite import TrialStore


def _manifest(runtime_lock, run_id="run-x") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def _record(run_id: str, trial_id: str, *, verdict="pass", index=0) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id,
        coordinates=TrialCoordinates(
            run_id=run_id, suite_id="s", suite_version="1",
            task_id="task", trial_index=index,
        ),
        stop_reason="agent_exit_0",
        verdict=verdict,
        grades=[GradeResult(
            grader_id="g", grader_version="v1", layer="outcome",
            score=Score(value=1.0), status=verdict if verdict in ("pass", "fail") else "cannot_judge",
        )],
    )


class _RunFixture:
    """A run dir + store holding one complete, classified trial."""

    def __init__(self, tmp_path: Path, runtime_lock, *, trial_verdict="pass"):
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir(parents=True)
        self.manifest = _manifest(runtime_lock)
        self.manifest_path = write_intent_manifest(self.manifest, self.run_dir)
        (self.run_dir / "harbor-job.json").write_text("{}\n", encoding="utf-8")
        (self.run_dir / "runtime_lock.json").write_text(
            runtime_lock.model_dump_json(), encoding="utf-8"
        )
        self.store_path = tmp_path / "store.sqlite3"
        self.store = TrialStore(self.store_path)
        self.store.create_run(self.manifest)
        record = _record(self.manifest.run_id, "trial-1", verdict=trial_verdict)
        self.store.persist_trial_with_grades(record, record.grades)
        self.write_summary(trials={
            "trial-1": {"phase": "ended", "stop_reason": "agent_exit_0"},
        }, unobserved=0)

    def write_summary(self, *, trials, unobserved) -> None:
        summary = {
            "run_id": self.manifest.run_id,
            "unobserved_trials": unobserved,
            "trials": trials,
        }
        (self.run_dir / "aeval_run_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

    def close(self) -> None:
        self.store.close()


@pytest.fixture()
def complete_run(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    yield fixture
    fixture.close()


def test_finalize_seals_complete_run(complete_run):
    report = finalize_run(complete_run.run_dir, complete_run.store_path)
    assert report.sealed is True
    assert report.recorded_trials == 1
    assert report.seal_digest
    assert report.attestation_digest
    assert report.attested_files >= 3  # manifest, config, lock, summary...
    manifest = json.loads(
        (complete_run.run_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["sealed"] is True
    assert manifest["exclusions"]["total"] == 1
    assert manifest["exclusions"]["valid"] == 1


def test_finalize_missing_summary_refused(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    fixture.close()
    (fixture.run_dir / "aeval_run_summary.json").unlink()
    with pytest.raises(FinalizeError, match="run summary missing"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_finalize_unobserved_trials_refused(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    fixture.write_summary(
        trials={"trial-1": {"phase": "ended"}}, unobserved=2,
    )
    fixture.close()
    with pytest.raises(FinalizeError, match="never observed"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_finalize_nonterminal_trial_refused(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    fixture.write_summary(
        trials={"trial-1": {"phase": "running"}}, unobserved=0,
    )
    fixture.close()
    with pytest.raises(FinalizeError, match="not terminal"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_finalize_unrecorded_trial_refused(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    fixture.write_summary(
        trials={
            "trial-1": {"phase": "ended"},
            "trial-2": {"phase": "ended"},  # never graded/persisted
        },
        unobserved=0,
    )
    fixture.close()
    with pytest.raises(FinalizeError, match="without a store record"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_finalize_unclassified_verdict_refused(tmp_path, runtime_lock):
    """A store record with verdict=None is not a classification."""
    fixture = _RunFixture(tmp_path, runtime_lock)
    record = _record(fixture.manifest.run_id, "trial-1", verdict=None)
    fixture.store._conn.execute("DELETE FROM rubric_results WHERE trial_id = 'trial-1'")
    fixture.store._conn.execute("DELETE FROM trials WHERE trial_id = 'trial-1'")
    fixture.store._conn.commit()
    fixture.store.persist_trial_with_grades(record, [])
    fixture.close()
    with pytest.raises(FinalizeError, match="no final classification"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_finalize_run_not_in_store_refused(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    fixture.close()
    with pytest.raises(FinalizeError, match="never recorded in the store"):
        finalize_run(fixture.run_dir, tmp_path / "empty.sqlite3")


def test_finalize_intent_rewrite_refused(tmp_path, runtime_lock):
    """Rewriting the intent manifest before seal is detected."""
    fixture = _RunFixture(tmp_path, runtime_lock)
    data = json.loads(fixture.manifest_path.read_text(encoding="utf-8"))
    data["budget_enforcement_point"] = "fabricated"
    fixture.manifest_path.write_text(json.dumps(data), encoding="utf-8")
    fixture.close()
    with pytest.raises(FinalizeError, match="rewritten"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_finalize_honest_fail_verdict_still_seals(tmp_path, runtime_lock):
    """A failing trial is a complete outcome, not an incomplete run."""
    fixture = _RunFixture(tmp_path, runtime_lock, trial_verdict="fail")
    fixture.close()
    report = finalize_run(fixture.run_dir, fixture.store_path)
    assert report.sealed is True


def test_finalize_double_seal_refused(tmp_path, runtime_lock):
    fixture = _RunFixture(tmp_path, runtime_lock)
    fixture.close()
    finalize_run(fixture.run_dir, fixture.store_path)
    with pytest.raises(FinalizeError, match="already sealed"):
        finalize_run(fixture.run_dir, fixture.store_path)


def test_seal_intent_digest_detects_rewrite(tmp_path, runtime_lock):
    """Direct unit test of the seal-time intent binding."""
    run_dir = tmp_path / "run"
    manifest = _manifest(runtime_lock)
    path = write_intent_manifest(manifest, run_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = intent_digest(data)
    # seal with the correct digest succeeds
    seal_run_manifest(path, ExclusionSummary(), expected_intent_digest=expected)
    assert json.loads(path.read_text(encoding="utf-8"))["sealed"] is True

    # a manifest rewritten after intent time is refused at seal
    run_dir2 = tmp_path / "run2"
    path2 = write_intent_manifest(_manifest(runtime_lock), run_dir2)
    data2 = json.loads(path2.read_text(encoding="utf-8"))
    expected2 = intent_digest(data2)  # digest as recorded at run creation
    data2["config_hash"] = "rewritten"
    path2.write_text(json.dumps(data2), encoding="utf-8")
    with pytest.raises(ManifestTamperError, match="rewritten after run creation"):
        seal_run_manifest(
            path2, ExclusionSummary(), expected_intent_digest=expected2,
        )


def test_store_roundtrip_run_manifest(tmp_path, runtime_lock):
    store = TrialStore(tmp_path / "s.sqlite3")
    try:
        manifest = _manifest(runtime_lock)
        store.create_run(manifest)
        loaded = store.load_run_manifest(manifest.run_id)
        assert loaded.run_id == manifest.run_id
        assert loaded.overlay.suite_id == manifest.overlay.suite_id
        expected = intent_digest(json.loads(manifest.model_dump_json(exclude_none=True)))
        assert store.run_intent_digest(manifest.run_id) == expected
    finally:
        store.close()
    with pytest.raises(KeyError):
        TrialStore(tmp_path / "s.sqlite3").load_run_manifest("missing-run")
