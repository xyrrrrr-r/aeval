"""Sandbox control bootstrap tests (P0-4 offline half)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aeval.contracts import RunBinding, TrialPaths
from aeval.control.bootstrap import (
    BootstrapError,
    deploy_control_stack,
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


async def test_bootstrap_refuses_a_tainted_trial(demo_suite, runtime_lock, tmp_path):
    """The owner is the hard block: no model token for a trial already
    judged infra_invalid (baseline/policy/evidence failure)."""
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    ctx.trials["t"].mark_infra_invalid("baseline_arrival: seed mismatch")
    with pytest.raises(BootstrapError, match="refusing to deploy a model token"):
        await bootstrap_trial_control(
            environment=FakeUploadEnvironment(), context=ctx, trial_id="t",
            paths=_paths(tmp_path), broker=_FakeBroker(tmp_path),
            provider="p", model="m",
        )


def test_control_config_mirrors_the_lease_limits(runtime_lock):
    """The sandbox adapter compares the broker's /info against this
    config field by field; a lease with maxSteps and a config without it
    fails with AEVAL_LEASE_MISMATCH (found on the real sandbox)."""
    paths = TrialPaths(
        sandbox_cwd="/w", dsh_home="/h", bundle_path="/b.json",
        session_root="h", download_root="d",
    )
    base = dict(
        run_binding={"run_id": "r"}, trial_id="t", session_id="s", paths=paths,
        gateway_url="http://127.0.0.1:1", provider="p", model="m",
    )
    bare = compose_control_config(**base)
    assert "maxSteps" not in bare and "reasoningEffort" not in bare

    mirrored = compose_control_config(
        **base, reasoning_effort="high", limits={"maxSteps": 5, "maxTokens": 4096}
    )
    assert mirrored["maxSteps"] == 5
    assert mirrored["maxTokens"] == 4096
    assert mirrored["reasoningEffort"] == "high"
    # the sandboxed deployment lets the harness process finalize as owner
    assert mirrored["ownerFinalize"] is True
    # limits participate in the digest, so the two cannot be confused
    assert mirrored["configDigest"] != bare["configDigest"]


class _RecordingEnvironment:
    def __init__(self, *, mint_ok=True):
        self.uploads: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self.mint_ok = mint_ok

    async def upload_file(self, source: str, target: str):
        self.uploads.append((Path(source).name, target))

    async def exec(self, command: str):
        self.commands.append(command)
        if "session_stub.js" in command:
            return SimpleNamespace(
                return_code=0 if self.mint_ok else 1,
                stdout='{"ok":true}' if self.mint_ok else "",
            )
        return SimpleNamespace(return_code=0, stdout="")


class _Agent:
    def __init__(self, prefix="/dev/shm/dshpkg"):
        self._prefix = prefix
        self.patches: list[str] = []
        self.env: dict[str, str] = {}

    def cli_bin_dir(self):
        return f"{self._prefix}/bin" if self._prefix else None

    def add_patch_file(self, path):
        self.patches.append(str(path))

    def pin_session(self, session_id):
        self.pinned = session_id

    def set_workspace_dir(self, path):
        self.workspace = path

    def set_run_env(self, key, value):
        self.env[key] = value


def _paths_for_stack():
    return TrialPaths(
        sandbox_cwd="/workspace", dsh_home="/logs/agent/dsh-home",
        bundle_path="/logs/agent/bundle_descriptor.json",
        session_root="dsh-home", download_root="d/t/agent",
    )


async def test_deploy_control_stack_uploads_and_registers(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("export const x = 1;\n")
    (dist / "sandbox_entry.js").write_text("export const y = 2;\n")
    (dist / "session_stub.js").write_text("export const z = 3;\n")
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    env = _RecordingEnvironment()
    agent = _Agent()
    config = {"sessionId": "sess-1", "jobTokenFile": "/run/aeval/trial-token",
              "provider": "p", "model": "m", "maxSteps": 5}

    patch = await deploy_control_stack(
        environment=env, agent=agent, paths=_paths_for_stack(), config=config,
        control_dist=dist, control_ca=ca, trial_id="t-1",
    )

    # placed inside the DSH tree so the harness packages resolve
    assert "/@deepseek-ai/dsh/node_modules/aeval-control/" in patch
    targets = {t for _, t in env.uploads}
    assert any(t.endswith("/dist/index.js") for t in targets)
    assert any(t.endswith("/config.json") for t in targets)
    assert any(t.endswith("/ca.crt") for t in targets)
    assert any(t.endswith("/cordis.patch.yml") for t in targets)
    # the session root is prepared, then the stub mints the owner session
    assert any(c.startswith("mkdir -p /logs/agent/dsh-home/sessions") for c in env.commands)
    mint = next(c for c in env.commands if "session_stub.js" in c)
    assert "sess-1" in mint and "/logs/agent/dsh-home/sessions" in mint
    assert "--compression zstd" in mint
    # the agent now carries the patch and trusts the CA
    assert agent.patches and agent.patches[0].endswith("cordis.patch.yml")
    assert agent.pinned == "sess-1"
    assert agent.workspace == "/workspace"
    assert agent.env["NODE_EXTRA_CA_CERTS"].endswith("/ca.crt")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda a: setattr(a, "_prefix", None), "no CLI install prefix"),
    ],
)
async def test_deploy_control_stack_fails_closed(tmp_path, mutate, message):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("x")
    agent = _Agent()
    mutate(agent)
    with pytest.raises(BootstrapError, match=message):
        await deploy_control_stack(
            environment=_RecordingEnvironment(), agent=agent,
            paths=_paths_for_stack(), config={"sessionId": "s"},
            control_dist=dist, control_ca=None, trial_id="t",
        )


async def test_deploy_control_stack_rejects_an_empty_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    with pytest.raises(BootstrapError, match="no built .js files"):
        await deploy_control_stack(
            environment=_RecordingEnvironment(), agent=_Agent(),
            paths=_paths_for_stack(), config={"sessionId": "s"},
            control_dist=dist, control_ca=None, trial_id="t",
        )


async def test_deploy_control_stack_requires_a_minted_session(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("x")
    with pytest.raises(BootstrapError, match="could not be minted"):
        await deploy_control_stack(
            environment=_RecordingEnvironment(mint_ok=False), agent=_Agent(),
            paths=_paths_for_stack(), config={"sessionId": "s", "jobTokenFile": "/t"},
            control_dist=dist, control_ca=None, trial_id="t",
        )


def test_control_config_mirrors_the_auxiliary_policy(runtime_lock):
    """D47: an allowed purpose is dispatched and ledgered, so the control
    config must mirror the broker's served policy or the sandbox adapter
    fails the lease identity check at /info."""
    paths = TrialPaths(
        sandbox_cwd="/w", dsh_home="/h", bundle_path="/b.json",
        session_root="h", download_root="d",
    )
    base = dict(
        run_binding={"run_id": "r"}, trial_id="t", session_id="s", paths=paths,
        gateway_url="http://127.0.0.1:1", provider="p", model="m",
    )
    bare = compose_control_config(**base)
    assert "auxiliaryPolicy" not in bare, "an ordinary deployment keeps its digest unchanged"
    assert bare["refuseAuxiliaryCalls"] is True

    mirrored = compose_control_config(
        **base, auxiliary_policy={"compaction": "allow"}
    )
    assert mirrored["auxiliaryPolicy"] == {"compaction": "allow"}
    assert mirrored["refuseAuxiliaryCalls"] is True
    # the policy participates in the digest, so the two cannot be confused
    assert mirrored["configDigest"] != bare["configDigest"]


# --- P2-4: the control stack is deployed only where it is declared -------------

class _StackAgent:
    CONTROL_STACK = "dsh"


class _NoStackAgent:
    """A second agent that needs none of the DSH machinery."""


async def test_an_agent_without_a_declared_stack_gets_none_deployed(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    """The framework must not push DSH's control stack into another agent."""
    import aeval.control.bootstrap as module

    async def _must_not_run(**kwargs):  # noqa: ANN003
        raise AssertionError("the control stack was deployed into an undeclared adapter")

    monkeypatch.setattr(module, "deploy_control_stack", _must_not_run)
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    env = FakeUploadEnvironment()
    broker = _FakeBroker(tmp_path)

    binding, config = await bootstrap_trial_control(
        environment=env, context=ctx, trial_id="t",
        paths=_paths(tmp_path), broker=broker,
        provider="offline-openai", model="test-model",
        agent=_NoStackAgent(), control_dist=tmp_path,
    )
    # the agent-neutral part still happens: token uploaded, control bound
    assert env.uploaded_contents == ["job-token"]
    assert config["gatewayUrl"] == "http://127.0.0.1:4321"
    assert binding is not None


async def test_an_agent_with_a_declared_stack_gets_it_deployed(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    import aeval.control.bootstrap as module

    calls = []

    async def _record(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return "cordis.patch.yml"

    monkeypatch.setattr(module, "deploy_control_stack", _record)
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    broker = _FakeBroker(tmp_path)

    await bootstrap_trial_control(
        environment=FakeUploadEnvironment(), context=ctx, trial_id="t",
        paths=_paths(tmp_path), broker=broker,
        provider="offline-openai", model="test-model",
        agent=_StackAgent(), control_dist=tmp_path,
    )
    assert len(calls) == 1
    assert calls[0]["agent"].__class__ is _StackAgent
