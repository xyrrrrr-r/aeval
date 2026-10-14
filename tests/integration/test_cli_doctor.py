"""CLI-level ``aeval doctor`` tests: the command surface, exit codes, --json.

The check logic itself is covered branch by branch in
``tests/unit/test_doctor.py`` with injected leaves; here the real CLI runs, so
these tests assert the contract a user and a script see: the documented line
format, the hint lines, and an exit code that agrees with the report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aeval.cli import app
from aeval.doctor import CHECK_IDS, DOCTOR_SCHEMA

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()
EXIT_DOCTOR_FAIL = 3


def _control_package(root: Path, *, reader: bool = True) -> Path:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "broker_main.js").write_text("// broker\n", encoding="utf-8")
    if reader:
        (dist / "session_reader.js").write_text("// reader\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "dsh-eval-control", "version": "0.2.6"}),
        encoding="utf-8",
    )
    return root


def _spec(path: Path, control_root: Path, **overrides) -> Path:
    data = {
        "controlRoot": str(control_root),
        "listenHost": "10.0.0.12",
        "listenPort": 8447,
        "listenTls": {"cert": str(path.parent / "gateway.crt"),
                      "key": str(path.parent / "gateway.key")},
        "controlCa": str(path.parent / "ca.crt"),
        "upstream": {
            "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "model": "deepseek-chat",
        },
        "identity": {"provider": "deepseek", "model": "deepseek-chat"},
        "limits": {"maxSteps": 60},
        "maxOutputTokens": 16384,
        "tokenCount": {"endpoint": "http://127.0.0.1:8791/tokens/count", "margin": 8},
    }
    data.update(overrides)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """No ambient broker spec, no DSH home, no upstream key from the outer shell."""
    for name in ("AEVAL_BROKER_JSON", "DEEPSEEK_API_KEY", "E2B_API_KEY",
                 "AEVAL_DSH_CONTROL_ROOT", "DSH_HOME", "DSH_DIR"):
        monkeypatch.delenv(name, raising=False)
    return {
        "HOME": str(tmp_path / "home"),
        "DSH_HOME": str(tmp_path / "home" / "dsh"),
        "NPM_CONFIG_PREFIX": str(tmp_path / "npm-prefix"),
    }


def test_doctor_json_is_stable_and_exit_code_matches_the_report(tmp_path, isolated_env):
    result = runner.invoke(
        app,
        ["doctor", "--json", "--no-probe", "--agents-dir", str(REPO / "agents")],
        env=isolated_env,
    )
    document = json.loads(result.output)
    assert document["schema"] == DOCTOR_SCHEMA
    assert document["tool"] == "aeval"
    assert set(document) == {"schema", "tool", "mode", "summary", "checks", "exit_code"}
    assert [check["id"] for check in document["checks"]] == list(CHECK_IDS)
    for check in document["checks"]:
        assert set(check) == {"id", "status", "detail", "hint"}
    failed = [check for check in document["checks"] if check["status"] == "fail"]
    assert document["summary"]["fail"] == len(failed)
    assert document["exit_code"] == (EXIT_DOCTOR_FAIL if failed else 0)
    assert result.exit_code == document["exit_code"]


def test_doctor_prints_lines_and_hints_and_fails_nonzero(tmp_path, isolated_env):
    spec = _spec(tmp_path / "broker.json", _control_package(tmp_path / "ctrl", reader=False))
    result = runner.invoke(
        app,
        ["doctor", "--broker-json", str(spec), "--no-probe",
         "--agents-dir", str(REPO / "agents"), "--backend", "docker"],
        env=isolated_env,
    )
    assert result.exit_code == EXIT_DOCTOR_FAIL, result.output
    lines = result.output.splitlines()
    assert lines[0] == "aeval doctor — mode: active"
    assert any(line.startswith("  [fail] control-dist") for line in lines)
    hints = [line for line in lines if line.startswith("  hint: ")]
    assert hints, result.output
    for line in hints:
        assert line[len("  hint: "):].strip()


def test_doctor_reports_the_pairing_and_runtime_image_checks(tmp_path, isolated_env):
    result = runner.invoke(
        app,
        [
            "doctor", "--no-probe",
            "--suite", str(REPO / "suites" / "e2e-hello"),
            "--agent", "dsh",
            "--agents-dir", str(REPO / "agents"),
        ],
        env=isolated_env,
    )
    assert "  [ok] pairing" in result.output, result.output
    assert "composed" in result.output
    assert "  [ok] runtime-image" in result.output


def test_no_probe_turns_the_counter_probe_into_a_warning(tmp_path, isolated_env):
    control = _control_package(tmp_path / "ctrl")
    spec = _spec(tmp_path / "broker.json", control)
    (tmp_path / "gateway.crt").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "gateway.key").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "ca.crt").write_text("ca\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["doctor", "--json", "--no-probe", "--broker-json", str(spec),
         "--agents-dir", str(REPO / "agents")],
        env=isolated_env,
    )
    document = json.loads(result.output)
    counter = next(c for c in document["checks"] if c["id"] == "token-endpoint")
    assert counter["status"] == "warn" and "--no-probe" in counter["detail"]


def test_doctor_never_writes_to_the_workspace(tmp_path, isolated_env):
    control = _control_package(tmp_path / "ctrl")
    spec = _spec(tmp_path / "broker.json", control)
    run_dir = tmp_path / "runs" / "1"
    (run_dir / "trials" / "t1").mkdir(parents=True)
    (run_dir / "trials" / "t1" / "collection_manifest.json").write_text("{}", encoding="utf-8")

    def snapshot() -> dict:
        return {
            str(path.relative_to(tmp_path)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    runner.invoke(
        app,
        ["doctor", "--no-probe", "--broker-json", str(spec),
         "--agents-dir", str(REPO / "agents"), "--run-dir", str(run_dir)],
        env=isolated_env,
    )
    assert snapshot() == before


def test_standalone_run_warns_about_the_missing_spec(tmp_path, isolated_env):
    result = runner.invoke(
        app,
        ["doctor", "--no-probe", "--agents-dir", str(REPO / "agents")],
        env=isolated_env,
    )
    assert "  [warn] broker-spec" in result.output
    assert "AEVAL_BROKER_JSON is not set" in result.output
