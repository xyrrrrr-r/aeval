"""Bundle descriptor boundary tests (plan §7 row 8).

Python accepts host-emitted descriptors only as untrusted input: the
model layer rejects absolute/escaping session roots, and the evidence
verifier refuses descriptors that don't sit inside the trial dir —
without ever invoking the DSH bridge, a grader, or a verifier.
"""

from __future__ import annotations

import json

import pydantic
import pytest

from aeval.contracts import BundleDescriptor
from aeval.hooks.evidence import EvidenceIntegrityError, verify_evidence_bundle
from tests.conftest import build_complete_trial_dir


def _descriptor(session_root="sessions/s-1", **overrides) -> dict:
    payload = {
        "schema_version": 1,
        "trial_id": "trial-1",
        "session_id": "s-1",
        "session_root": session_root,
        "stop_reason": "agent_exit_0",
        "config_digest": "c" * 64,
    }
    payload.update(overrides)
    return payload


def test_descriptor_requires_relative_session_root():
    assert BundleDescriptor.model_validate(_descriptor()).session_root == "sessions/s-1"


@pytest.mark.parametrize("bad_root", [
    "C:\\abs\\path", "/abs/path", "\\\\server\\share",
    "../escape", "sessions/../../escape", "a/./../escape",
])
def test_descriptor_rejects_absolute_or_escaping_roots(bad_root):
    with pytest.raises(pydantic.ValidationError):
        BundleDescriptor.model_validate(_descriptor(session_root=bad_root))


def test_descriptor_rejects_unknown_schema_and_missing_fields():
    with pytest.raises(pydantic.ValidationError):
        BundleDescriptor.model_validate(_descriptor(schema_version=99))
    with pytest.raises(pydantic.ValidationError):
        BundleDescriptor.model_validate({"trial_id": "t"})
    with pytest.raises(pydantic.ValidationError):
        BundleDescriptor.model_validate(_descriptor(stop_reason="made_up_reason"))


def test_evidence_verifier_accepts_in_trial_descriptor(tmp_path, runtime_lock, demo_suite):
    trial_dir, manifest = build_complete_trial_dir(tmp_path / "trial-1")
    session_dir = trial_dir / "sessions" / "s-1"
    session_dir.mkdir(parents=True)
    (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(_descriptor()), encoding="utf-8")
    from aeval.hooks.evidence import build_required_collect_plan
    plan = build_required_collect_plan(demo_suite)
    bundle = verify_evidence_bundle(trial_dir, runtime_lock, plan)
    assert bundle.bundle_descriptor is not None
    assert bundle.stop_reason == "agent_exit_0"


def test_evidence_verifier_rejects_descriptor_escaping_trial_dir(
    tmp_path, runtime_lock, demo_suite
):
    trial_dir, _ = build_complete_trial_dir(tmp_path / "trial-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.jsonl").write_text("{}\n", encoding="utf-8")
    # an escaping session_root is refused before any bundle is produced —
    # the model layer rejects it inside the verifier, end-to-end
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(_descriptor(session_root="../outside")), encoding="utf-8")
    from aeval.hooks.evidence import build_required_collect_plan
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="descriptor invalid"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)


def test_evidence_verifier_rejects_invalid_descriptor_json(
    tmp_path, runtime_lock, demo_suite
):
    trial_dir, _ = build_complete_trial_dir(tmp_path / "trial-1")
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(_descriptor(session_root="/abs")), encoding="utf-8")
    from aeval.hooks.evidence import build_required_collect_plan
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="descriptor invalid"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)


def test_missing_descriptor_records_issue_not_crash(tmp_path, runtime_lock, demo_suite):
    trial_dir, _ = build_complete_trial_dir(tmp_path / "trial-1")
    from aeval.hooks.evidence import build_required_collect_plan
    plan = build_required_collect_plan(demo_suite)
    bundle = verify_evidence_bundle(trial_dir, runtime_lock, plan)
    assert bundle.bundle_descriptor is None
    assert any("descriptor missing" in i for i in bundle.issues)
