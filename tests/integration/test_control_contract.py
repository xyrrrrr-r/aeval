"""Offline Python/TypeScript wire tests against an already-built control checkout.

Use AEVAL_CONTROL_REPO (or the older AEVAL_DSH_CONTROL_REPO) or a sibling
dsh-eval-control. Missing Node/dist explicitly skips; an obsolete or broken
build fails rather than hiding drift. No package installation, gateway,
model, Docker, or DSH session is needed.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from itertools import combinations
import json
import os
from pathlib import Path
import shutil
import subprocess

import pydantic
import pytest

from aeval.contracts import (
    BundleDescriptor,
    RunBinding,
    TrialBinding,
    TrialPaths,
    canonical_json,
    control_config_digest,
)


# JSON travels over stdin, not through shell interpolation. All validation and
# on-disk ownership checks below call the actual compiled product functions.
_NODE_DRIVER = r"""
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const input = JSON.parse(readFileSync(0, 'utf8'));
const configModule = await import(pathToFileURL(input.configModule).href);
const writerModule = await import(pathToFileURL(input.writerModule).href);
const { resolveEvalControlConfig, validateRunBinding, digestEvalControlConfig } = configModule;
const { buildBundleDescriptor, writeBundleDescriptor, BundleWriter, RunObservationState } = writerModule;
const attempt = (fn) => {
  try { return { accepted: true, value: fn() }; }
  catch (error) { return { accepted: false, error: String(error.message) }; }
};
let result;
if (input.op === 'probe') {
  result = {
    schema: writerModule.BUNDLE_DESCRIPTOR_SCHEMA_VERSION,
    exports: [resolveEvalControlConfig, validateRunBinding, digestEvalControlConfig,
      buildBundleDescriptor, writeBundleDescriptor, BundleWriter, RunObservationState]
      .every((item) => typeof item === 'function'),
  };
} else if (input.op === 'resolve') {
  const resolved = resolveEvalControlConfig(input.config);
  const digest = digestEvalControlConfig(resolved);
  const config = resolveEvalControlConfig({ ...resolved, configDigest: digest });
  const descriptor = buildBundleDescriptor(config, 'budget_exhausted');
  writeBundleDescriptor(input.path, descriptor);
  result = { config, digest, descriptor, onDisk: JSON.parse(readFileSync(input.path, 'utf8')) };
} else if (input.op === 'accept') {
  // The trusted owner config is kept separate from the Python wire payload.
  const config = resolveEvalControlConfig(input.config);
  writeFileSync(input.path, JSON.stringify(input.descriptor), 'utf8');
  const writer = new BundleWriter(input.path, config);
  const state = new RunObservationState(config.sessionId);
  state.recordTerminal('budget_exhausted');
  try {
    result = attempt(() => {
      validateRunBinding(input.descriptor.run);
      writer.flush(state);
      return JSON.parse(readFileSync(input.path, 'utf8'));
    });
    result.onDisk = JSON.parse(readFileSync(input.path, 'utf8'));
  } finally { writer.release(); }
} else if (input.op === 'validate') {
  result = input.configs.map((config) => attempt(() => resolveEvalControlConfig(config)));
} else if (input.op === 'runs') {
  result = input.runs.map((run) => attempt(() => validateRunBinding(run)));
} else if (input.op === 'digests') {
  result = input.configs.map((config) => digestEvalControlConfig(config));
} else {
  throw new Error(`Unknown test operation: ${input.op}`);
}
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def control_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("offline control contract: node is unavailable")
    # AEVAL_CONTROL_REPO is the neutral spelling; the DSH-flavored one is
    # kept as a fallback so existing operator environments keep working.
    override = os.environ.get("AEVAL_CONTROL_REPO") or os.environ.get("AEVAL_DSH_CONTROL_REPO")
    repo = (
        Path(override).expanduser().resolve()
        if override else Path(__file__).resolve().parents[3] / "dsh-eval-control"
    )
    config_module = repo / "dist" / "config.js"
    writer_module = repo / "dist" / "bundle_writer.js"
    if not config_module.is_file() or not writer_module.is_file():
        pytest.skip(
            f"offline control contract: built dist/config.js and dist/bundle_writer.js "
            f"unavailable in {repo}; set AEVAL_CONTROL_REPO to a built checkout"
        )

    def invoke(op, **payload):
        result = subprocess.run(
            [node, "--input-type=module", "--eval", _NODE_DRIVER],
            input=json.dumps({
                "op": op,
                "configModule": str(config_module),
                "writerModule": str(writer_module),
                **payload,
            }, ensure_ascii=False),
            cwd=repo,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"offline Node contract failed ({repo}):\n{result.stderr}\n{result.stdout}"
        )
        return json.loads(result.stdout)

    probe = invoke("probe")
    assert probe == {"schema": 2, "exports": True}, (
        f"stale control dist in {repo}: build the schema-2 sources before testing; "
        "this test deliberately never installs or builds dependencies"
    )
    return invoke


