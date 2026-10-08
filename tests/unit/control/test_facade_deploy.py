"""Generic facade deployment flavor tests.

The generic flavor is what an agent whose runner starts it itself needs:
there is no plugin tree to graft and no patch to apply, so the deployment is
upload → background start → health gate. These tests pin the parts that must
not drift: discovery, the shipped closure, the exact start contract, and the
dispatch that decides which flavor runs.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import shutil

import pytest

from aeval.contracts import ControlDistLock, RunBinding, TrialPaths
# importing the dsh flavor registers it (the adapter package IS the
# registration) — the dsh-dispatch tests below need it in the registry
import aeval.agents.dsh.control_flavor  # noqa: F401
from aeval.control.bootstrap import (
    FACADE_SANDBOX_ROOT,
    BootstrapError,
    _deploy_declared_stack,
    _facade_runtime_files,
    bootstrap_trial_control,
    deploy_generic_facade,
    facade_dist_candidates,
    resolve_facade_dist,
)
from aeval.hooks.context import EvaluationContext


class _FakeExecEnvironment:
    """An environment that records commands and can script their outcomes."""

    def __init__(self, *, healthy_after=1, extract_code=0, start_code=0, log="boom"):
        self.commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.blobs: dict[str, bytes] = {}
        self.probes = 0
        self._healthy_after = healthy_after
        self._extract_code = extract_code
        self._start_code = start_code
        self._log = log

    async def upload_file(self, source: str, target: str):
        self.uploads.append((source, target))
        self.blobs[target] = Path(source).read_bytes()

    async def exec(self, command: str):
        self.commands.append(command)
        if "healthz" in command:
            self.probes += 1
            return SimpleNamespace(return_code=0 if self.probes >= self._healthy_after else 1, stdout="")
        if command.startswith("tar -xzf"):
            return SimpleNamespace(return_code=self._extract_code, stdout="")
        if "facade_main.js" in command:
            return SimpleNamespace(return_code=self._start_code, stdout="facade-pid=42")
        if command.startswith("cat "):
            return SimpleNamespace(return_code=0, stdout=self._log)
        return SimpleNamespace(return_code=0, stdout="")


def _built_dist(root: Path, names=("facade_main.js", "gateway_lease.js")) -> Path:
    dist = root / "deepagents-eval-control" / "dist"
    dist.mkdir(parents=True)
    for name in names:
        (dist / name).write_text(f"export const {name.replace('.', '_')} = 1;\n", encoding="utf-8")
    (dist.parent / "package.json").write_text('{"name": "deepagents-eval-control"}\n', encoding="utf-8")
    modules = dist.parent / "node_modules"
    (modules / "@deepseek-ai" / "dsh-llm").mkdir(parents=True)
    (modules / "@deepseek-ai" / "dsh-llm" / "package.json").write_text("{}\n", encoding="utf-8")
    (modules / "typescript").mkdir()
    (modules / "typescript" / "tsc.js").write_text("// dev only\n", encoding="utf-8")
    (modules / "@types").mkdir()
    (modules / "@types" / "node.d.ts").write_text("// dev only\n", encoding="utf-8")
    return dist


def test_a_privately_signed_broker_ships_its_ca_and_points_node_at_it(tmp_path):
    """The lab pins the broker listener to a public address with a private
    signer, so the facade's outbound TLS needs the trust anchor — exactly what
    the DSH control tree does with NODE_EXTRA_CA_CERTS."""
    dist = _built_dist(tmp_path)
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n", encoding="utf-8")
    env = _FakeExecEnvironment()
    asyncio.run(deploy_generic_facade(
        environment=env, facade_dist=dist, gateway_url="https://10.0.0.1:8447",
        token_file="/run/aeval/trial-token", control_ca=ca,
    ))
    with tarfile.open(fileobj=io.BytesIO(env.blobs["/tmp/aeval-facade.tar.gz"]), mode="r:gz") as tar:
        assert "ca.crt" in tar.getnames()
    start = next(cmd for cmd in env.commands if "facade_main.js" in cmd)
    assert "NODE_EXTRA_CA_CERTS=/opt/aeval-facade/ca.crt" in start


def test_a_loopback_broker_needs_no_extra_ca(tmp_path):
    dist = _built_dist(tmp_path)
    env = _FakeExecEnvironment()
    asyncio.run(deploy_generic_facade(
        environment=env, facade_dist=dist, gateway_url="http://127.0.0.1:5000",
        token_file="/run/aeval/trial-token",
    ))
    start = next(cmd for cmd in env.commands if "facade_main.js" in cmd)
    assert "NODE_EXTRA_CA_CERTS" not in start


def test_a_dist_without_its_closure_is_refused_before_any_upload(tmp_path):
    """A real target host surfaced this the hard way: a dist shipped without node_modules
    uploads fine, starts, and then dies inside the sandbox with
    ERR_MODULE_NOT_FOUND while the health gate times out. The missing closure
    is refused here, with the remedy, before the tar is built."""
    dist = _built_dist(tmp_path)
    shutil.rmtree(dist.parent / "node_modules")
    env = _FakeExecEnvironment()
    with pytest.raises(BootstrapError) as excinfo:
        asyncio.run(deploy_generic_facade(
            environment=env, facade_dist=dist, gateway_url="http://10.0.0.1:5000",
            token_file="/run/aeval/trial-token",
        ))
    assert "npm ci" in str(excinfo.value)
    assert env.uploads == []
    assert env.commands == []


def test_discovery_prefers_the_operator_override(tmp_path, monkeypatch):
    built = _built_dist(tmp_path)
    override = tmp_path / "elsewhere" / "dist"
    override.mkdir(parents=True)
    (override / "facade_main.js").write_text("export {};\n", encoding="utf-8")
    monkeypatch.setenv("AEVAL_FACADE_DIST", str(override))
    assert facade_dist_candidates(tmp_path)[0] == override
    assert resolve_facade_dist(tmp_path) == override

    monkeypatch.delenv("AEVAL_FACADE_DIST")
    assert resolve_facade_dist(tmp_path) == built


def test_discovery_fails_closed_when_nothing_is_built(tmp_path, monkeypatch):
    import aeval.control.bootstrap as module

    # Every ancestor of a temp dir eventually reaches the aeval checkout, so
    # the "nothing built" case is pinned by narrowing the candidate list.
    monkeypatch.setattr(module, "facade_dist_candidates", lambda start=None: [tmp_path / "nope"])
    with pytest.raises(BootstrapError, match="no built deepagent facade dist"):
        resolve_facade_dist(tmp_path)


def test_the_shipped_closure_holds_runtime_files_and_no_dev_trees(tmp_path):
    dist = _built_dist(tmp_path)
    shipped = {relative for _, relative in _facade_runtime_files(dist)}
    assert shipped == {
        "dist/facade_main.js",
        "dist/gateway_lease.js",
        "package.json",
        "node_modules/@deepseek-ai/dsh-llm/package.json",
    }


async def test_deploy_generic_facade_uploads_starts_and_health_gates(tmp_path):
    dist = _built_dist(tmp_path)
    env = _FakeExecEnvironment(healthy_after=2)

    url = await deploy_generic_facade(
        environment=env, facade_dist=dist,
        gateway_url="http://10.0.0.1:5000", token_file="/run/aeval/trial-token",
        port=8787, health_timeout_sec=5.0,
    )

    assert url == "http://127.0.0.1:8787"
    # one tarball, extracted into the self-contained tree
    assert [target for _, target in env.uploads] == ["/tmp/aeval-facade.tar.gz"]
    assert any(cmd.startswith(f"mkdir -p {FACADE_SANDBOX_ROOT.as_posix()}") for cmd in env.commands)
    extract = next(cmd for cmd in env.commands if cmd.startswith("tar -xzf"))
    assert f"-C {FACADE_SANDBOX_ROOT.as_posix()}" in extract
    # the uploaded tarball carries the built entry, not just the shims
    with tarfile.open(fileobj=io.BytesIO(env.blobs["/tmp/aeval-facade.tar.gz"]), mode="r:gz") as tar:
        assert "dist/facade_main.js" in tar.getnames()
        assert "node_modules/@deepseek-ai/dsh-llm/package.json" in tar.getnames()
    # the start contract: detached AND returning, env-pinned, logs captured.
    # ``setsid --fork`` is the load-bearing part: a trailing ``&`` leaves the
    # exec waiting on a shell that holds the pipes (hung on a real target host).
    start = next(cmd for cmd in env.commands if "facade_main.js" in cmd)
    assert "setsid --fork" in start
    # no trailing background job: the command must RETURN (only the && chain)
    assert " & " not in start and not start.rstrip().endswith("&")
    assert "AEVAL_GATEWAY_URL=http://10.0.0.1:5000" in start
    assert "AEVAL_TRIAL_TOKEN_FILE=/run/aeval/trial-token" in start
    assert "AEVAL_FACADE_PORT=8787" in start
    # the protocol surface defaults to chat only: an existing deployment's
    # facade behaves exactly as before until a routing declares more
    assert "AEVAL_FACADE_PROTOCOLS=chat_completions" in start
    assert ">/tmp/aeval-facade.log 2>&1 < /dev/null" in start
    # the gate waited for the second probe
    assert env.probes == 2


async def test_the_declared_wire_selects_the_served_protocols(tmp_path):
    dist = _built_dist(tmp_path)
    env = _FakeExecEnvironment()
    await deploy_generic_facade(
        environment=env, facade_dist=dist,
        gateway_url="http://10.0.0.1:5000", token_file="/run/aeval/trial-token",
        protocols=["responses"],
    )
    start = next(cmd for cmd in env.commands if "facade_main.js" in cmd)
    assert "AEVAL_FACADE_PROTOCOLS=responses" in start


async def test_an_unknown_facade_protocol_is_refused_before_any_upload(tmp_path):
    dist = _built_dist(tmp_path)
    env = _FakeExecEnvironment()
    with pytest.raises(BootstrapError, match="facade protocol"):
        await deploy_generic_facade(
            environment=env, facade_dist=dist,
            gateway_url="http://10.0.0.1:5000", token_file="/run/aeval/trial-token",
            protocols=["gopher"],
        )
    assert env.uploads == []


async def test_deploy_generic_facade_refuses_an_unhealthy_facade(tmp_path):
    dist = _built_dist(tmp_path)
    env = _FakeExecEnvironment(healthy_after=10_000, log="Error: AEVAL_LEASE_MISMATCH")
    # the operator gets the reason, not just a timeout
    with pytest.raises(BootstrapError, match="AEVAL_LEASE_MISMATCH"):
        await deploy_generic_facade(
            environment=env, facade_dist=dist,
            gateway_url="http://10.0.0.1:5000", health_timeout_sec=0.05,
        )
    assert any(cmd.startswith("cat ") for cmd in env.commands)


async def test_deploy_generic_facade_fails_closed_on_a_broken_tree(tmp_path):
    dist = _built_dist(tmp_path)
    with pytest.raises(BootstrapError, match="no built facade_main.js"):
        await deploy_generic_facade(
            environment=_FakeExecEnvironment(), facade_dist=tmp_path / "empty",
            gateway_url="http://10.0.0.1:5000",
        )
    env = _FakeExecEnvironment(extract_code=1)
    with pytest.raises(BootstrapError, match="could not be extracted"):
        await deploy_generic_facade(
            environment=env, facade_dist=dist, gateway_url="http://10.0.0.1:5000",
        )
    env = _FakeExecEnvironment(start_code=127)
    with pytest.raises(BootstrapError, match="could not be started"):
        await deploy_generic_facade(
            environment=env, facade_dist=dist, gateway_url="http://10.0.0.1:5000",
        )


async def test_deploy_generic_facade_needs_upload_and_exec(tmp_path):
    with pytest.raises(BootstrapError, match="no upload_file/exec"):
        await deploy_generic_facade(
            environment=SimpleNamespace(), facade_dist=_built_dist(tmp_path),
            gateway_url="http://10.0.0.1:5000",
        )


# ── the dispatch: which flavor runs, and what a declared-but-unknown stack does ──


class _FacadeAgent:
    CONTROL_STACK = "deepagent-facade"
    # Which wire the agent speaks: the deployment
    # derives the facade's served endpoints from exactly this declaration.
    MODEL_ROUTING = {
        "agent_protocol": "openai_responses",
        "env": {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY"},
    }


class _ChatFacadeAgent:
    CONTROL_STACK = "deepagent-facade"
    MODEL_ROUTING = {
        "agent_protocol": "openai_chat",
        "env": {"base_url": "ANTHROPIC_BASE_URL", "api_key": "ANTHROPIC_API_KEY"},
    }


class _UnroutedFacadeAgent:
    """Declares the facade stack but not which wire it speaks — refused."""

    CONTROL_STACK = "deepagent-facade"


class _NativeFacadeAgent:
    """gateway-native routing with a facade stack — serves nothing, refused."""

    CONTROL_STACK = "deepagent-facade"
    MODEL_ROUTING = {"agent_protocol": "gateway_native"}


class _DshAgent:
    CONTROL_STACK = "dsh"


class _MysteryAgent:
    CONTROL_STACK = "team-framework-v9"


class _NoStackAgent:
    """An agent that needs none of this."""


class _UploadOnlyEnvironment:
    async def upload_file(self, source: str, target: str):
        return None


class _BrokerStub:
    url = "http://127.0.0.1:4321"
    token_path: Path

    def __init__(self, tmp_path: Path):
        self.token_path = tmp_path / "token"
        self.token_path.write_text("job-token", encoding="utf-8")


def _paths() -> TrialPaths:
    return TrialPaths(
        sandbox_cwd="/workspace", agent_home="/logs/agent/dsh-home",
        bundle_path="/logs/agent/bundle_descriptor.json",
        session_root="dsh-home", download_root="trials/t/agent",
    )


def _config() -> dict:
    return {
        "sessionId": "sess-1", "gatewayUrl": "http://127.0.0.1:4321",
        "jobTokenFile": "/run/aeval/trial-token", "provider": "p", "model": "m",
    }


def _context(demo_suite, runtime_lock, tmp_path, *, facade_lock=None) -> EvaluationContext:
    from aeval.hooks.context import TrialState

    ctx = EvaluationContext(
        run_id="r", runtime_lock=runtime_lock, suite=demo_suite,
        run_dir=tmp_path, store_path=tmp_path / "s.db",
    )
    ctx.run_binding = RunBinding(
        run_id="r", job_config_hash="a" * 64, config_file_sha256="b" * 64,
        runtime_lock_digest="c" * 64,
    )
    # start_trial needs a full Harbor event; plant the owner state directly.
    state = TrialState(trial_id="t", phase="running")
    state.trial_dir = tmp_path / "trials" / "t"
    state.trial_dir.mkdir(parents=True, exist_ok=True)
    ctx.trials["t"] = state
    if facade_lock is not None:
        ctx.runtime_lock = runtime_lock.model_copy(update={"facade_dist": facade_lock})
    return ctx


async def test_the_facade_flavor_deploys_the_generic_tree(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    import aeval.control.bootstrap as module

    dist = _built_dist(tmp_path)
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return "http://127.0.0.1:8787"

    monkeypatch.setattr(module, "deploy_generic_facade", _record)

    env = _FakeExecEnvironment()
    await bootstrap_trial_control(
        environment=env, context=_context(demo_suite, runtime_lock, tmp_path), trial_id="t",
        paths=_paths(), broker=_BrokerStub(tmp_path),
        provider="p", model="m", agent=_FacadeAgent(), facade_dist=dist,
    )
    assert len(calls) == 1
    assert calls[0]["facade_dist"] == dist
    assert calls[0]["gateway_url"] == "http://127.0.0.1:4321"
    assert calls[0]["token_file"] == "/run/aeval/trial-token"
    # the served endpoints follow the declared wire, never a default
    assert calls[0]["protocols"] == ["responses"]
    # the token still reaches the sandbox through the agent-neutral path —
    # and no facade tarball is uploaded by the recorded fake
    assert [target for _, target in env.uploads] == ["/run/aeval/trial-token"]


async def test_the_chat_wire_routes_the_facade_to_the_chat_endpoint(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    import aeval.control.bootstrap as module

    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return "http://127.0.0.1:8787"

    monkeypatch.setattr(module, "deploy_generic_facade", _record)
    await _deploy_declared_stack(
        environment=_FakeExecEnvironment(),
        context=_context(demo_suite, runtime_lock, tmp_path),
        agent=_ChatFacadeAgent(), paths=_paths(), config=_config(),
        control_dist=None, control_ca=None, facade_dist=_built_dist(tmp_path), trial_id="t",
    )
    assert calls[0]["protocols"] == ["chat_completions"]


async def test_a_facade_stack_without_a_wire_declaration_is_refused(
    demo_suite, runtime_lock, tmp_path
):
    """The facade would serve endpoints the agent never calls — deploy nothing."""
    for agent in (_UnroutedFacadeAgent, _NativeFacadeAgent):
        with pytest.raises(BootstrapError, match="model routing"):
            await _deploy_declared_stack(
                environment=_FakeExecEnvironment(),
                context=_context(demo_suite, runtime_lock, tmp_path),
                agent=agent(), paths=_paths(), config=_config(),
                control_dist=None, control_ca=None, facade_dist=None, trial_id="t",
            )


async def test_a_declared_but_unknown_stack_is_refused(
    demo_suite, runtime_lock, tmp_path
):
    """Fail-closed twice over: the config cannot be shaped for an unregistered
    stack (compose time) and the stack cannot be deployed (deploy time) — the
    earlier refusal fires first, naming the stack."""
    with pytest.raises(BootstrapError, match="team-framework-v9"):
        await bootstrap_trial_control(
            environment=_FakeExecEnvironment(),
            context=_context(demo_suite, runtime_lock, tmp_path), trial_id="t",
            paths=_paths(), broker=_BrokerStub(tmp_path),
            provider="p", model="m", agent=_MysteryAgent(),
        )


async def test_the_dsh_flavor_still_needs_its_control_dist(
    demo_suite, runtime_lock, tmp_path
):
    class _DshWithPrefix(_DshAgent):
        # the flavor's interface requirements, satisfied so the refusal under
        # test is the missing controlDist, not a missing hook
        @staticmethod
        def cli_bin_dir():
            return "/opt/dsh"

        def add_patch_file(self, path):
            self.patch = path

    with pytest.raises(BootstrapError, match="no controlDist"):
        await _deploy_declared_stack(
            environment=_FakeExecEnvironment(),
            context=_context(demo_suite, runtime_lock, tmp_path),
            agent=_DshWithPrefix(), paths=_paths(), config=_config(),
            control_dist=None, control_ca=None, facade_dist=None, trial_id="t",
        )


async def test_an_undeclared_stack_deploys_nothing(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    import aeval.control.bootstrap as module

    monkeypatch.setattr(module, "deploy_generic_facade", lambda **k: pytest.fail("deployed anyway"))
    await _deploy_declared_stack(
        environment=_FakeExecEnvironment(),
        context=_context(demo_suite, runtime_lock, tmp_path),
        agent=_NoStackAgent(), paths=_paths(), config=_config(),
        control_dist=None, control_ca=None, facade_dist=None, trial_id="t",
    )


async def test_bytes_changed_after_the_lock_are_refused(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    import aeval.control.bootstrap as module

    dist = _built_dist(tmp_path)
    stale = ControlDistLock(files=["facade_main.js"], sha256="f" * 64)
    monkeypatch.setattr(module, "deploy_generic_facade", lambda **k: pytest.fail("uploaded anyway"))
    with pytest.raises(BootstrapError, match="changed after the run lock was taken"):
        await _deploy_declared_stack(
            environment=_FakeExecEnvironment(),
            context=_context(demo_suite, runtime_lock, tmp_path, facade_lock=stale),
            agent=_FacadeAgent(), paths=_paths(), config=_config(),
            control_dist=None, control_ca=None, facade_dist=dist, trial_id="t",
        )


async def test_matching_lock_bytes_are_deployed(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    import aeval.control.bootstrap as module
    from aeval.provenance import fingerprint_control_dist

    dist = _built_dist(tmp_path)
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(module, "deploy_generic_facade", _record)
    await _deploy_declared_stack(
        environment=_FakeExecEnvironment(),
        context=_context(
            demo_suite, runtime_lock, tmp_path,
            facade_lock=fingerprint_control_dist(dist),
        ),
        agent=_FacadeAgent(), paths=_paths(), config=_config(),
        control_dist=None, control_ca=None, facade_dist=dist, trial_id="t",
    )
    assert len(calls) == 1
