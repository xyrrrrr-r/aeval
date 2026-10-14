"""CLI-level ``aeval quickstart`` tests: the command surface and the exit codes.

The stages themselves are covered with injected leaves in
``tests/unit/test_quickstart.py``; here the real CLI runs, so these assert what
a script actually sees: the plan, the two ways of writing nothing, and a
non-zero exit when the backend cannot be used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeval.cli import EXIT_VALIDATION_ERROR, app
from aeval.quickstart import EXIT_NO_VALID_TRIALS

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_quickstart_help_documents_the_contract():
    result = runner.invoke(app, ["quickstart", "--help"])
    assert result.exit_code == 0
    for option in ("--backend", "--suite", "--agents-dir", "--workspace",
                   "--dry-run", "--yes", "--install-prefix", "--token-count-port"):
        assert option in result.output
    assert "/root/dshpkg" in result.output


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path):
    workspace = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "quickstart", "--backend", "docker",
            "--suite", str(REPO / "suites" / "tbench-pilot"),
            "--agents-dir", str(REPO / "agents"),
            "--workspace", str(workspace),
            "--listen-host", "192.0.2.10",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "quickstart plan" in result.output
    assert "broker-spec" in result.output
    assert "nothing was written" in result.output
    assert not workspace.exists()


def test_yes_is_required_to_write_anything(tmp_path):
    workspace = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "quickstart", "--backend", "docker",
            "--suite", str(REPO / "suites" / "tbench-pilot"),
            "--agents-dir", str(REPO / "agents"),
            "--workspace", str(workspace),
            "--listen-host", "192.0.2.10",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not workspace.exists()


def test_e2b_without_a_credential_exits_non_zero_and_writes_no_report(
    tmp_path, monkeypatch
):
    """The e2b backend cannot run here; the contract is to say what is missing."""
    for name in ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "aeval.provenance.verify_e2b_backend", lambda *args, **kwargs: "1.0.0"
    )
    workspace = tmp_path / "ws-e2b"
    result = runner.invoke(
        app,
        [
            "quickstart", "--backend", "e2b",
            "--suite", str(REPO / "suites" / "e2e-hello"),
            "--agents-dir", str(REPO / "agents"),
            "--workspace", str(workspace),
            "--yes",
        ],
    )
    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert "E2B_API_KEY" in result.output
    assert not (workspace / "report.md").exists()


def test_the_docker_backend_refuses_a_suite_its_tasks_cannot_survive(tmp_path):
    """e2e-hello's tasks are no-network, which docker cannot enforce."""
    result = runner.invoke(
        app,
        [
            "quickstart", "--backend", "docker",
            "--suite", str(REPO / "suites" / "e2e-hello"),
            "--agents-dir", str(REPO / "agents"),
            "--workspace", str(tmp_path / "ws"),
            "--dry-run",
        ],
    )
    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert "no-network" in result.output
    assert "hint:" in result.output


def test_the_exit_code_for_a_run_that_scored_nothing_is_its_own():
    assert EXIT_NO_VALID_TRIALS not in (0, EXIT_VALIDATION_ERROR)
    assert json.loads(json.dumps({"exit": EXIT_NO_VALID_TRIALS}))["exit"] == 6
