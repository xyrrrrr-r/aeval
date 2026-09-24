"""Contracts tests: the record shapes everything else depends on."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from itertools import combinations
import json

import pytest
import pydantic

from harbor.models.job.config import JobConfig, RetryConfig
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
    RunBinding,
    RunManifest,
    Score,
    TrialBinding,
    TrialPaths,
    TrialRecord,
    canonical_json,
    control_config_digest,
    job_config_hash,
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
    run = RunBinding(**_run_payload())
    with pytest.raises(pydantic.ValidationError, match="escape"):
        BundleDescriptor(
            schema_version=2, run=run,
            trial_id="t", session_id="s", session_root="../up",
            stop_reason="agent_exit_0", config_digest="d" * 64,
        )
    with pytest.raises(pydantic.ValidationError, match="relative"):
        BundleDescriptor(
            schema_version=2, run=run,
            trial_id="t", session_id="s", session_root="/abs",
            stop_reason="agent_exit_0", config_digest="d" * 64,
        )


def test_canonical_json_is_deterministic():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_grade_result_invalid_shape_rejected_at_model():
    # value on an invalid score is the 0-impersonation shape
    with pytest.raises(pydantic.ValidationError):
        Score(value=0.0, valid=False, invalid_reasons=["x"])


def _run_payload():
    return {
        "run_id": "run-1",
        "job_config_hash": "a" * 64,
        "config_file_sha256": "b" * 64,
        "runtime_lock_digest": "c" * 64,
    }


def _paths_payload():
    return {
        "sandbox_cwd": "/workspace/任务",
        "dsh_home": "/tmp/dsh-home",
        "bundle_path": "/tmp/evidence/bundle_descriptor.json",
        "session_root": "sessions/s-1",
        "download_root": "downloads/trial-1",
    }


def _binding_payload():
    return {
        "run": _run_payload(),
        "trial_id": "trial-1",
        "session_id": "s-1",
        "config_digest": "d" * 64,
        "paths": _paths_payload(),
    }


def _bound_descriptor(binding):
    return BundleDescriptor(
        schema_version=2, run=binding.run, trial_id=binding.trial_id,
        session_id=binding.session_id, config_digest=binding.config_digest,
        session_root=binding.paths.session_root, stop_reason="budget_exhausted",
    )


@pytest.mark.parametrize(("model", "factory"), [
    (RunBinding, _run_payload), (TrialPaths, _paths_payload),
    (TrialBinding, _binding_payload),
])
def test_binding_models_require_every_field_forbid_extras_and_are_frozen(model, factory):
    payload = factory()
    instance = model.model_validate(payload)
    assert model.model_validate_json(instance.model_dump_json()) == instance
    for field in payload:
        incomplete = deepcopy(payload)
        del incomplete[field]
        with pytest.raises(pydantic.ValidationError, match=field):
            model.model_validate(incomplete)
        with pytest.raises(pydantic.ValidationError, match="frozen_instance"):
            setattr(instance, field, getattr(instance, field))
    with pytest.raises(pydantic.ValidationError, match="extra_forbidden"):
        model.model_validate({**payload, "untrusted": True})


@pytest.mark.parametrize(("model", "factory"), [
    (RunBinding, _run_payload), (TrialPaths, _paths_payload),
    (TrialBinding, _binding_payload),
])
@pytest.mark.parametrize("value", [None, True, 123, b"valid-text", [], {}])
def test_binding_models_reject_non_string_fields(model, factory, value):
    for field, valid in factory().items():
        if isinstance(valid, str):
            with pytest.raises(pydantic.ValidationError, match=field):
                model.model_validate({**factory(), field: value})


@pytest.mark.parametrize("alias", ["runId", "jobConfigHash", "configFileSha256", "runtimeLockDigest"])
def test_run_binding_rejects_camel_case_aliases(alias):
    with pytest.raises(pydantic.ValidationError, match="extra_forbidden"):
        RunBinding.model_validate({**_run_payload(), alias: "a" * 64})


@pytest.mark.parametrize("value", ["", " id", "id ", "two ids", "id\tpart", "id\n", "id\x00", "id\x7f", "id\x85", "id\u00a0", "id\u2028"])
def test_bindings_reject_invalid_identifiers(value):
    with pytest.raises(pydantic.ValidationError, match="run_id"):
        RunBinding.model_validate({**_run_payload(), "run_id": value})
    for field in ("trial_id", "session_id"):
        with pytest.raises(pydantic.ValidationError, match=field):
            TrialBinding.model_validate({**_binding_payload(), field: value})


@pytest.mark.parametrize("value", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n", "sha256:" + "a" * 64])
def test_bindings_reject_invalid_digests(value):
    for field in ("job_config_hash", "config_file_sha256", "runtime_lock_digest"):
        with pytest.raises(pydantic.ValidationError, match=field):
            RunBinding.model_validate({**_run_payload(), field: value})
    with pytest.raises(pydantic.ValidationError, match="config_digest"):
        TrialBinding.model_validate({**_binding_payload(), "config_digest": value})


@pytest.mark.parametrize("field", ["sandbox_cwd", "dsh_home", "bundle_path"])
@pytest.mark.parametrize("path", [
    "", "relative/path", "C:/workspace", "C:workspace", "//server/share",
    "\\workspace", "/workspace\\child", "/workspace/../escape", "/workspace:ads",
    " /workspace", "/workspace ", "/workspace\x00", "/workspace\n",
    "/workspace\x85", "/workspace\u2028", "/workspace\u2029",
])
def test_trial_paths_require_absolute_posix_sandbox_paths(field, path):
    with pytest.raises(pydantic.ValidationError, match=field):
        TrialPaths.model_validate({**_paths_payload(), field: path})


@pytest.mark.parametrize("field", ["sandbox_cwd", "dsh_home", "bundle_path"])
def test_trial_paths_normalize_posix_paths_without_host_os_conversion(field):
    paths = TrialPaths.model_validate({**_paths_payload(), field: "/工作区//./模型-é/"})
    assert getattr(paths, field) == "/工作区/模型-é"


@pytest.mark.parametrize("field", ["session_root", "download_root"])
@pytest.mark.parametrize("path", [
    "", "../escape", "a/../escape", "/absolute", "\\\\server\\share",
    "C:relative", "a:stream", "a/CON", "a/nul.txt", "a/LPT1", "a/COM³.log",
    "a/trailing.", "a/trailing /child", "a/ leading", "a/?", "a/\x00", "a/\u2028",
])
def test_trial_paths_reject_unsafe_relative_roots(field, path):
    with pytest.raises(pydantic.ValidationError, match=field):
        TrialPaths.model_validate({**_paths_payload(), field: path})


@pytest.mark.parametrize("field", ["session_root", "download_root"])
@pytest.mark.parametrize(("raw", "normalized"), [
    (".\\会话\\模型-é\\", "会话/模型-é"),
    ("./sessions//./s-1/", "sessions/s-1"), ("././", "."),
])
def test_trial_paths_normalize_relative_roots(field, raw, normalized):
    paths = TrialPaths.model_validate({**_paths_payload(), field: raw})
    assert getattr(paths, field) == normalized


def test_trial_binding_verifies_normalized_descriptor_and_roundtrip():
    binding = TrialBinding.model_validate(_binding_payload())
    descriptor = _bound_descriptor(binding)
    payload = descriptor.model_dump(mode="json")
    payload["session_root"] = ".\\sessions\\s-1\\"
    normalized = BundleDescriptor.model_validate(payload)
    assert binding.verify_descriptor(normalized) is None
    assert binding.verify_descriptor(BundleDescriptor.model_validate_json(descriptor.model_dump_json())) is None
    assert len({binding.config_digest, binding.run.job_config_hash,
                binding.run.config_file_sha256, binding.run.runtime_lock_digest}) == 4
    with pytest.raises(pydantic.ValidationError, match="frozen_instance"):
        binding.paths.session_root = "other"
    with pytest.raises(pydantic.ValidationError, match="frozen_instance"):
        binding.run.run_id = "other-run"


@pytest.mark.parametrize(("field", "replacement"), [
    ("run.run_id", "other-run"), ("run.job_config_hash", "e" * 64),
    ("run.config_file_sha256", "e" * 64), ("run.runtime_lock_digest", "e" * 64),
    ("trial_id", "other-trial"), ("session_id", "other-session"),
    ("config_digest", "e" * 64), ("session_root", "sessions/other"),
])
def test_trial_binding_rejects_each_descriptor_identity_mismatch(field, replacement):
    binding = TrialBinding.model_validate(_binding_payload())
    payload = _bound_descriptor(binding).model_dump(mode="json")
    keys = field.split(".")
    owner = payload["run"] if len(keys) == 2 else payload
    owner[keys[-1]] = replacement
    descriptor = BundleDescriptor.model_validate(payload)  # well-formed is not trusted
    with pytest.raises(ValueError, match=keys[0]):
        binding.verify_descriptor(descriptor)


@pytest.mark.parametrize(("left", "right"), list(combinations([
    "run.job_config_hash", "run.config_file_sha256", "run.runtime_lock_digest", "config_digest",
], 2)))
def test_trial_binding_rejects_swapping_any_two_digests(left, right):
    binding = TrialBinding.model_validate(_binding_payload())
    payload = _bound_descriptor(binding).model_dump(mode="json")
    left_owner = payload["run"] if left.startswith("run.") else payload
    right_owner = payload["run"] if right.startswith("run.") else payload
    left_key, right_key = left.split(".")[-1], right.split(".")[-1]
    assert left_owner[left_key] != right_owner[right_key]
    left_owner[left_key], right_owner[right_key] = right_owner[right_key], left_owner[left_key]
    with pytest.raises(ValueError, match="differs from trusted trial binding"):
        binding.verify_descriptor(BundleDescriptor.model_validate(payload))


def test_control_digest_uses_sorted_compact_utf8_and_excludes_only_config_digest():
    config = {
        "z": {"b": ["路径/模型-é", False, None], "a": 2},
        "configDigest": "a" * 64,
        "a": {"configDigest": "nested-is-not-excluded"},
    }
    before = deepcopy(config)
    expected = '{"a":{"configDigest":"nested-is-not-excluded"},"z":{"a":2,"b":["路径/模型-é",false,null]}}'.encode("utf-8")
    digest = sha256(expected).hexdigest()
    assert canonical_json({k: v for k, v in config.items() if k != "configDigest"}) == expected
    assert control_config_digest(config) == digest
    reordered = {"a": config["a"], "configDigest": "b" * 64, "z": {"a": 2, "b": config["z"]["b"]}}
    assert control_config_digest(reordered) == digest
    assert control_config_digest({k: v for k, v in config.items() if k != "configDigest"}) == digest
    for field in ("config_digest", "extra"):
        assert control_config_digest({**config, field: "included"}) != digest
    changed = deepcopy(config)
    changed["a"]["configDigest"] = "changed"
    assert control_config_digest(changed) != digest
    assert config == before


def test_job_config_hash_matches_harbor_serialization_not_file_or_control_digest():
    config = JobConfig(
        job_name="job-one", jobs_dir="jobs-one", extra_instructions=["检查路径/模型-é"],
        retry=RetryConfig(include_exceptions={"ZError", "AError"}, exclude_exceptions={"YError", "BError"}),
    )
    original = config.model_dump(mode="json")
    data = config.model_dump(mode="json", exclude={"job_name", "jobs_dir"})
    data["retry"]["include_exceptions"] = ["AError", "ZError"]
    data["retry"]["exclude_exceptions"] = ["BError", "YError"]
    expected = sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert job_config_hash(config) == expected
    relocated = JobConfig.model_validate({**original, "job_name": "job-two", "jobs_dir": "jobs-two"})
    assert job_config_hash(relocated) == expected
    assert job_config_hash(JobConfig.model_validate({**original, "n_attempts": 2})) != expected
    assert expected != control_config_digest(data)
    assert expected != sha256(config.model_dump_json().encode("utf-8")).hexdigest()
    assert config.model_dump(mode="json") == original


def test_job_config_hash_supports_null_retry_exception_sets():
    config = JobConfig(retry=RetryConfig(include_exceptions=None, exclude_exceptions=None))
    data = config.model_dump(mode="json", exclude={"job_name", "jobs_dir"})
    expected = sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert job_config_hash(config) == expected
