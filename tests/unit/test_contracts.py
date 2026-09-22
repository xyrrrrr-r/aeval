"""Contracts tests: the record shapes everything else depends on."""

from __future__ import annotations

import pytest
import pydantic

from harbor.models.trajectories import Agent, Step, Trajectory

from aeval.contracts import (
    ArtifactRef,
    BundleDescriptor,
    CanonicalTranscript,
    CompletenessRecord,
    FieldCompleteness,
    GradeResult,
    ImageIdentity,
    RequirementBitmap,
    RunManifest,
    Score,
    TrialRecord,
    canonical_json,
)

from tests.conftest import FIXTURES


def _atif() -> Trajectory:
    return Trajectory(
        agent=Agent(name="dsh", version="0.1.7-alpha.1"),
        steps=[Step(step_id=1, source="user", message="hi")],
    )


def test_requirement_bitmap_fixed_six():
    bitmap = RequirementBitmap()
    assert bitmap.satisfied_count == 0
    assert not bitmap.all_satisfied
    full = RequirementBitmap(
        input_complete=True,
        agent_finished=True,
        integration_valid=True,
        render_valid=True,
        judge_finished=True,
        artifact_schema_ok=True,
    )
    assert full.all_satisfied
    assert set(full.to_dict()) == {
        "input_complete", "agent_finished", "integration_valid",
        "render_valid", "judge_finished", "artifact_schema_ok",
    }


def test_canonical_transcript_roundtrip_preserves_aeval_extra():
    ct = CanonicalTranscript.build(
        atif=_atif(),
        stop_reason="budget_exhausted",
        evidence_uri="file://trial/session",
        completeness=CompletenessRecord(
            fields=[FieldCompleteness(field="events", status="ok")]
        ),
    )
    data = ct.to_json_dict()
    # serialized form is a self-contained ATIF document
    assert data["extra"]["aeval"]["stop_reason"] == "budget_exhausted"
    restored = CanonicalTranscript.from_json_dict(data)
    assert restored.stop_reason == "budget_exhausted"
    assert restored.evidence_uri == "file://trial/session"
    assert restored.completeness.status_of("events") == "ok"
    # vendor extras survive the roundtrip untouched
    assert restored.atif.steps[0].message == "hi"


def test_score_never_fakes_zero():
    with pytest.raises(pydantic.ValidationError):
        Score(value=float("nan"))
    with pytest.raises(pydantic.ValidationError):
        Score(value=float("inf"))
    # None value is valid (unjudgeable), valid flag carries the meaning
    assert Score(value=None, valid=False, invalid_reasons=["x"]).value is None


def test_image_identity_rejects_mutable_tags():
    with pytest.raises(pydantic.ValidationError, match="digest-pinned"):
        ImageIdentity(reference="repo:latest", digest="x", platform="linux/amd64")
    ok = ImageIdentity(
        reference=f"repo@sha256:{'a' * 64}",
        digest="a" * 64,
        platform="linux/amd64",
    )
    assert ok.pinned


def test_artifact_ref_rejects_absolute_and_escaping_paths():
    with pytest.raises(pydantic.ValidationError, match="relative"):
        ArtifactRef(
            media_type="a", sha256="a" * 64, size_bytes=1, path="/abs/path"
        )
    with pytest.raises(pydantic.ValidationError, match="relative"):
        ArtifactRef(
            media_type="a", sha256="a" * 64, size_bytes=1, path="C:\\abs"
        )
    with pytest.raises(pydantic.ValidationError, match="escape"):
        ArtifactRef(
            media_type="a", sha256="a" * 64, size_bytes=1, path="../outside"
        )


def test_bundle_descriptor_session_root_must_stay_inside():
    with pytest.raises(pydantic.ValidationError, match="escape"):
        BundleDescriptor(
            trial_id="t", session_id="s", session_root="../up",
            stop_reason="agent_exit_0", config_digest="d" * 64,
        )
    with pytest.raises(pydantic.ValidationError, match="relative"):
        BundleDescriptor(
            trial_id="t", session_id="s", session_root="/abs",
            stop_reason="agent_exit_0", config_digest="d" * 64,
        )


def test_canonical_json_is_deterministic():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_grade_result_invalid_shape_rejected_at_model():
    # value on an invalid score is the 0-impersonation shape
    with pytest.raises(pydantic.ValidationError):
        Score(value=0.0, valid=False, invalid_reasons=["x"])
