"""Sandbox control bootstrap tests (P0-4 offline half)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aeval.contracts import RunBinding, TrialPaths
from aeval.control.bootstrap import (
    BootstrapError,
    SANDBOX_TOKEN_PATH,
    bootstrap_trial_control,
    compose_control_config,
)
from aeval.control.broker import ModelBrokerProcess
from aeval.hooks.context import EvaluationContext


class FakeUploadEnvironment:
    def __init__(self, *, fail=False):
        self.uploads: list[tuple[str, str]] = []
        self.uploaded_contents: list[str] = []
        self.fail = fail

    async def upload_file(self, source: str, target: str):
        if self.fail:
            raise OSError("upload failed")
        self.uploads.append((source, target))
        self.uploaded_contents.append(Path(source).read_text(encoding="utf-8"))


class _FakeBroker:
    """Just enough surface for bootstrap: a started broker."""

    def __init__(self, tmp_path: Path, *, token="job-token", missing=False,
                 empty=False, not_started=False):
        self.url = None if not_started else "http://127.0.0.1:4321"
        token_path = tmp_path / "broker-token"
        if not missing:
            token_path.write_text("" if empty else token, encoding="utf-8")
        self.token_path = None if not_started else token_path


def _paths(trial_dir: Path) -> TrialPaths:
    return TrialPaths(
        sandbox_cwd="/workspace",
        dsh_home="/logs/agent/dsh-home",
        bundle_path="/logs/agent/bundle_descriptor.json",
        session_root="dsh-home",
        download_root="trials/t/agent",
    )


async def _context(demo_suite, runtime_lock, tmp_path) -> EvaluationContext:
    ctx = EvaluationContext(
        run_id="r", runtime_lock=runtime_lock, suite=demo_suite,
        run_dir=tmp_path, store_path=tmp_path / "s.db",
    )
    ctx.run_binding = RunBinding(
        run_id="r", job_config_hash="b" * 64,
        config_file_sha256="c" * 64, runtime_lock_digest=ctx.runtime_lock.digest(),
    )
    # start_trial needs a full Harbor event; unit-level we plant the state
    # directly (integration coverage of start_trial lives elsewhere).
    from aeval.hooks.context import TrialState

    state = TrialState(trial_id="t", phase="running")
    state.trial_dir = tmp_path / "trials" / "t"
    state.trial_dir.mkdir(parents=True, exist_ok=True)
    ctx.trials["t"] = state
    return ctx


async def test_bootstrap_uploads_token_and_binds_control(
    demo_suite, runtime_lock, tmp_path
):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    env = FakeUploadEnvironment()
    broker = _FakeBroker(tmp_path)

    binding, config = await bootstrap_trial_control(
        environment=env, context=ctx, trial_id="t",
        paths=_paths(tmp_path), broker=broker,
        provider="offline-openai", model="test-model",
    )

    # token uploaded to the fixed sandbox path, exactly once, verbatim
    assert len(env.uploads) == 1
    source, target = env.uploads[0]
    assert target == SANDBOX_TOKEN_PATH.as_posix()
    assert env.uploaded_contents == ["job-token"]

    # config carries the broker URL, pinned identity, no aux calls
    assert config["gatewayUrl"] == "http://127.0.0.1:4321"
    assert config["jobTokenFile"] == SANDBOX_TOKEN_PATH.as_posix()
    assert config["refuseAuxiliaryCalls"] is True
    assert config["provider"] == "offline-openai"
    assert config["model"] == "test-model"

    # the owner binding is the plugin's trusted one
    state = ctx.trials["t"]
    assert state.binding is binding
    assert binding.config_digest == config["configDigest"]


def test_compose_control_config_digest_is_deterministic(runtime_lock):
    paths = TrialPaths(
        sandbox_cwd="/w", dsh_home="/h", bundle_path="/b.json",
        session_root="h", download_root="d",
    )
    base = dict(
        run_binding={"run_id": "r"},
        trial_id="t", session_id="s", paths=paths,
        gateway_url="http://127.0.0.1:1",
    )
    a = compose_control_config(provider="p", model="m", **base)
    b = compose_control_config(provider="p", model="m", **base)
    assert a["configDigest"] == b["configDigest"]
    # any field change flips the digest
    c = compose_control_config(provider="p", model="m2", **base)
    assert c["configDigest"] != a["configDigest"]


async def test_bootstrap_rejects_unstarted_broker(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    with pytest.raises(BootstrapError, match="broker is not ready"):
        await bootstrap_trial_control(
            environment=FakeUploadEnvironment(), context=ctx, trial_id="t",
            paths=_paths(tmp_path), broker=_FakeBroker(tmp_path, not_started=True),
            provider="p", model="m",
        )


async def test_bootstrap_rejects_missing_token_file(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    with pytest.raises(BootstrapError, match="did not write its job token"):
        await bootstrap_trial_control(
            environment=FakeUploadEnvironment(), context=ctx, trial_id="t",
            paths=_paths(tmp_path), broker=_FakeBroker(tmp_path, missing=True),
            provider="p", model="m",
        )


async def test_bootstrap_rejects_empty_token(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    with pytest.raises(BootstrapError, match="job token is empty"):
        await bootstrap_trial_control(
            environment=FakeUploadEnvironment(), context=ctx, trial_id="t",
            paths=_paths(tmp_path), broker=_FakeBroker(tmp_path, empty=True),
            provider="p", model="m",
        )


async def test_bootstrap_rejects_unknown_trial(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    with pytest.raises(BootstrapError, match="no session id for this trial"):
        await bootstrap_trial_control(
            environment=FakeUploadEnvironment(), context=ctx, trial_id="ghost",
            paths=_paths(tmp_path), broker=_FakeBroker(tmp_path),
            provider="p", model="m",
        )


async def test_bootstrap_rejects_environment_without_upload(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)

    class NoUpload:
        pass

    with pytest.raises(BootstrapError, match="no upload_file"):
        await bootstrap_trial_control(
            environment=NoUpload(), context=ctx, trial_id="t",
            paths=_paths(tmp_path), broker=_FakeBroker(tmp_path),
            provider="p", model="m",
        )


async def test_bootstrap_rejects_owner_binding_mismatch(demo_suite, runtime_lock, tmp_path):
    """A control config that disagrees with the trusted run identity must
    be refused by the owner binding (fail-closed, no sandbox run)."""
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    # paths whose download_root does not match the trial's agent dir
    bad = _paths(tmp_path).model_copy(update={"download_root": "elsewhere/agent"})
    with pytest.raises(BootstrapError, match="owner refused the control binding"):
        await bootstrap_trial_control(
            environment=FakeUploadEnvironment(), context=ctx, trial_id="t",
            paths=bad, broker=_FakeBroker(tmp_path),
            provider="p", model="m",
        )
