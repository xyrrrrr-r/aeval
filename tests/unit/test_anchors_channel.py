"""Sealed rubric-anchors channel tests (integration P2, §5.6 mid-term).

The channel has three links, each tested end to end: the suite declares
``verdict.anchors: task_anchors`` → collection seals the suite's
``rubric/task_anchors.json`` into the trial at the fixed path → the
trajectory grader loads the anchors from the sealed copy (digest
verified), so a regrade depends only on sealed bytes. Suites that do
not declare the channel keep a byte-identical plan (opt-in).
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from aeval.contracts import RuntimeLock, TrialCoordinates, TrialRecord, ArtifactRef
from aeval.hooks.collection import CollectionError, collect_trial_evidence
from aeval.hooks.collectors import produce_task_anchors
from aeval.hooks.evidence import build_required_collect_plan
from aeval.verdict.trajectory.base import (
    SealedArtifactError,
    load_sealed_anchors,
)

from tests.unit.test_collection_producer import FakeAgent, FakeEnvironment


def _anchors_bytes() -> bytes:
    return json.dumps(
        {
            "identity-intro": {
                "identity_probes": ["你是谁"],
                "identity_keywords": ["助手", "AI"],
            },
            "secret-guard": {},
        },
        indent=2, ensure_ascii=False,
    ).encode("utf-8")


def _declaring_suite(tmp_path: Path, *, with_file: bool = True):
    if with_file:
        rubric_dir = tmp_path / "suite" / "rubric"
        rubric_dir.mkdir(parents=True, exist_ok=True)
        (rubric_dir / "task_anchors.json").write_bytes(_anchors_bytes())
    return SimpleNamespace(
        suite_dir=str(tmp_path / "suite"),
        overlay=SimpleNamespace(
            observables=[
                SimpleNamespace(
                    name="result", type="string", source="file:/workspace/result"
                )
            ],
            verdict=SimpleNamespace(anchors="task_anchors"),
        ),
    )


def _plain_suite(tmp_path: Path):
    suite = _declaring_suite(tmp_path)
    suite.overlay.verdict = SimpleNamespace(anchors=None)
    return suite


# --- plan gating -----------------------------------------------------------


def test_undeclaring_suites_keep_a_task_anchors_free_plan(tmp_path):
    plan = build_required_collect_plan(_plain_suite(tmp_path))
    assert "task_anchors" not in plan


def test_declaring_suites_get_the_anchors_output_in_their_plan(tmp_path):
    plan = build_required_collect_plan(_declaring_suite(tmp_path))
    assert "task_anchors" in plan


# --- collection ------------------------------------------------------------


async def test_collection_seals_the_suite_anchors_at_the_fixed_path(
    tmp_path, runtime_lock
):
    suite = _declaring_suite(tmp_path)
    agent = FakeAgent(tmp_path, "session-1")
    manifest = await collect_trial_evidence(
        trial_dir=tmp_path / "trial",
        trial_id="trial-1",
        suite=suite,
        environment=FakeEnvironment(),
        agent=agent,
        runtime_lock=runtime_lock,
        session_id="session-1",
    )
    sealed = tmp_path / "trial" / "rubric" / "task_anchors.json"
    assert sealed.is_file()
    assert sealed.read_bytes() == _anchors_bytes()  # verbatim, not re-serialized
    # the manifest records the output at its fixed path with its digest
    outcome = next(o for o in manifest.outcomes if o.name == "task_anchors")
    assert outcome.output_path == "rubric/task_anchors.json"
    assert outcome.sha256 == sha256(_anchors_bytes()).hexdigest()
    assert any(
        a.path == "rubric/task_anchors.json" and a.sha256 == outcome.sha256
        for a in manifest.artifacts
    )


async def test_collection_skips_the_channel_when_not_declared(tmp_path, runtime_lock):
    suite = _plain_suite(tmp_path)
    agent = FakeAgent(tmp_path, "session-1")
    manifest = await collect_trial_evidence(
        trial_dir=tmp_path / "trial",
        trial_id="trial-1",
        suite=suite,
        environment=FakeEnvironment(),
        agent=agent,
        runtime_lock=runtime_lock,
        session_id="session-1",
    )
    assert not (tmp_path / "trial" / "rubric").exists()
    assert not any(o.name == "task_anchors" for o in manifest.outcomes)


async def test_declared_but_missing_rubric_file_fails_closed(tmp_path, runtime_lock):
    suite = _declaring_suite(tmp_path, with_file=False)
    agent = FakeAgent(tmp_path, "session-1")
    with pytest.raises(CollectionError, match="cannot be read"):
        await collect_trial_evidence(
            trial_dir=tmp_path / "trial",
            trial_id="trial-1",
            suite=suite,
            environment=FakeEnvironment(),
            agent=agent,
            runtime_lock=runtime_lock,
            session_id="session-1",
        )


def test_produce_task_anchors_writes_atomically_with_digest(tmp_path):
    outcome, ref = produce_task_anchors(tmp_path, _anchors_bytes())
    assert outcome.name == "task_anchors"
    assert outcome.output_path == "rubric/task_anchors.json"
    assert outcome.sha256 == sha256(_anchors_bytes()).hexdigest()
    assert ref.sha256 == sha256(_anchors_bytes()).hexdigest()
    assert ref.path == "rubric/task_anchors.json"


# --- the grading side -------------------------------------------------------


def _record_with_anchors(tmp_path, *, content: bytes | None, digest: str | None = None):
    artifacts = {}
    if content is not None:
        (tmp_path / "rubric").mkdir(exist_ok=True)
        (tmp_path / "rubric" / "task_anchors.json").write_bytes(content)
        artifacts["task_anchors"] = ArtifactRef(
            media_type="application/json",
            sha256=digest or sha256(content).hexdigest(),
            size_bytes=len(content),
            path="rubric/task_anchors.json",
        )
    return TrialRecord(
        trial_id="t1",
        coordinates=TrialCoordinates(
            run_id="r", suite_id="s", suite_version="0",
            task_id="identity-intro", trial_index=0,
        ),
        stop_reason="agent_claimed_done",
        artifacts=artifacts,
        artifact_base=str(tmp_path),
    )


def test_load_sealed_anchors_round_trips_the_table(tmp_path):
    record = _record_with_anchors(tmp_path, content=_anchors_bytes())
    anchors = load_sealed_anchors(record)
    assert anchors["identity-intro"]["identity_probes"] == ["你是谁"]
    assert "secret-guard" in anchors


def test_load_sealed_anchors_missing_artifact_is_cannot_judge_material(tmp_path):
    record = _record_with_anchors(tmp_path, content=None)
    with pytest.raises(SealedArtifactError, match="never sealed"):
        load_sealed_anchors(record)


def test_load_sealed_anchors_rejects_a_digest_mismatch(tmp_path):
    record = _record_with_anchors(
        tmp_path, content=_anchors_bytes(), digest="0" * 64
    )
    with pytest.raises(SealedArtifactError, match="digest mismatch"):
        load_sealed_anchors(record)


def test_load_sealed_anchors_rejects_a_non_object_table(tmp_path):
    record = _record_with_anchors(tmp_path, content=b'["not", "a", "table"]')
    with pytest.raises(SealedArtifactError, match="not a JSON object"):
        load_sealed_anchors(record)


def test_transcript_errors_still_carry_the_transcript_name(tmp_path):
    """The refactor kept the established exception surface: transcript
    loading failures raise SealedTranscriptError (now a subclass of the
    shared SealedArtifactError), so existing callers keep catching it."""
    from aeval.verdict.trajectory.base import (
        SealedTranscriptError, load_sealed_transcript,
    )

    record = _record_with_anchors(tmp_path, content=_anchors_bytes())
    with pytest.raises(SealedTranscriptError, match="never sealed"):
        load_sealed_transcript(record)
    with pytest.raises(SealedArtifactError):
        load_sealed_transcript(record)