def _python_config():
    config = {
        "run": {
            "run_id": "run-offline",
            "job_config_hash": sha256(b"resolved Harbor job").hexdigest(),
            "config_file_sha256": sha256(b"original job config file").hexdigest(),
            "runtime_lock_digest": sha256(b"runtime lock").hexdigest(),
        },
        "trialId": "trial-1",
        "sessionId": "session-1",
        "sessionRoot": "sessions/会话-é",
        "configDigest": "0" * 64,
        "provider": "offline-provider",
        "model": "offline-model",
        "reasoningEffort": "high",
        "maxSteps": 3,
        "maxTokens": 64,
        "tools": {"deny": ["exec"], "allow": ["read_file"]},
        "lineage": {"parentSessionId": "parent-session", "parentTrialId": "parent-trial", "forkStep": 0},
        "bundlePath": "/tmp/评估/bundle_descriptor.json",
        "gatewayUrl": "http://127.0.0.1:1",
        "jobTokenFile": "/run/凭据/job.token",
        "refuseAuxiliaryCalls": True,
    }
    config["configDigest"] = control_config_digest(config)
    return config


def _binding(config):
    return TrialBinding(
        run=RunBinding.model_validate(config["run"]),
        trial_id=config["trialId"], session_id=config["sessionId"],
        config_digest=control_config_digest(config),
        paths=TrialPaths(
            sandbox_cwd="/workspace/任务", agent_home="/tmp/dsh-home",
            bundle_path=config["bundlePath"], session_root=config["sessionRoot"],
            download_root="downloads/评估",
        ),
    )


def _python_descriptor(config):
    binding = _binding(config)
    lineage = config.get("lineage")
    return BundleDescriptor(
        schema_version=2, run=binding.run, trial_id=binding.trial_id,
        session_id=binding.session_id, session_root=binding.paths.session_root,
        stop_reason="budget_exhausted", config_digest=binding.config_digest,
        lineage={
            "parent_session_id": lineage["parentSessionId"],
            "parent_trial_id": lineage["parentTrialId"],
            "fork_step": lineage["forkStep"],
        } if lineage is not None else None,
    )


def _owner(payload, field):
    keys = field.split(".")
    return (payload["run"] if len(keys) == 2 else payload), keys[-1]


