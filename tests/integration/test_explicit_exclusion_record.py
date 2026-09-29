"""D52: an unverifiable trial must leave an explicit, reasoned exclusion.

Found on the real chain: the DSH agent died at the single-response token
cap (``turn_end`` reason ``max-tokens``), so no official session record
existed; the evidence gate fired correctly, but the trial then left NO
store record at all. ``finalize_run`` refused to seal the whole run
("trial(s) without a store record") and the evidence of every other
trial was lost with it.

The fix keeps the fail-closed gate — the verifier still never runs for an
unverifiable trial, so the valid denominator stays strict — and adds a
``cannot_judge`` record carrying the reason, so the seal reports an
explicit exclusion instead of an unexplained gap.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from aeval.bundle.finalize import FinalizeError, finalize_run
from aeval.bundle.manifest import write_intent_manifest
from aeval.contracts import (
    OverlayIdentity,
    RunManifest,
    TrialCoordinates,
    TrialRecord,
    VersionsBundle,
)
from aeval.hooks.plugin import _record_unjudgeable_exclusion
from aeval.store.sqlite import TrialStore

REASON = (
    "the trial's agent exposes no DSH session — the official session "
    "record cannot be collected"
)


def _manifest(runtime_lock, run_id="run-d52") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def _context(store_path: Path, run_id: str) -> SimpleNamespace:
    counter = itertools.count()
    return SimpleNamespace(
        store_path=store_path,
        run_id=run_id,
        suite=SimpleNamespace(id="s", version="1"),
        next_trial_index=lambda: next(counter),
    )


def _state(trial_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        trial_id=trial_id,
        stop_reason="infra_error",
        baseline_ok=True,
        evidence_issues=[REASON],
        infra_invalid_reasons=[REASON],
    )


def _empty_run(tmp_path: Path, runtime_lock) -> tuple[RunManifest, Path, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    manifest = _manifest(runtime_lock)
    write_intent_manifest(manifest, run_dir)
    (run_dir / "harbor-job.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "runtime_lock.json").write_text(
        runtime_lock.model_dump_json(), encoding="utf-8"
    )
    store_path = tmp_path / "store.sqlite3"
    store = TrialStore(store_path)
    store.create_run(manifest)
    store.close()
    return manifest, run_dir, store_path


def _write_summary(run_dir: Path, manifest: RunManifest, trials: dict) -> None:
    (run_dir / "aeval_run_summary.json").write_text(
        json.dumps(
            {"run_id": manifest.run_id, "unobserved_trials": 0, "trials": trials}
        ),
        encoding="utf-8",
    )


def test_exclusion_record_carries_the_reason(tmp_path, runtime_lock):
    """The trial is recorded as cannot_judge WITH the reason attached."""
    manifest, _, store_path = _empty_run(tmp_path, runtime_lock)
    state = _state("trial-1")

    recorded = asyncio.run(
        _record_unjudgeable_exclusion(
            SimpleNamespace(task_name="hello-world"),
            _context(store_path, manifest.run_id),
            state,
            REASON,
        )
    )
    assert recorded is True

    store = TrialStore(store_path)
    try:
        record = store.load_trial("trial-1")
    finally:
        store.close()
    assert record.verdict == "cannot_judge"
    assert record.stop_reason == "infra_error"
    payload = record.transcript_extra["aeval"]
    assert payload["exclusion_reason"] == REASON
    assert payload["evidence_issues"] == [REASON]


def test_exclusion_record_never_overwrites_a_graded_trial(tmp_path, runtime_lock):
    """A trial that was already graded keeps its own verdict."""
    manifest, _, store_path = _empty_run(tmp_path, runtime_lock)
    store = TrialStore(store_path)
    store.persist_trial_with_grades(
        TrialRecord(
            trial_id="trial-1",
            coordinates=TrialCoordinates(
                run_id=manifest.run_id, suite_id="s", suite_version="1",
                task_id="hello-world", trial_index=0,
            ),
            stop_reason="agent_exit_0",
            verdict="pass",
        ),
        [],
    )
    store.close()

    asyncio.run(
        _record_unjudgeable_exclusion(
            SimpleNamespace(task_name="hello-world"),
            _context(store_path, manifest.run_id),
            _state("trial-1"),
            REASON,
        )
    )
    store = TrialStore(store_path)
    try:
        assert store.load_trial("trial-1").verdict == "pass"
    finally:
        store.close()


def test_run_with_explicit_exclusion_still_seals(tmp_path, runtime_lock):
    """The seal reports the exclusion explicitly; the valid set stays strict."""
    manifest, run_dir, store_path = _empty_run(tmp_path, runtime_lock)
    store = TrialStore(store_path)
    store.persist_trial_with_grades(
        TrialRecord(
            trial_id="trial-ok",
            coordinates=TrialCoordinates(
                run_id=manifest.run_id, suite_id="s", suite_version="1",
                task_id="hello-world", trial_index=0,
            ),
            stop_reason="agent_exit_0",
            verdict="pass",
        ),
        [],
    )
    store.close()
    asyncio.run(
        _record_unjudgeable_exclusion(
            SimpleNamespace(task_name="sqlite-db-truncate"),
            _context(store_path, manifest.run_id),
            _state("trial-excluded"),
            REASON,
        )
    )
    _write_summary(
        run_dir,
        manifest,
        {
            "trial-ok": {"phase": "ended", "stop_reason": "agent_exit_0"},
            "trial-excluded": {"phase": "ended", "stop_reason": "infra_error"},
        },
    )

    report = finalize_run(run_dir, store_path)
    assert report.sealed is True
    assert report.recorded_trials == 2
    assert report.exclusions.valid == 1
    assert report.exclusions.total == 2
    assert report.exclusions.excluded_trial_ids["cannot_judge"] == ["trial-excluded"]
    assert "trial-excluded" in report.exclusions.excluded_trial_ids["infra_invalid"]
    sealed = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert sealed["sealed"] is True
    assert sealed["exclusions"]["valid"] == 1


def test_reason_less_gap_still_refuses_to_seal(tmp_path, runtime_lock):
    """The fail-closed rail is intact: an unrecorded trial never seals."""
    manifest, run_dir, store_path = _empty_run(tmp_path, runtime_lock)
    _write_summary(
        run_dir,
        manifest,
        {"trial-missing": {"phase": "ended", "stop_reason": "infra_error"}},
    )
    try:
        finalize_run(run_dir, store_path)
    except FinalizeError as exc:
        assert "without a store record" in str(exc)
    else:  # pragma: no cover - the rail must fire
        raise AssertionError("a record-less trial must never seal")
