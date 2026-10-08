"""``aeval trajectory`` renders one task to stdout or every task to a directory.

The panel body itself is covered by the metrics/mapper suites; this test pins
the command's *orchestration* — the two mutually exclusive modes, per-task
grouping, batch file writing, and the honest filename mapping — without driving
the whole sealing pipeline. ``TrialStore`` and the render helper are stubbed so
the assertions are about argument handling and output shape, not evidence.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aeval.cli import _trajectory_task_filename, app

runner = CliRunner()


def _record(task_id: str, index: int) -> SimpleNamespace:
    return SimpleNamespace(
        trial_id=f"{task_id}-t{index}",
        coordinates=SimpleNamespace(task_id=task_id, trial_index=index),
    )


class _FakeStore:
    """Stand-in for TrialStore: one manifest, a fixed list of trial records."""

    def __init__(self, path: Path):
        self._records = _RECORDS

    def load_run_manifest(self, run_id: str):
        return SimpleNamespace(
            overlay=SimpleNamespace(suite_id="s", suite_version="1"),
            task_titles={},
        )

    def list_trials(self, run_ids):
        return list(self._records)

    def close(self):
        pass


# Two tasks, several trials, deliberately out of task order to exercise grouping.
_RECORDS = [_record("b.task", 0), _record("a.task", 1), _record("b.task", 2), _record("a.task", 0)]


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr("aeval.store.sqlite.TrialStore", _FakeStore)
    monkeypatch.setattr("aeval.cli._resolve_turn_metrics_factory", lambda suites_dir, manifest: (None, None))
    # Render returns a task-keyed sentinel so the test sees which tasks ran and
    # can confirm each landed in its own file.
    monkeypatch.setattr("aeval.cli._render_task_panel",
                        lambda **kw: f"<html>{kw['task_id']}</html>")


def test_batch_mode_writes_one_file_per_task(tmp_path, stubbed):
    out = tmp_path / "panels"
    result = runner.invoke(app, [
        "trajectory", "--store", str(tmp_path / "store.sqlite3"), "run-1", "--out", str(out),
    ])
    assert result.exit_code == 0, result.output
    files = sorted(p.name for p in out.glob("*.html"))
    assert files == ["a.task.html", "b.task.html"]
    assert (out / "a.task.html").read_text(encoding="utf-8") == "<html>a.task</html>"
    assert (out / "b.task.html").read_text(encoding="utf-8") == "<html>b.task</html>"


def test_batch_mode_rejects_a_task_argument(tmp_path, stubbed):
    result = runner.invoke(app, [
        "trajectory", "--store", str(tmp_path / "store.sqlite3"), "run-1",
        "a.task", "--out", str(tmp_path / "panels"),
    ])
    assert result.exit_code != 0
    assert "drop the TASK" in result.stderr


def test_single_mode_still_prints_to_stdout(tmp_path, stubbed):
    result = runner.invoke(app, [
        "trajectory", "--store", str(tmp_path / "store.sqlite3"), "run-1", "a.task",
    ])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "<html>a.task</html>"


def test_missing_task_and_out_is_an_error(tmp_path, stubbed):
    result = runner.invoke(app, [
        "trajectory", "--store", str(tmp_path / "store.sqlite3"), "run-1",
    ])
    assert result.exit_code != 0
    assert "--out" in result.stderr


def test_unknown_task_in_single_mode_is_an_error(tmp_path, stubbed):
    result = runner.invoke(app, [
        "trajectory", "--store", str(tmp_path / "store.sqlite3"), "run-1", "nope",
    ])
    assert result.exit_code != 0
    assert "no trials for task" in result.stderr


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("memory.tenant_isolation", "memory.tenant_isolation.html"),
        ("alpha.one", "alpha.one.html"),
        ("weird/../id", "weird_.._id.html"),
        ("", ".html"),
    ],
)
def test_task_filename_is_escape_safe(task_id, expected):
    got = _trajectory_task_filename(task_id)
    assert got == expected
    assert "/" not in got and "\\" not in got
