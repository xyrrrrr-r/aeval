"""Owner-side trial/environment access seam.

Harbor hook events carry no environment object; the owner wraps the
queue's ``_setup_hooks`` to reach the live ``Trial`` and its started
``agent_environment``. These tests pin the seam's behaviour and its
fail-closed contract.
"""

from __future__ import annotations

import pytest

from aeval.hooks.environment_access import (
    EnvironmentAccessError,
    TrialEnvironmentRegistry,
    install_trial_capture,
)


class FakeEnv:
    network_policy = "no-network"


class FakeTrial:
    def __init__(self, trial_id, *, environment=None, agent=None):
        self.id = trial_id
        self.agent_environment = environment
        self.agent = agent


class FakeQueue:
    def __init__(self):
        self.calls = []

    def _setup_hooks(self, trial):
        self.calls.append(trial)
        return "wired"


class FakeJob:
    def __init__(self, queue):
        self._trial_queue = queue


def test_registry_captures_and_resolves():
    registry = TrialEnvironmentRegistry()
    env = FakeEnv()
    trial = FakeTrial("t-1", environment=env, agent="agent-obj")
    registry.capture(trial)
    assert "t-1" in registry
    assert len(registry) == 1
    assert registry.trial("t-1") is trial
    assert registry.environment("t-1") is env
    assert registry.agent("t-1") == "agent-obj"
    registry.forget("t-1")
    assert "t-1" not in registry


def test_registry_lookups_are_fail_closed():
    registry = TrialEnvironmentRegistry()
    # unknown trial → None (callers must treat as "cannot observe")
    assert registry.environment("ghost") is None
    assert registry.agent("ghost") is None
    assert registry.trial("ghost") is None
    # a trial whose environment is absent yields None, never a stub
    registry.capture(FakeTrial("t-2", environment=None))
    assert registry.environment("t-2") is None


def test_capture_ignores_trials_without_an_id():
    registry = TrialEnvironmentRegistry()

    class NoId:
        pass

    registry.capture(NoId())
    assert len(registry) == 0


def test_install_wraps_setup_hooks_and_preserves_behaviour():
    queue = FakeQueue()
    job = FakeJob(queue)
    registry = TrialEnvironmentRegistry()
    install_trial_capture(job, registry)

    trial = FakeTrial("t-3", environment=FakeEnv())
    assert queue._setup_hooks(trial) == "wired"  # original result unchanged
    assert queue.calls == [trial]
    assert registry.environment("t-3") is trial.agent_environment


def test_install_is_idempotent():
    queue = FakeQueue()
    job = FakeJob(queue)
    registry = TrialEnvironmentRegistry()
    install_trial_capture(job, registry)
    first = queue._setup_hooks
    install_trial_capture(job, registry)
    assert queue._setup_hooks is first, "double install must not stack wrappers"


def test_original_setup_hooks_exception_propagates_unwrapped():
    class BoomQueue:
        def _setup_hooks(self, trial):
            raise RuntimeError("harbor wiring failed")

    registry = TrialEnvironmentRegistry()
    job = FakeJob(BoomQueue())
    install_trial_capture(job, registry)
    with pytest.raises(RuntimeError, match="harbor wiring failed"):
        job._trial_queue._setup_hooks(FakeTrial("t-4"))
    # a trial that never wired successfully is not recorded as capturable
    assert "t-4" not in registry


def test_install_fails_loudly_when_harbor_shape_changes():
    with pytest.raises(EnvironmentAccessError, match="no _trial_queue"):
        install_trial_capture(object(), TrialEnvironmentRegistry())

    class QueueWithoutSetup:
        pass

    with pytest.raises(EnvironmentAccessError, match="no _setup_hooks"):
        install_trial_capture(FakeJob(QueueWithoutSetup()), TrialEnvironmentRegistry())
