"""Observed session identity (P1-2b).

When the adapter's recorder is a foreign runtime (the ACP runner mints its own
session id), one trial has two trusted identities: the id aeval minted for the
control wire, and the one the owner read out of the trial's OWN record. The
descriptor may state the observed one — and only when every other field still
matches the binding exactly.
"""

from __future__ import annotations

import pytest

from aeval.contracts import BundleDescriptor, RunBinding, TrialBinding, TrialPaths
from aeval.hooks.context import EvaluationContext, LifecycleError, TrialState


def _paths() -> TrialPaths:
    return TrialPaths(
        sandbox_cwd="/workspace",
        agent_home="/root/.deepagents",
        bundle_path="/logs/agent/bundle_descriptor.json",
        session_root=".",
        download_root="trials/t/agent",
    )


def _binding(trial_id: str, session_id: str, config_digest: str = "e" * 64) -> TrialBinding:
    return TrialBinding(
        run=RunBinding(
            run_id="run-hello",
            job_config_hash="b" * 64,
            config_file_sha256="c" * 64,
            runtime_lock_digest="d" * 64,
        ),
        trial_id=trial_id,
        session_id=session_id,
        config_digest=config_digest,
        paths=_paths(),
    )


def _descriptor(binding: TrialBinding, session_id: str, **overrides) -> BundleDescriptor:
    fields = {
        "schema_version": 2,
        "run": binding.run,
        "trial_id": binding.trial_id,
        "session_id": session_id,
        "session_root": binding.paths.session_root,
        "stop_reason": "agent_exit_0",
        "config_digest": binding.config_digest,
    }
    fields.update(overrides)
    return BundleDescriptor.model_validate(fields)


def _context(demo_suite, runtime_lock, tmp_path, state: TrialState) -> EvaluationContext:
    context = EvaluationContext(
        run_id="r",
        runtime_lock=runtime_lock,
        suite=demo_suite,
        run_dir=tmp_path,
        store_path=tmp_path / "s.db",
    )
    context.trials[state.trial_id] = state
    return context


def test_the_minted_identity_is_still_the_default(demo_suite, runtime_lock, tmp_path):
    state = TrialState(trial_id="t", session_id="wire-1", binding=_binding("t", "wire-1"))
    context = _context(demo_suite, runtime_lock, tmp_path, state)
    assert context.verify_descriptor("t", _descriptor(state.binding, "wire-1")) is None


def test_the_observed_agent_identity_is_accepted_once_it_is_known(
    demo_suite, runtime_lock, tmp_path
):
    state = TrialState(trial_id="t", session_id="wire-1", binding=_binding("t", "wire-1"))
    context = _context(demo_suite, runtime_lock, tmp_path, state)
    # nothing observed yet: an id nobody established is refused
    with pytest.raises(LifecycleError, match="session_id"):
        context.verify_descriptor("t", _descriptor(state.binding, "acp-1"))
    state.observed_agent_session_id = "acp-1"
    assert context.verify_descriptor("t", _descriptor(state.binding, "acp-1")) is None
    # an unrelated third identity is still refused
    with pytest.raises(LifecycleError, match="session_id"):
        context.verify_descriptor("t", _descriptor(state.binding, "acp-2"))


def test_the_observed_identity_does_not_weaken_any_other_field(
    demo_suite, runtime_lock, tmp_path
):
    """Substituting the identity must leave every other check strict."""
    state = TrialState(trial_id="t", session_id="wire-1", binding=_binding("t", "wire-1"))
    state.observed_agent_session_id = "acp-1"
    context = _context(demo_suite, runtime_lock, tmp_path, state)
    for field, value in (
        ("trial_id", "other-trial"),
        ("config_digest", "f" * 64),
        ("session_root", "elsewhere"),
    ):
        with pytest.raises(LifecycleError):
            context.verify_descriptor(
                "t", _descriptor(state.binding, "acp-1", **{field: value})
            )
    # ``stop_reason`` is deliberately NOT part of the binding: it is the
    # terminal observation, checked by the evidence gate against the sandbox's
    # own record, not against the owner's binding.
    assert (
        context.verify_descriptor("t", _descriptor(state.binding, "acp-1", stop_reason="crashed"))
        is None
    )


def test_the_observed_identity_never_leaks_across_trials(
    demo_suite, runtime_lock, tmp_path
):
    state = TrialState(trial_id="t", session_id="wire-1", binding=_binding("t", "wire-1"))
    state.observed_agent_session_id = "acp-1"
    context = _context(demo_suite, runtime_lock, tmp_path, state)
    # another trial's descriptor (same ids except the trial) is refused by the
    # ordinary binding check, observed identity or not
    other = _descriptor(state.binding, "acp-1", trial_id="t2")
    with pytest.raises(LifecycleError):
        context.verify_descriptor("t", other)
