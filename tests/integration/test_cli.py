"""CLI integration tests (plan §7 row 14): exit codes and side effects.

``run`` must delegate to Harbor (never build its own loop) and pass the
AEVAL_* contract via the environment; a validation failure must reach
Harbor zero times. Exit codes: 0 ok, 2 param, 3 validation, 4 system,
5 Harbor exited 0 but the aeval chain is incomplete (P0-8 final gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeval.cli import app, selftest_app  # noqa: F401
from aeval.contracts import (
    GradeResult,
    OverlayIdentity,
    RunManifest,
    Score,
    TrialRecord,
    VersionsBundle,
)

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEMO = FIXTURES / "suites" / "demo"

@pytest.fixture()
def harbor_calls(monkeypatch, runtime_lock):
    import subprocess
    from types import SimpleNamespace

    calls = []
    original = subprocess.run

    def run(args, **kwargs):
        if args[0] != "test-harbor":
            return original(args, **kwargs)
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("aeval.provenance.build_runtime_lock", lambda **kwargs: runtime_lock)
    monkeypatch.setattr("aeval.suite_loader.composition.suite_source_commit", lambda path: "a" * 40)
    return calls


def test_probe_ok(native_suite_dir):
    result = runner.invoke(app, ["probe", "--suite", str(native_suite_dir)])
    assert result.exit_code == 0, result.output
    assert "native-example" in result.output
    assert "task-references=1" in result.output


def test_probe_rejects_missing_suite(tmp_path):
    result = runner.invoke(app, ["probe", "--suite", str(tmp_path / "nowhere")])
    assert result.exit_code == 3


def test_list_discovers_suites():
    result = runner.invoke(app, ["list", "--suites-dir", str(FIXTURES / "suites")])
    assert result.exit_code == 0
    assert "refund-policy" in result.output


def test_list_rejects_missing_root(tmp_path):
    result = runner.invoke(app, ["list", "--suites-dir", str(tmp_path / "nowhere")])
    assert result.exit_code == 3


def test_explain_renders_read_only_view(native_suite_dir):
    result = runner.invoke(app, ["explain", str(native_suite_dir)])
    assert result.exit_code == 0, result.output
    assert "Native Harbor job" in result.output
    assert '"n_attempts": 2' in result.output


def test_run_delegates_to_harbor_with_env_contract(tmp_path, native_suite_dir, harbor_calls):
    from hashlib import sha256
    from harbor.models.job.config import JobConfig

    run_dir = tmp_path / "myrun"
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(run_dir),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 7, result.output
    assert len(harbor_calls) == 1
    argv, kwargs = harbor_calls[0]
    assert argv == ["test-harbor", "run", "--config", str(run_dir / "harbor-job.json"),
                    "--plugin", "aeval.hooks:AevalPlugin"]
    assert kwargs["env"]["AEVAL_RUN_ID"] == "run-myrun"
    assert kwargs["env"]["AEVAL_SUITE_DIR"] == str(native_suite_dir.resolve())
    assert Path(kwargs["env"]["AEVAL_RUNTIME_LOCK"]).is_file()
    raw = (run_dir / "harbor-job.json").read_bytes()
    job = JobConfig.model_validate_json(raw)
    assert job.n_attempts == 2
    assert job.n_concurrent_trials == 1
    assert job.tasks[0].path == native_suite_dir / "tasks/example"
    assert job.jobs_dir == run_dir / "harbor"
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-myrun"
    assert manifest["overlay"]["suite_id"] == "native-example"
    assert manifest["overlay"]["source_commit"] == "a" * 40
    assert manifest["lock_ref"] == "harbor/synthetic/lock.json"
    assert manifest["config_file_sha256"] == sha256(raw).hexdigest()
    assert manifest["config_hash"] != manifest["config_file_sha256"]
    assert manifest["sealed"] is False


def test_repeated_runs_compare_inputs_not_output_paths(native_suite_dir, tmp_path, harbor_calls):
    import yaml
    from aeval.bundle.attestation import compare_manifests

    def run(name):
        out = tmp_path / name
        result = runner.invoke(app, [
            "run", "--suite", str(native_suite_dir), "--run-dir", str(out),
            "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
        ])
        assert result.exit_code == 7, result.output
        return RunManifest.model_validate_json((out / "run_manifest.json").read_bytes())

    left, right = run("first"), run("second")
    assert left.config_file_sha256 != right.config_file_sha256
    assert left.config_hash == right.config_hash
    assert not compare_manifests(left, right)
    job_path = native_suite_dir / "job.yaml"
    data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    data["n_attempts"] = 3
    job_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    changed = run("changed")
    assert "config" in compare_manifests(left, changed)


@pytest.mark.parametrize("env", [[123], [None], [True]])
def test_malformed_env_entries_have_validation_diagnostics(native_suite_dir, env):
    import yaml

    path = native_suite_dir / "job.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["environment"] = {"env": env}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(app, ["probe", "--suite", str(native_suite_dir)])
    assert result.exit_code == 3
    assert "environment.env must be a mapping" in result.output


def test_run_invalid_suite_never_reaches_harbor(tmp_path, harbor_calls):
    result = runner.invoke(app, [
        "run", "--suite", str(tmp_path / "nowhere"), "--run-dir", str(tmp_path / "myrun"),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 3
    assert not harbor_calls
    assert not (tmp_path / "myrun").exists()


def test_run_dir_never_reused(tmp_path, native_suite_dir, harbor_calls):
    args = [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(tmp_path / "myrun"),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 7, first.output
    second = runner.invoke(app, args)
    assert second.exit_code == 3
    assert "never reused" in second.output
    assert len(harbor_calls) == 1


def test_harbor_cli_accepts_composed_configuration(tmp_path, native_suite_dir, harbor_calls):
    import subprocess
    import sys

    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(tmp_path / "smoke"),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 7, result.output
    argv, _ = harbor_calls[0]
    parsed = subprocess.run(
        [sys.executable, "-m", "harbor.cli.main", *argv[1:], "--print-config"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
    assert json.loads(parsed.stdout)["tasks"][0]["path"] == str(native_suite_dir / "tasks/example")


def test_legacy_demo_is_not_mistaken_for_runnable_suite():
    result = runner.invoke(app, ["probe", "--suite", str(DEMO)])
    assert result.exit_code == 3


@pytest.mark.parametrize("field", ["dataset_digest", "job_digest"])
def test_probe_rejects_pinned_declaration_digest_mismatch(native_suite_dir, field):
    import yaml

    path = native_suite_dir / "suite.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["harbor"][field] = "0" * 64
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(app, ["probe", "--suite", str(native_suite_dir)])
    assert result.exit_code == 3
    assert "digest mismatch" in result.output


def test_malformed_suite_yaml_is_a_validation_error(native_suite_dir):
    (native_suite_dir / "suite.yaml").write_text("schema_version: [", encoding="utf-8")
    result = runner.invoke(app, ["probe", "--suite", str(native_suite_dir)])
    assert result.exit_code == 3
    assert "Cannot read suite manifest" in result.output


def test_uncommitted_suite_never_reaches_harbor(native_suite_dir, tmp_path, harbor_calls, monkeypatch):
    from aeval.suite_models import SuiteError

    def missing_commit(path):
        raise SuiteError("Suite has uncommitted changes")

    monkeypatch.setattr("aeval.suite_loader.composition.suite_source_commit", missing_commit)
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(tmp_path / "blocked"),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 3
    assert not harbor_calls
    assert not (tmp_path / "blocked").exists()


def test_rejudge_refuses_without_source_bundle(tmp_path):
    result = runner.invoke(app, [
        "rejudge", "--store", str(tmp_path / "store.sqlite3"), "run-1",
        "--target-verifier", "v2",
    ])
    assert result.exit_code == 3
    assert "refusing to call Harbor" in result.output


def test_selftest_manifest_blocks_all_injections():
    result = runner.invoke(selftest_app, ["manifest"])
    assert result.exit_code == 0
    assert "all injections blocked" in result.output


def test_recompute_reports_clean_bundle(tmp_path, runtime_lock):
    from aeval.bundle.attestation import create_test_result_attestation
    from aeval.bundle.manifest import seal_run_manifest, write_intent_manifest
    from aeval.contracts import ExclusionSummary

    run_dir = tmp_path / "bundle"
    manifest = RunManifest(
        run_id="r1",
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )
    manifest_path = write_intent_manifest(manifest, run_dir)
    (run_dir / "trials.jsonl").write_text("{}\n", encoding="utf-8")
    seal_run_manifest(manifest_path, ExclusionSummary())
    create_test_result_attestation(run_dir)

    result = runner.invoke(app, ["recompute", str(run_dir)])
    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["attested_files"] >= 2


def test_report_renders_from_store(tmp_path, runtime_lock):
    from aeval.store.sqlite import TrialStore

    store_path = tmp_path / "store.sqlite3"
    store = TrialStore(store_path)
    manifest = RunManifest(
        run_id="run-1",
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )
    store.create_run(manifest)
    for i, verdict in enumerate(["pass", "pass", "fail"]):
        record = TrialRecord(
            trial_id=f"t{i}",
            coordinates={"run_id": "run-1", "suite_id": "s", "suite_version": "1",
                         "task_id": "task", "trial_index": i},
            stop_reason="agent_exit_0",
            verdict=verdict,
        )
        store.persist_trial(record)
        store.persist_grades(record.trial_id, [GradeResult(
            grader_id="g", grader_version="v1", layer="outcome",
            score=Score(value=1.0 if verdict == "pass" else 0.0),
            status="pass" if verdict == "pass" else "fail",
        )])
    store.close()

    result = runner.invoke(app, ["report", "--store", str(store_path), "run-1", "--k", "2"])
    assert result.exit_code == 0
    assert "pass@2" in result.output
    assert "pass^2" in result.output


def _fake_harbor(returncode=0):
    """Harabor stand-in that simulates the plugin contract.

    With returncode 0 it writes a complete run summary and persists a
    classified trial into the store (exactly what AevalPlugin +
    grade_and_record owe the CLI); with returncode 7 it does nothing.
    """
    import subprocess
    from types import SimpleNamespace

    def run(args, **kwargs):
        if args[0] != "test-harbor":
            return subprocess.run(args, **kwargs)
        if returncode == 0:
            from aeval.store.sqlite import TrialStore

            env = kwargs["env"]
            run_dir = Path(env["AEVAL_RUN_DIR"])
            store = TrialStore(env["AEVAL_STORE_PATH"])
            record = TrialRecord(
                trial_id="t0",
                coordinates={"run_id": env["AEVAL_RUN_ID"], "suite_id": "s",
                             "suite_version": "1", "task_id": "task", "trial_index": 0},
                stop_reason="agent_exit_0",
                verdict="pass",
            )
            store.persist_trial(record)
            store.persist_grades(record.trial_id, [GradeResult(
                grader_id="g", grader_version="v1", layer="outcome",
                score=Score(value=1.0), status="pass",
            )])
            store.close()
            (run_dir / "aeval_run_summary.json").write_text(json.dumps({
                "run_id": env["AEVAL_RUN_ID"],
                "unobserved_trials": 0,
                "trials": {"t0": {"phase": "ended", "stop_reason": "agent_exit_0"}},
            }), encoding="utf-8")
        return SimpleNamespace(returncode=returncode)
    return run


def test_run_final_gate_requires_complete_chain(tmp_path, native_suite_dir, monkeypatch, runtime_lock):
    """Harbor 0 + no plugin summary → exit 5, never a silent success."""
    import subprocess

    def harbor_without_summary(args, **kwargs):
        import subprocess as sp
        from types import SimpleNamespace

        if args[0] != "test-harbor":
            return sp.run(args, **kwargs)
        from aeval.store.sqlite import TrialStore

        env = kwargs["env"]
        store = TrialStore(env["AEVAL_STORE_PATH"])
        record = TrialRecord(
            trial_id="t0",
            coordinates={"run_id": env["AEVAL_RUN_ID"], "suite_id": "s",
                         "suite_version": "1", "task_id": "task", "trial_index": 0},
            stop_reason="agent_exit_0",
            verdict="pass",
        )
        store.persist_trial(record)
        store.persist_grades(record.trial_id, [GradeResult(
            grader_id="g", grader_version="v1", layer="outcome",
            score=Score(value=1.0), status="pass",
        )])
        store.close()
        # no aeval_run_summary.json written
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", harbor_without_summary)
    monkeypatch.setattr("aeval.provenance.build_runtime_lock", lambda **kwargs: runtime_lock)
    monkeypatch.setattr("aeval.suite_loader.composition.suite_source_commit", lambda path: "a" * 40)

    run_dir = tmp_path / "gate"
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(run_dir),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 5
    assert "run incomplete" in result.output
    # nothing was sealed
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["sealed"] is False


def test_run_seals_when_chain_is_whole(tmp_path, native_suite_dir, monkeypatch, runtime_lock):
    import subprocess

    monkeypatch.setattr(subprocess, "run", _fake_harbor(returncode=0))
    monkeypatch.setattr("aeval.provenance.build_runtime_lock", lambda **kwargs: runtime_lock)
    monkeypatch.setattr("aeval.suite_loader.composition.suite_source_commit", lambda path: "a" * 40)

    run_dir = tmp_path / "whole"
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(run_dir),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 0, result.output
    assert "sealed" in result.output
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["sealed"] is True
    assert (run_dir / "attestation.json").is_file()


def test_run_records_intent_in_store_before_harbor(tmp_path, native_suite_dir, harbor_calls):
    from aeval.store.sqlite import TrialStore

    run_dir = tmp_path / "intent"
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(run_dir),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
    ])
    assert result.exit_code == 7  # harbor nonzero propagates, store still written
    store = TrialStore(tmp_path / "store.sqlite3")
    try:
        manifest = store.load_run_manifest("run-intent")
        assert manifest.run_id == "run-intent"
    finally:
        store.close()


def _two_runs_store(tmp_path, runtime_lock, *, same_lock=True):
    from aeval.store.sqlite import TrialStore

    store_path = tmp_path / "store.sqlite3"
    store = TrialStore(store_path)
    lock2 = runtime_lock if same_lock else runtime_lock.model_copy(
        update={"harbor_lock_ref": "different"}
    )
    for run_id, lock, n in (("run-1", runtime_lock, 2), ("run-2", lock2, 3)):
        manifest = RunManifest(
            run_id=run_id, runtime_lock=lock,
            overlay=OverlayIdentity(
                suite_id="s", suite_version="1", overlay_digest="d" * 64,
                source_commit="9" * 40,
            ),
            versions=VersionsBundle(aeval_version="0.1.0"),
        )
        store.create_run(manifest)
        for i, verdict in enumerate(["pass"] * n):
            record = TrialRecord(
                trial_id=f"{run_id}-t{i}",
                coordinates={"run_id": run_id, "suite_id": "s", "suite_version": "1",
                             "task_id": "task", "trial_index": i},
                stop_reason="agent_exit_0", verdict=verdict,
            )
            store.persist_trial(record)
            store.persist_grades(record.trial_id, [GradeResult(
                grader_id="g", grader_version="v1", layer="outcome",
                score=Score(value=1.0), status="pass",
            )])
    store.close()
    return store_path


def test_report_compare_comparable_runs(tmp_path, runtime_lock):
    store_path = _two_runs_store(tmp_path, runtime_lock, same_lock=True)
    result = runner.invoke(app, [
        "report", "--store", str(store_path), "run-1", "run-2", "--compare",
    ])
    assert result.exit_code == 0, result.output
    assert "comparable" in result.output


def test_report_compare_refuses_incomparable_runs(tmp_path, runtime_lock):
    store_path = _two_runs_store(tmp_path, runtime_lock, same_lock=False)
    result = runner.invoke(app, [
        "report", "--store", str(store_path), "run-1", "run-2", "--compare",
    ])
    assert result.exit_code == 3
    assert "NOT comparable" in result.output


def test_report_compare_needs_two_runs(tmp_path, runtime_lock):
    store_path = _two_runs_store(tmp_path, runtime_lock, same_lock=True)
    result = runner.invoke(app, [
        "report", "--store", str(store_path), "run-1", "--compare",
    ])
    assert result.exit_code == 2


def test_report_refuses_unrecorded_run(tmp_path, runtime_lock):
    store_path = _two_runs_store(tmp_path, runtime_lock, same_lock=True)
    result = runner.invoke(app, [
        "report", "--store", str(store_path), "run-1", "run-ghost",
    ])
    assert result.exit_code == 3
    assert "not in the store" in result.output


# --- P0-2/E2E: the runtime lock must pin the sandbox image -------------


def test_run_pins_sandbox_image_in_runtime_lock(tmp_path, native_suite_dir, monkeypatch):
    """--sandbox-image/--sandbox-platform land in the runtime lock, where
    observed-identity binding compares the live sandbox against them."""
    import subprocess
    from types import SimpleNamespace
    from aeval.contracts import RuntimeLock

    # unlike harbor_calls, do NOT stub build_runtime_lock: the images
    # kwarg is exactly what this test verifies
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kwargs: SimpleNamespace(returncode=7),
    )
    monkeypatch.setattr("aeval.suite_loader.composition.suite_source_commit", lambda path: "a" * 40)
    run_dir = tmp_path / "myrun"
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(run_dir),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
        "--sandbox-image",
        "ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf",
        "--sandbox-platform", "arm64",
    ])
    assert result.exit_code == 7, result.output  # fake harbor fails; lock written first
    lock = RuntimeLock.model_validate_json(
        (run_dir / "runtime_lock.json").read_text(encoding="utf-8")
    )
    sandbox = lock.images["sandbox"]
    assert sandbox.platform == "arm64"
    assert sandbox.digest == "11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf"
    assert sandbox.reference.startswith("ubuntu@sha256:")


def test_run_sandbox_image_requires_platform(tmp_path, native_suite_dir, harbor_calls):
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(tmp_path / "myrun"),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
        "--sandbox-image", "ubuntu@sha256:" + "a" * 64,
    ])
    assert result.exit_code == 3
    assert not harbor_calls
    assert "requires --sandbox-platform" in result.output


def test_run_sandbox_image_must_be_digest_pinned(tmp_path, native_suite_dir, harbor_calls):
    result = runner.invoke(app, [
        "run", "--suite", str(native_suite_dir), "--run-dir", str(tmp_path / "myrun"),
        "--store", str(tmp_path / "store.sqlite3"), "--harbor-cli", "test-harbor",
        "--sandbox-image", "ubuntu:24.04", "--sandbox-platform", "arm64",
    ])
    assert result.exit_code == 3
    assert not harbor_calls
    assert "digest-pinned" in result.output
