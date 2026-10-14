"""The report must read the trial-dir layout the writers publish.

A fully sealed run reported ``证据覆盖：0/3`` about evidence it held:
``aeval quickstart`` links each trial's evidence directory under
``<workspace>/runs/trials`` (``quickstart._link_evidence``) while the report
looked only in ``<workspace>/trials``. These tests pin the reader to every
layout aeval publishes or tells an operator to publish, pin the precedence
between them, and pin the three failure reasons apart so a真缺目录 is never
confused with a目录在但无清单.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harbor.models.trajectories import Agent, FinalMetrics, Trajectory

from aeval.cli import (
    _iter_sealed_evidence,
    _missing_evidence_reason,
    _scan_trial_dirs,
    _trial_dir_candidates,
    _trial_dirs,
)
from aeval.contracts import ArtifactRef, CanonicalTranscript, TrialRecord
from aeval.verdict.trajectory.stats import collect_stats
from tests.unit.verdict.test_stats import sample_steps


def _store(workspace: Path) -> Path:
    """A store at ``<workspace>/run.sqlite3`` — ``store.parent`` is the workspace."""
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace / "run.sqlite3"


def _trial_dir(root: Path, name: str, trial_id: str) -> Path:
    trial = root / name
    trial.mkdir(parents=True, exist_ok=True)
    (trial / "collection_manifest.json").write_text(
        json.dumps({"schema_version": 1, "trial_id": trial_id}), encoding="utf-8"
    )
    return trial


def _transcript_bytes() -> bytes:
    transcript = CanonicalTranscript(
        atif=Trajectory(
            agent=Agent(name="dsh", version="test"),
            steps=sample_steps(),
            final_metrics=FinalMetrics(
                total_prompt_tokens=3650, total_completion_tokens=80,
                total_cached_tokens=100,
            ),
        ),
        stop_reason="agent_claimed_done",
    )
    return transcript.model_dump_json().encode("utf-8")


def _record(trial_id: str, trial_dir: Path, content: bytes) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id,
        coordinates={
            "run_id": "r1", "suite_id": "s", "suite_version": "1",
            "task_id": "alpha.one", "trial_index": 0,
        },
        stop_reason="agent_claimed_done",
        artifacts={
            "canonical_transcript": ArtifactRef(
                media_type="application/json",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                path="canonical_transcript.json",
            )
        },
        artifact_base=str(trial_dir),
    )


# --------------------------------------------------------------------------- #
# (b) reader ↔ writer path consistency
# --------------------------------------------------------------------------- #


def test_the_quickstart_layout_under_runs_trials_is_read(tmp_path):
    """``<ws>/runs/trials`` is what quickstart writes — the report must see it."""
    workspace = tmp_path / "ws"
    store = _store(workspace)
    trial = _trial_dir(workspace / "runs" / "trials", "hello-world__WS6Zd7V", "t1")
    assert _trial_dirs(store) == {"t1": trial}


def test_the_historical_layout_next_to_the_store_still_resolves(tmp_path):
    workspace = tmp_path / "ws"
    store = _store(workspace)
    trial = _trial_dir(workspace / "trials", "some-driver-name", "t1")
    assert _trial_dirs(store) == {"t1": trial}


def test_the_doctor_layout_under_the_run_dir_resolves(tmp_path):
    """``aeval doctor`` tells operators to link into ``<run-dir>/trials``."""
    workspace = tmp_path / "ws"
    store = _store(workspace)
    trial = _trial_dir(workspace / "run" / "trials", "linked", "t1")
    assert _trial_dirs(store) == {"t1": trial}


def test_the_candidate_order_is_the_documented_one(tmp_path):
    workspace = tmp_path / "ws"
    store = _store(workspace)
    assert _trial_dir_candidates(store) == (
        workspace / "trials",
        workspace / "runs" / "trials",
        workspace / "run" / "trials",
    )


def test_a_repeated_trial_id_takes_the_first_layout(tmp_path):
    workspace = tmp_path / "ws"
    store = _store(workspace)
    first = _trial_dir(workspace / "trials", "old", "t1")
    _trial_dir(workspace / "runs" / "trials", "new", "t1")
    assert _trial_dirs(store) == {"t1": first}


def test_layouts_are_a_union_not_a_choice(tmp_path):
    """A partially populated layout must not hide a populated one."""
    workspace = tmp_path / "ws"
    store = _store(workspace)
    old = _trial_dir(workspace / "trials", "a", "t1")
    new = _trial_dir(workspace / "runs" / "trials", "b", "t2")
    assert _trial_dirs(store) == {"t1": old, "t2": new}


def test_published_dirs_are_reported_by_the_scan(tmp_path):
    workspace = tmp_path / "ws"
    store = _store(workspace)
    scan = _scan_trial_dirs(store)
    assert scan.published == () and scan.entries == 0
    (workspace / "runs" / "trials").mkdir(parents=True)
    scan = _scan_trial_dirs(store)
    assert scan.published == (workspace / "runs" / "trials",)


def test_a_sealed_trial_under_runs_trials_becomes_evidence(tmp_path):
    """End to end through the production reader: mapping → verified load → stats.

    Before the path fix this yielded ``(record, None, "试次目录缺失")``.
    """
    workspace = tmp_path / "ws"
    store = _store(workspace)
    trial = _trial_dir(workspace / "runs" / "trials", "hello-world__WS6Zd7V", "t1")
    content = _transcript_bytes()
    (trial / "canonical_transcript.json").write_bytes(content)

    rows = list(_iter_sealed_evidence(store, [_record("t1", trial, content)]))

    assert len(rows) == 1
    record, evidence, reason = rows[0]
    assert reason is None and evidence is not None
    stats = collect_stats(record, evidence)
    assert stats.total_prompt_tokens == 3650
    assert stats.total_completion_tokens == 80
    assert stats.tool_calls_total == 2


# --------------------------------------------------------------------------- #
# (a) five facts, five reasons
#
# These mirror the Lead's independent verifier: case B removes a whole trial
# directory (its link survives, so the link breaks), case C removes only the
# transcript (that one keeps the loader's reason), case D removes only the
# manifest. The reasons must be pairwise distinct or an operator cannot tell
# which fix applies.
# --------------------------------------------------------------------------- #


def test_no_published_directory_is_reported_as_missing(tmp_path):
    store = _store(tmp_path / "ws")
    assert _missing_evidence_reason(_scan_trial_dirs(store)) == "试次目录缺失"


def test_an_empty_published_directory_is_not_a_manifest_problem(tmp_path):
    workspace = tmp_path / "ws"
    store = _store(workspace)
    (workspace / "runs" / "trials").mkdir(parents=True)
    reason = _missing_evidence_reason(_scan_trial_dirs(store))
    assert "没有任何条目" in reason
    assert "无 collection_manifest" not in reason


def test_a_broken_link_says_the_directory_is_gone(tmp_path):
    """Case B: the trial dir was removed, its link is left dangling."""
    workspace = tmp_path / "ws"
    store = _store(workspace)
    links = workspace / "runs" / "trials"
    links.mkdir(parents=True)
    (links / "gone__abcdefg").symlink_to(workspace / "run" / "harbor" / "gone")
    reason = _missing_evidence_reason(_scan_trial_dirs(store))
    assert "试次目录缺失" in reason and "链接" in reason
    assert "无 collection_manifest" not in reason


def test_a_directory_without_a_manifest_says_so(tmp_path):
    """Case D: dir and transcript kept, manifest removed."""
    workspace = tmp_path / "ws"
    store = _store(workspace)
    _trial_dir(workspace / "runs" / "trials", "unmanifested", "t1")
    (workspace / "runs" / "trials" / "unmanifested" / "collection_manifest.json").unlink()
    reason = _missing_evidence_reason(_scan_trial_dirs(store))
    assert "无 collection_manifest" in reason
    assert "试次目录缺失" not in reason


def test_a_manifest_that_does_not_match_says_so(tmp_path):
    workspace = tmp_path / "ws"
    store = _store(workspace)
    _trial_dir(workspace / "runs" / "trials", "some-other-trial", "some-other-id")
    reason = _missing_evidence_reason(_scan_trial_dirs(store))
    assert "trial_id 对不上" in reason
    assert "试次目录缺失" not in reason and "无 collection_manifest" not in reason


def test_the_reasons_are_distinct_across_states(tmp_path):
    """Every published state yields its own reason (each built in isolation)."""

    def fresh(name: str) -> tuple[Path, Path]:
        workspace = tmp_path / name
        return _store(workspace), workspace

    store, _ = fresh("missing")
    missing = _missing_evidence_reason(_scan_trial_dirs(store))

    store, workspace = fresh("empty")
    (workspace / "runs" / "trials").mkdir(parents=True)
    empty = _missing_evidence_reason(_scan_trial_dirs(store))

    store, workspace = fresh("broken")
    links = workspace / "runs" / "trials"
    links.mkdir(parents=True)
    (links / "gone__abcdefg").symlink_to(workspace / "run" / "harbor" / "gone")
    broken = _missing_evidence_reason(_scan_trial_dirs(store))

    store, workspace = fresh("unmanifested")
    _trial_dir(workspace / "runs" / "trials", "d", "t1")
    (workspace / "runs" / "trials" / "d" / "collection_manifest.json").unlink()
    unmanifested = _missing_evidence_reason(_scan_trial_dirs(store))

    store, workspace = fresh("unmatched")
    _trial_dir(workspace / "runs" / "trials", "other", "other-id")
    unmatched = _missing_evidence_reason(_scan_trial_dirs(store))

    reasons = {missing, empty, broken, unmanifested, unmatched}
    assert len(reasons) == 5, reasons
