"""CLI integration tests (plan §7 row 14): exit codes and side effects.

``run`` must delegate to Harbor (never build its own loop) and pass the
AEVAL_* contract via the environment; a validation failure must reach
Harbor zero times. Exit codes: 0 ok, 2 param, 3 validation, 4 system.
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

STUB_HARBOR = "@echo off\r\necho %AEVAL_RUN_ID%> \"%~dp0stub_invoked.txt\"\r\nexit /b 7\r\n"


def _write_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "stub-harbor.cmd"
    stub.write_text(STUB_HARBOR, encoding="utf-8")
    return stub


def _invoked(tmp_path: Path) -> bool:
    return (tmp_path / "stub_invoked.txt").is_file()


def test_probe_ok():
    result = runner.invoke(app, ["probe", "--suite", str(DEMO)])
    assert result.exit_code == 0
    assert "refund-policy" in result.output


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


def test_explain_renders_read_only_view():
    result = runner.invoke(app, ["explain", str(DEMO)])
    assert result.exit_code == 0


def test_run_delegates_to_harbor_with_env_contract(tmp_path):
    stub = _write_stub(tmp_path)
    run_dir = tmp_path / "myrun"
    result = runner.invoke(app, [
        "run",
        "--suite", str(DEMO),
        "--run-dir", str(run_dir),
        "--store", str(tmp_path / "store.sqlite3"),
        "--harbor-cli", str(stub),
    ])
    # Harbor's exit code propagates (delegation, not orchestration)
    assert result.exit_code == 7
    assert _invoked(tmp_path)
    marker = (tmp_path / "stub_invoked.txt").read_text(encoding="utf-8").strip()
    assert marker == "run-myrun"
    assert (run_dir / "run_manifest.json").is_file()
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "run-myrun"
    assert manifest["overlay"]["suite_id"] == "refund-policy"
    assert manifest["sealed"] is False  # intent, sealed only at end


def test_run_invalid_suite_never_reaches_harbor(tmp_path):
    stub = _write_stub(tmp_path)
    result = runner.invoke(app, [
        "run",
        "--suite", str(tmp_path / "nowhere"),
        "--run-dir", str(tmp_path / "myrun"),
        "--store", str(tmp_path / "store.sqlite3"),
        "--harbor-cli", str(stub),
    ])
    assert result.exit_code == 3
    assert not _invoked(tmp_path)  # side effect never happened
    assert not (tmp_path / "myrun" / "run_manifest.json").is_file()


def test_run_dir_never_reused(tmp_path):
    stub = _write_stub(tmp_path)
    args = [
        "run",
        "--suite", str(DEMO),
        "--run-dir", str(tmp_path / "myrun"),
        "--store", str(tmp_path / "store.sqlite3"),
        "--harbor-cli", str(stub),
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 7
    second = runner.invoke(app, args)
    assert second.exit_code == 3
    assert "never reused" in second.output + str(second.stderr or "")


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
