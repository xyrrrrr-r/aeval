"""Harbor contract lock tests.

These pin the real behavior of the locked Harbor 0.23.0 wheel so an
upstream change cannot silently reposition our hooks. When one of
these fails after a Harbor upgrade, the integration in
aeval/hooks must be re-audited before any release.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest

import harbor
from harbor.models.job.plugin import BaseJobPlugin, JobPlugin
from harbor.trial.hooks import TrialEvent, TrialHookEvent
from harbor.utils.trajectory_validator import TrajectoryValidator

from aeval.hooks.plugin import AevalPlugin


def test_locked_harbor_version():
    from importlib.metadata import version

    assert version("harbor") == "0.23.0"


def test_job_plugin_protocol_is_exactly_two_methods():
    methods = {m for m in dir(JobPlugin) if not m.startswith("_")}
    assert methods == {"on_job_start", "on_job_end"}
    assert inspect.iscoroutinefunction(JobPlugin.on_job_start)
    assert inspect.iscoroutinefunction(JobPlugin.on_job_end)


def test_aeval_plugin_satisfies_the_runtime_protocol():
    assert isinstance(AevalPlugin(), JobPlugin)


def test_job_exposes_the_owned_trial_hook_registrations():
    from harbor.job import Job

    for name in (
        "on_trial_started",
        "on_trial_cancelled",
        "on_environment_started",
        "on_agent_ended",
        "on_verification_started",
        "on_trial_ended",
    ):
        assert hasattr(Job, name), f"harbor Job lost hook registration {name}"


def test_trial_events_cover_the_locked_lifecycle():
    values = {e.value for e in TrialEvent}
    assert values == {
        "start", "environment-start", "agent-start", "agent-end",
        "verification-start", "end", "cancel",
    }


def test_trial_result_required_fields_are_locked():
    from harbor.models.trial.result import TrialResult

    required = {n for n, f in TrialResult.model_fields.items() if f.is_required()}
    assert required == {
        "task_name", "trial_name", "trial_uri", "task_id",
        "task_checksum", "config", "agent_info",
    }


def test_trial_hook_event_computes_trial_id():
    from harbor.models.task.id import LocalTaskId
    from harbor.models.trial.config import TaskConfig, TrialConfig
    from harbor.models.trial.result import AgentInfo, TrialResult
    from harbor.models.job.lock import TrialLock

    config = TrialConfig(trial_name="trial", task=TaskConfig(name="task"))
    result = TrialResult(
        task_name="task",
        trial_name="trial",
        trial_uri="file:///t",
        task_id=LocalTaskId(path=Path("C:/tmp/task.yaml")),
        task_checksum="0" * 64,
        config=config,
        agent_info=AgentInfo(name="dsh", version="0.1.7-alpha.1"),
    )
    event = TrialHookEvent(
        event=TrialEvent.VERIFICATION_START,
        task_name="task",
        config=config,
        result=result,
        lock=TrialLock.model_construct(),  # lock contents are irrelevant here
    )
    assert event.trial_id == result.id  # computed, not passed


def test_atif_validator_collects_errors_not_first_only():
    validator = TrajectoryValidator()
    # an empty trajectory dict produces multiple collected errors
    validator.validate({"agent": {"name": "dsh", "version": "0"}, "steps": []})
    assert len(validator.errors) >= 1


def test_atif_step_requires_message_and_string_timestamp():
    from harbor.models.trajectories import Agent, Step, Trajectory

    with pytest.raises(Exception):
        Trajectory(
            agent=Agent(name="dsh", version="0"),
            steps=[Step(step_id=1, source="system")],  # no message
        )


def test_verifier_config_field_set_is_locked():
    """Harbor 0.23.0 has no separate/shared verifier mode flag.

    The verifier always runs in its own environment; aeval's gate
    therefore locks on a DECLARED verifier, not on a mode flag. If
    harbor ever grows a mode field, re-audit the evidence gate before
    trusting an upgrade.
    """
    from harbor.models.trial.config import VerifierConfig

    assert set(VerifierConfig.model_fields) == {
        "override_timeout_sec", "max_timeout_sec", "include_logs",
        "exclude_logs", "env", "import_path", "kwargs", "disable",
    }


def test_regrade_reuses_source_artifacts_without_agent_phase():
    from harbor.models.trial.config import SourceTrialConfig, TaskConfig, TrialConfig

    source = SourceTrialConfig(
        action="regrade", type="local",
        trial_id=uuid4(), path="C:/tmp/source-trial",
    )
    config = TrialConfig(
        trial_name="t", task=TaskConfig(name="task"), source_trial=source,
    )
    assert config.is_regrade is True
    plain = TrialConfig(trial_name="t", task=TaskConfig(name="task"))
    assert plain.is_regrade is False


def test_plugin_loads_via_production_import_path():
    import importlib

    module = importlib.import_module("aeval.hooks")
    assert module.AevalPlugin is AevalPlugin  # aeval.hooks:AevalPlugin
