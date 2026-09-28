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
        "schema_version": 2,
        "run": {
            "run_id": "run-1",
            "job_config_hash": "a" * 64,
            "config_file_sha256": "b" * 64,
            "runtime_lock_digest": "d" * 64,
        },
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


@pytest.mark.parametrize("schema", [1, 3, 99, "2", None, True])
def test_descriptor_rejects_non_v2_schema(schema):
    with pytest.raises(pydantic.ValidationError, match="schema_version"):
        BundleDescriptor.model_validate(_descriptor(schema_version=schema))


@pytest.mark.parametrize("field", [
    "run", "trial_id", "session_id", "session_root", "stop_reason", "config_digest",
])
def test_descriptor_rejects_missing_required_fields(field):
    payload = _descriptor()
    del payload[field]
    with pytest.raises(pydantic.ValidationError, match=field):
        BundleDescriptor.model_validate(payload)


def test_descriptor_rejects_unknown_fields_and_stop_reason():
    with pytest.raises(pydantic.ValidationError, match="extra_forbidden"):
        BundleDescriptor.model_validate(_descriptor(untrusted=True))
    with pytest.raises(pydantic.ValidationError, match="stop_reason"):
        BundleDescriptor.model_validate(_descriptor(stop_reason="made_up_reason"))


@pytest.mark.parametrize("field", ["trial_id", "session_id", "config_digest", "session_root"])
@pytest.mark.parametrize("value", [None, True, 123, b"s-1", [], {}])
def test_descriptor_fields_do_not_coerce(field, value):
    with pytest.raises(pydantic.ValidationError, match=field):
        BundleDescriptor.model_validate(_descriptor(**{field: value}))


@pytest.mark.parametrize("field", ["trial_id", "session_id"])
@pytest.mark.parametrize("value", ["", " id", "id ", "two ids", "id\tpart", "id\n", "id\x00", "id\x7f", "id\x85", "id\u2028", "id\u00a0"])
def test_descriptor_rejects_invalid_identifiers(field, value):
    with pytest.raises(pydantic.ValidationError, match=field):
        BundleDescriptor.model_validate(_descriptor(**{field: value}))


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n", "sha256:" + "a" * 64])
def test_descriptor_rejects_invalid_config_digest(value):
    with pytest.raises(pydantic.ValidationError, match="config_digest"):
        BundleDescriptor.model_validate(_descriptor(config_digest=value))


@pytest.mark.parametrize("bad_root", [
    "", " ", "C:sessions/s-1", "sessions/s-1:stream", "sessions/CON",
    "sessions/con.txt", "sessions/PrN.log", "AUX/session", "NUL",
    "sessions/COM1", "sessions/LPT9.txt", "sessions/COM¹.log", "sessions/LPT²",
    "sessions/trailing.", "sessions/ leading", "sessions/trailing /s-1",
    "sessions/<bad>", 'sessions/"bad"', "sessions/a|b", "sessions/a?b",
    "sessions/a*b", "sessions/s\x00", "sessions/s\x1f", "sessions/s\x7f",
    "sessions/s\x85", "sessions/s\u2028", "sessions/s\u2029",
])
def test_descriptor_rejects_nonportable_session_roots(bad_root):
    with pytest.raises(pydantic.ValidationError, match="session_root"):
        BundleDescriptor.model_validate(_descriptor(session_root=bad_root))


@pytest.mark.parametrize(("raw", "normalized"), [
    (".\\sessions\\s-1\\", "sessions/s-1"),
    ("./sessions//./s-1/", "sessions/s-1"),
    ("././", "."),
    ("会话\\模型-é", "会话/模型-é"),
    ("sessions/COM10/conifer", "sessions/COM10/conifer"),
])
def test_descriptor_normalizes_portable_session_roots(raw, normalized):
    descriptor = BundleDescriptor.model_validate(_descriptor(session_root=raw))
    assert descriptor.session_root == normalized
    assert BundleDescriptor.model_validate_json(descriptor.model_dump_json()) == descriptor


def test_descriptor_and_nested_run_are_frozen():
    descriptor = BundleDescriptor.model_validate(_descriptor())
    with pytest.raises(pydantic.ValidationError, match="frozen_instance"):
        descriptor.trial_id = "other-trial"
    with pytest.raises(pydantic.ValidationError, match="frozen_instance"):
        descriptor.run.run_id = "other-run"


def test_evidence_verifier_accepts_in_trial_descriptor(tmp_path, runtime_lock, demo_suite):
    from aeval.hooks.evidence import build_required_collect_plan

    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial-1",
        plan=build_required_collect_plan(demo_suite),
        runtime_lock=runtime_lock,
    )
    # a descriptor whose session_root owns the session artifact passes;
    # ownership is by content, so the official record must carry the same
    # bytes the collector wrote to the fixed path
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(_descriptor(session_root="sessions")), encoding="utf-8")
    record = trial_dir / "sessions" / "s-1" / "session.v4.jsonl.zstd"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes(
        (trial_dir / "sessions" / "session.v4.jsonl.zstd").read_bytes()
    )
    from aeval.hooks.evidence import build_required_collect_plan
    plan = build_required_collect_plan(demo_suite)
    bundle = verify_evidence_bundle(trial_dir, runtime_lock, plan)
    assert bundle.bundle_descriptor is not None
    assert bundle.stop_reason == "agent_exit_0"


def test_evidence_verifier_rejects_descriptor_escaping_trial_dir(
    tmp_path, runtime_lock, demo_suite
):
    from aeval.hooks.evidence import build_required_collect_plan

    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial-1",
        plan=build_required_collect_plan(demo_suite),
        runtime_lock=runtime_lock,
    )
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
    from aeval.hooks.evidence import build_required_collect_plan

    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial-1",
        plan=build_required_collect_plan(demo_suite),
        runtime_lock=runtime_lock,
    )
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(_descriptor(session_root="/abs")), encoding="utf-8")
    from aeval.hooks.evidence import build_required_collect_plan
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="descriptor invalid"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)


def test_missing_descriptor_is_a_hard_failure(tmp_path, runtime_lock, demo_suite):
    """P0-6: without the descriptor there is no session ownership or
    stop reason — the evidence is incomplete and the gate fails."""
    from aeval.hooks.evidence import build_required_collect_plan

    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial-1",
        plan=build_required_collect_plan(demo_suite),
        runtime_lock=runtime_lock,
        descriptor=False,
    )
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="bundle descriptor missing"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)
