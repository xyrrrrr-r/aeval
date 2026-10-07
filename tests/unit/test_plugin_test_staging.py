"""Suite-declared staging of a task's tests before evidence collection.

Terminal-Bench tasks publish their reward from ``tests/test.sh``. A
terminal-bench suite's collect command runs that script and reads
``/logs/verifier/reward.txt`` as an observable — but Harbor uploads
``tests/`` only at verification time, i.e. AFTER the collect phase, so
the suite must stage the tests itself once the agent has stopped.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aeval.hooks.broker_lifecycle import trial_control_paths
from aeval.hooks.plugin import TEST_STAGE_DIR, _stage_task_tests
from aeval.suite_models import DriverSpec


def _context(*, stage: bool, environment=None):
    return SimpleNamespace(
        suite=SimpleNamespace(overlay=SimpleNamespace(
            driver=SimpleNamespace(stage_tests_before_collect=stage)
        )),
        environments=(
            None if environment is None
            else SimpleNamespace(environment=lambda trial_id: environment)
        ),
    )


def _event(task_path: Path):
    return SimpleNamespace(config=SimpleNamespace(task=SimpleNamespace(path=task_path)))


def _state():
    return SimpleNamespace(trial_id="trial-1", evidence_issues=[])


class _Env:
    def __init__(self, *, fail: bool = False, missing_api: bool = False) -> None:
        self.uploads: list[tuple[Path, str]] = []
        self._fail = fail
        if not missing_api:
            self.upload_dir = self._upload

    async def _upload(self, *, source_dir: Path, target_dir: str) -> None:
        if self._fail:
            raise RuntimeError("sandbox upload exploded")
        self.uploads.append((Path(source_dir), target_dir))


@pytest.mark.parametrize("stage", [True, False])
def test_driver_flag_round_trips_and_defaults_off(stage):
    assert DriverSpec().stage_tests_before_collect is False
    assert DriverSpec(stage_tests_before_collect=stage).stage_tests_before_collect is stage


async def test_stages_the_task_tests_into_the_sandbox(tmp_path):
    task_dir = tmp_path / "tasks" / "t1"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    env = _Env()
    state = _state()

    await _stage_task_tests(_event(task_dir), _context(stage=True, environment=env), state)

    assert env.uploads == [(task_dir / "tests", TEST_STAGE_DIR)]
    assert state.evidence_issues == []


async def test_does_nothing_when_the_suite_does_not_opt_in(tmp_path):
    task_dir = tmp_path / "tasks" / "t1"
    (task_dir / "tests").mkdir(parents=True)
    env = _Env()
    state = _state()

    await _stage_task_tests(_event(task_dir), _context(stage=False, environment=env), state)

    assert env.uploads == []
    assert state.evidence_issues == []


@pytest.mark.parametrize(
    "make_env, issue_fragment",
    [
        (lambda: None, "no environment handle"),
        (lambda: _Env(missing_api=True), "no upload_dir"),
    ],
)
async def test_missing_upload_path_is_recorded_not_ignored(
    tmp_path, make_env, issue_fragment
):
    task_dir = tmp_path / "tasks" / "t1"
    (task_dir / "tests").mkdir(parents=True)
    state = _state()

    await _stage_task_tests(
        _event(task_dir), _context(stage=True, environment=make_env()), state
    )

    assert len(state.evidence_issues) == 1
    assert issue_fragment in state.evidence_issues[0]


async def test_missing_tests_directory_is_recorded(tmp_path):
    state = _state()

    await _stage_task_tests(
        _event(tmp_path / "tasks" / "t1"), _context(stage=True, environment=_Env()), state
    )

    assert len(state.evidence_issues) == 1
    assert "is missing" in state.evidence_issues[0]


async def test_upload_failure_is_recorded_so_collection_fails_loudly(tmp_path):
    task_dir = tmp_path / "tasks" / "t1"
    (task_dir / "tests").mkdir(parents=True)
    state = _state()

    await _stage_task_tests(
        _event(task_dir), _context(stage=True, environment=_Env(fail=True)), state
    )

    assert len(state.evidence_issues) == 1
    assert "staging task tests failed" in state.evidence_issues[0]


async def test_trial_without_a_task_path_is_recorded():
    state = _state()
    event = SimpleNamespace(config=SimpleNamespace(task=SimpleNamespace(path=None)))

    await _stage_task_tests(event, _context(stage=True, environment=_Env()), state)

    assert len(state.evidence_issues) == 1
    assert "no task path" in state.evidence_issues[0]


def test_workspace_dir_defaults_to_the_owner_convention():
    """Unchanged default: a suite that says nothing keeps /workspace, and
    the owner-side control paths follow the suite's declaration."""
    assert DriverSpec().workspace_dir == "/workspace"
    assert DriverSpec(workspace_dir="/app").workspace_dir == "/app"


def _paths_state(tmp_path: Path):
    return SimpleNamespace(trial_dir=tmp_path / "run" / "trial-1")


def test_control_paths_take_the_sandbox_cwd_from_the_driver(tmp_path):
    """The lease/session plumbing must use the SAME cwd the agent runs in.

    DSH refuses to resume a session recorded in another directory, so a
    suite that declares ``workspace_dir`` and a control path that ignores
    it would fail every trial at session resume; and the two must match
    the task's own WORKDIR or the agent writes where the tests never look
    (measured: every pilot trial failed on /app/hello.txt)."""
    run_dir = tmp_path / "run"
    state = _paths_state(tmp_path)
    state.trial_dir.mkdir(parents=True)
    (state.trial_dir / "agent").mkdir()

    default = trial_control_paths(state, run_dir)
    assert default.sandbox_cwd == "/workspace"

    driver = DriverSpec(workspace_dir="/app")
    declared = trial_control_paths(state, run_dir, driver)
    assert declared.sandbox_cwd == "/app"
    # everything else is unchanged by the declaration
    assert declared.agent_home == default.agent_home
    assert declared.bundle_path == default.bundle_path
    assert declared.download_root == default.download_root