@pytest.mark.parametrize("with_lineage", [False, True])
def test_ts_descriptor_to_python_and_back(control_node, tmp_path, with_lineage):
    raw = _python_config()
    if not with_lineage:
        del raw["lineage"]
    raw["sessionRoot"] = ".\\sessions\\会话-é\\"
    result = control_node("resolve", config=raw, path=str(tmp_path / "ts.json"))
    config = result["config"]
    assert config["sessionRoot"] == "sessions/会话-é"
    assert result["onDisk"] == result["descriptor"]
    assert config["configDigest"] == result["digest"] == control_config_digest(config)
    assert len({config["configDigest"], config["run"]["job_config_hash"],
                config["run"]["config_file_sha256"], config["run"]["runtime_lock_digest"]}) == 4
    digest_input = {k: v for k, v in config.items() if k != "configDigest"}
    expected_bytes = json.dumps(digest_input, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert canonical_json(digest_input) == expected_bytes
    assert "会话-é".encode("utf-8") in expected_bytes
    assert result["digest"] == sha256(expected_bytes).hexdigest()
    descriptor = BundleDescriptor.model_validate(result["onDisk"])
    assert _binding(config).verify_descriptor(descriptor) is None
    assert descriptor == _python_descriptor(config)
    restored = BundleDescriptor.model_validate_json(descriptor.model_dump_json())
    assert restored == descriptor
    # Both a Python-built descriptor and a Python-serialized TS descriptor must
    # survive the TS writer's real persisted-binding checks, not a test comparer.
    for index, candidate in enumerate((_python_descriptor(config), restored)):
        accepted = control_node(
            "accept", config=config, descriptor=candidate.model_dump(mode="json", exclude_none=True),
            path=str(tmp_path / f"python-{index}.json"),
        )
        assert accepted["accepted"], accepted
        assert BundleDescriptor.model_validate(accepted["value"]) == descriptor


def test_python_config_and_descriptor_are_accepted_without_ts_preprocessing(control_node, tmp_path):
    config = _python_config()
    descriptor = _python_descriptor(config)
    validated = control_node("validate", configs=[config])
    assert validated == [{"accepted": True, "value": config}]
    assert control_node("digests", configs=[config]) == [config["configDigest"]]
    result = control_node(
        "accept", config=config, descriptor=descriptor.model_dump(mode="json", exclude_none=True),
        path=str(tmp_path / "from-python.json"),
    )
    assert result["accepted"], result
    assert BundleDescriptor.model_validate(result["value"]) == descriptor
    assert _binding(config).verify_descriptor(BundleDescriptor.model_validate(result["onDisk"])) is None


def test_digest_is_over_resolved_defaults_not_raw_input(control_node, tmp_path):
    raw = _python_config()
    for field in ("bundlePath", "refuseAuxiliaryCalls", "reasoningEffort", "maxSteps", "maxTokens", "tools", "lineage"):
        del raw[field]
    result = control_node("resolve", config=raw, path=str(tmp_path / "defaults.json"))
    resolved = result["config"]
    assert resolved["bundlePath"] == "bundle_descriptor.json"
    assert resolved["refuseAuxiliaryCalls"] is True
    assert result["digest"] == control_config_digest(resolved)
    assert result["digest"] != control_config_digest(raw)
    assert not {"tools", "lineage", "reasoningEffort", "maxSteps", "maxTokens"} & resolved.keys()


_MISMATCHES = [
    ("run.run_id", "other-run"), ("run.job_config_hash", "e" * 64),
    ("run.config_file_sha256", "e" * 64), ("run.runtime_lock_digest", "e" * 64),
    ("trial_id", "other-trial"), ("session_id", "other-session"),
    ("config_digest", "e" * 64), ("session_root", "sessions/other"),
]


@pytest.mark.parametrize(("field", "replacement"), _MISMATCHES)
def test_both_owners_reject_each_well_formed_identity_mismatch(control_node, tmp_path, field, replacement):
    config = _python_config()
    descriptor = _python_descriptor(config).model_dump(mode="json", exclude_none=True)
    owner, key = _owner(descriptor, field)
    owner[key] = replacement
    changed = BundleDescriptor.model_validate(descriptor)
    with pytest.raises(ValueError, match=field.split(".")[0]):
        _binding(config).verify_descriptor(changed)
    result = control_node("accept", config=config, descriptor=descriptor, path=str(tmp_path / "mismatch.json"))
    assert result["accepted"] is False, field
    assert "bound to a different" in result["error"]
    assert result["onDisk"] == descriptor, "a rejected binding must never be overwritten"


@pytest.mark.parametrize(("left", "right"), list(combinations([
    "run.job_config_hash", "run.config_file_sha256", "run.runtime_lock_digest", "config_digest",
], 2)))
def test_both_owners_reject_all_digest_swaps(control_node, tmp_path, left, right):
    config = _python_config()
    descriptor = _python_descriptor(config).model_dump(mode="json", exclude_none=True)
    left_owner, left_key = _owner(descriptor, left)
    right_owner, right_key = _owner(descriptor, right)
    assert left_owner[left_key] != right_owner[right_key]
    left_owner[left_key], right_owner[right_key] = right_owner[right_key], left_owner[left_key]
    with pytest.raises(ValueError, match="differs from trusted trial binding"):
        _binding(config).verify_descriptor(BundleDescriptor.model_validate(descriptor))
    result = control_node("accept", config=config, descriptor=descriptor, path=str(tmp_path / "swapped.json"))
    assert result["accepted"] is False
    assert "bound to a different" in result["error"]
    assert result["onDisk"] == descriptor


def test_run_binding_validation_agrees_in_both_languages(control_node):
    valid = _python_config()["run"]
    assert control_node("runs", runs=[valid]) == [{"accepted": True, "value": valid}]
    invalid = []
    for field in valid:
        missing = deepcopy(valid)
        del missing[field]
        invalid.append(missing)
        for value in (None, True, 123, [], {}, "", "with space", "x\x00"):
            invalid.append({**valid, field: value})
    for field in ("job_config_hash", "config_file_sha256", "runtime_lock_digest"):
        for value in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n"):
            invalid.append({**valid, field: value})
    invalid.extend([{**valid, "runId": "run-1"}, {**valid, "extra": True}])
    results = control_node("runs", runs=invalid)
    for payload, result in zip(invalid, results, strict=True):
        with pytest.raises(pydantic.ValidationError):
            RunBinding.model_validate(payload)
        assert result["accepted"] is False, payload
        assert result["error"]


def test_ts_config_requires_run_and_strict_identity_fields(control_node):
    config = _python_config()
    invalid = []
    for field in ("run", "trialId", "sessionId", "configDigest"):
        missing = deepcopy(config)
        del missing[field]
        invalid.append(missing)
        for value in (None, True, 123, [], {}, "", "bad id", "bad\x00"):
            invalid.append({**config, field: value})
    for value in ("a" * 63, "a" * 65, "A" * 64, "g" * 64):
        invalid.append({**config, "configDigest": value})
    for payload, result in zip(invalid, control_node("validate", configs=invalid), strict=True):
        assert result["accepted"] is False, payload
        assert result["error"]


def test_portable_session_root_validation_agrees_in_both_languages(control_node):
    good = {
        ".\\sessions\\会话-é\\": "sessions/会话-é",
        "./sessions//./s-1/": "sessions/s-1",
        "././": ".",
        "sessions/COM10/conifer": "sessions/COM10/conifer",
    }
    bad = [
        "", "/absolute", "\\\\server\\share", "C:relative", "C:/absolute",
        "a:stream", "../escape", "a/./../escape", "a/CON", "a/con.txt", "a/AUX",
        "a/NUL", "a/PrN.log", "a/COM1", "a/LPT9.txt", "a/COM¹.log", "a/LPT²",
        "a/trailing.", "a/ leading", "a/trailing /s", "a/<bad>", "a/a|b", "a/a?b",
        "a/a*b", "a/\x00", "a/\x1f", "a/\x85", "a/\u2028", "a/\u2029",
    ]
    roots = [*good, *bad]
    configs = [{**_python_config(), "sessionRoot": root} for root in roots]
    results = control_node("validate", configs=configs)
    descriptor = _python_descriptor(_python_config()).model_dump(mode="json")
    for root, result in zip(roots, results, strict=True):
        payload = {**descriptor, "session_root": root}
        if root in good:
            assert result["accepted"], result
            assert result["value"]["sessionRoot"] == good[root]
            assert BundleDescriptor.model_validate(payload).session_root == good[root]
        else:
            assert result["accepted"] is False, root
            with pytest.raises(pydantic.ValidationError, match="session_root"):
                BundleDescriptor.model_validate(payload)


def test_digest_covers_every_resolved_field_and_only_excludes_config_digest(control_node):
    config = _python_config()
    reordered = dict(reversed(list(config.items())))
    reordered["run"] = dict(reversed(list(config["run"].items())))
    reordered["tools"] = dict(reversed(list(config["tools"].items())))
    variants = [config, reordered, {**config, "configDigest": "f" * 64}]
    for field in config:
        if field == "configDigest":
            continue
        changed = deepcopy(config)
        value = changed[field]
        if isinstance(value, dict):
            for nested in value:
                nested_change = deepcopy(config)
                current = value[nested]
                nested_change[field][nested] = (
                    current + ["write_file"] if isinstance(current, list)
                    else current + 1 if isinstance(current, int)
                    else current + "-changed"
                )
                variants.append(nested_change)
        else:
            changed[field] = (
                not value if isinstance(value, bool)
                else value + 1 if isinstance(value, int)
                else value + "-changed"
            )
            variants.append(changed)
    # Exercise the hash function directly: it must not silently drop unknown
    # keys or nested configDigest keys even though config validation forbids them.
    variants.extend([
        {**config, "config_digest": "included"},
        {**config, "nested": {"configDigest": "included"}},
    ])
    actual = control_node("digests", configs=variants)
    assert actual == [control_config_digest(value) for value in variants]
    assert actual[:3] == [config["configDigest"]] * 3
    assert all(digest != actual[0] for digest in actual[3:])
