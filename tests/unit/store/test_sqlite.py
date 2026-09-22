"""SQLite TrialStore tests (plan §7 row 10): atomic, unique, idempotent."""

from __future__ import annotations

import pytest

from aeval.contracts import (
    GradeResult,
    OverlayIdentity,
    RunManifest,
    Score,
    TrialRecord,
    VersionsBundle,
)
from aeval.store.sqlite import StoreConflictError, TrialStore


def _manifest(run_id: str, runtime_lock) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="refund-policy",
            suite_version="1.4.0",
            overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def _record(run_id: str, index: int = 0, trial_id: str | None = None) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id or f"{run_id}-t{index}",
        coordinates={
            "run_id": run_id,
            "suite_id": "refund-policy",
            "suite_version": "1.4.0",
            "task_id": "refund-0123",
            "trial_index": index,
        },
        stop_reason="agent_exit_0",
        verdict="pass",
    )


def _grade(grader_id="g", version="v1", status="pass", value=1.0) -> GradeResult:
    return GradeResult(
        grader_id=grader_id,
        grader_version=version,
        layer="outcome",
        score=Score(value=value) if value is not None else
              Score(value=None, valid=False, invalid_reasons=["x"]),
        status=status,
    )


@pytest.fixture()
def store(tmp_path, runtime_lock):
    s = TrialStore(tmp_path / "store.sqlite3")
    s.create_run(_manifest("run-1", runtime_lock))
    yield s
    s.close()


def test_run_ids_are_never_reused(store, runtime_lock):
    with pytest.raises(StoreConflictError, match="never reused"):
        store.create_run(_manifest("run-1", runtime_lock))


def test_trial_roundtrip_preserves_record(store):
    record = _record("run-1")
    store.persist_trial(record)
    store.persist_grades(record.trial_id, [_grade(), _grade("g2", "v2", "fail", 0.0)])
    loaded = store.load_trial(record.trial_id)
    assert loaded.coordinates == record.coordinates
    assert loaded.stop_reason == record.stop_reason
    assert loaded.verdict == "pass"
    assert {g.grader_id for g in loaded.grades} == {"g", "g2"}


def test_duplicate_trial_coordinates_rejected(store):
    store.persist_trial(_record("run-1", index=0, trial_id="t-a"))
    with pytest.raises(StoreConflictError, match="conflicts with an existing record"):
        store.persist_trial(_record("run-1", index=0, trial_id="t-b"))


def test_same_trial_id_twice_rejected(store):
    store.persist_trial(_record("run-1", trial_id="t-dup"))
    with pytest.raises(StoreConflictError):
        store.persist_trial(_record("run-1", trial_id="t-dup"))


def test_trial_for_unknown_run_rejected(store):
    with pytest.raises(StoreConflictError):
        store.persist_trial(_record("no-such-run"))


def test_same_grader_version_twice_rejected_new_version_ok(store):
    record = _record("run-1")
    store.persist_trial(record)
    store.persist_grades(record.trial_id, [_grade()])
    with pytest.raises(StoreConflictError, match="new grader version"):
        store.persist_grades(record.trial_id, [_grade()])
    # regrade under a new version is a new row, not a mutation
    store.persist_grades(record.trial_id, [_grade(version="v2", status="fail", value=0.0)])
    loaded = store.load_trial(record.trial_id)
    by_version = {g.grader_version: g for g in loaded.grades}
    assert by_version["v1"].status == "pass"
    assert by_version["v2"].status == "fail"


def test_load_missing_trial_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.load_trial("nope")


def test_list_trials_filters_by_run(tmp_path, runtime_lock):
    store = TrialStore(tmp_path / "s2.sqlite3")
    try:
        store.create_run(_manifest("run-1", runtime_lock))
        store.create_run(_manifest("run-2", runtime_lock))
        store.persist_trial(_record("run-1", index=0, trial_id="r1-t0"))
        store.persist_trial(_record("run-1", index=1, trial_id="r1-t1"))
        store.persist_trial(_record("run-2", index=0, trial_id="r2-t0"))
        listed = store.list_trials(["run-1"])
        assert {t.trial_id for t in listed} == {"r1-t0", "r1-t1"}
        both = store.list_trials(["run-1", "run-2"])
        assert len(both) == 3
    finally:
        store.close()
