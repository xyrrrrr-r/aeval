"""Plugin broker lifecycle tests (P0-4/5): opt-in controlled routing.

Positive paths run the REAL compiled TS broker bin. The pinned-port
requirement, the config-first digest chain, and teardown on every
terminal path are asserted against the real process; failure shapes
are pinned with fakes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harbor.trial.hooks import TrialEvent

from tests.integration.test_plugin_lifecycle import emit, event_for, owned_job

from aeval.hooks.broker_lifecycle import (
    BROKER_SPEC_ENV,
    BrokerSpecError,
    parse_broker_spec,
    start_trial_broker,
    stop_trial_broker,
    trial_control_paths,
)
from aeval.hooks.context import TrialState
from aeval.hooks.plugin import AevalPlugin, HookRegistrationError
from aeval.contracts import control_config_digest

CONTROL_ROOT = Path(__file__).parents[3] / "dsh-eval-control"
KEY_ENV = "AEVAL_BROKER_LIFECYCLE_KEY"
KEY = "offline-test-key-0123456789abcdef"


def _broker_js() -> Path:
    candidate = CONTROL_ROOT / "dist" / "broker_main.js"
    if not candidate.is_file():
        pytest.skip("dsh-eval-control dist not built — run npm run build there first")
    return candidate


def _spec_json(tmp_path: Path, *, port=0, **overrides) -> Path:
    data = {
        "brokerJs": str(_broker_js()),
        "listenPort": port,
        "identity": {"provider": "offline-openai", "model": "test-model"},
        "limits": {"maxSteps": 5},
        "maxOutputTokens": 64,
        "upstream": {
            "provider": "offline-openai",
            "baseUrl": "http://127.0.0.1:9",
            "apiKeyEnv": KEY_ENV,
            "model": "test-model",
        },
    }
    data.update(overrides)
    path = tmp_path / "broker-spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _state(tmp_path: Path, trial_id="t-1") -> TrialState:
    state = TrialState(trial_id=trial_id, phase="running")
    state.trial_dir = tmp_path / "trials" / trial_id
    state.trial_dir.mkdir(parents=True, exist_ok=True)
    return state


async def _context(demo_suite, runtime_lock, tmp_path):
    from aeval.contracts import RunBinding
    from aeval.hooks.context import EvaluationContext

    ctx = EvaluationContext(
        run_id="r", runtime_lock=runtime_lock, suite=demo_suite,
        run_dir=tmp_path, store_path=tmp_path / "s.db",
    )
    ctx.run_binding = RunBinding(
        run_id="r", job_config_hash="b" * 64,
        config_file_sha256="c" * 64, runtime_lock_digest=runtime_lock.digest(),
    )
    return ctx


# --- spec parsing -----------------------------------------------------


def test_parse_spec_none_when_unset(monkeypatch):
    monkeypatch.delenv(BROKER_SPEC_ENV, raising=False)
    assert parse_broker_spec() is None


def test_parse_spec_happy_path(tmp_path, monkeypatch):
    spec_path = _spec_json(tmp_path, port=4711)
    monkeypatch.setenv(BROKER_SPEC_ENV, str(spec_path))
    monkeypatch.setenv(KEY_ENV, KEY)
    spec = parse_broker_spec()
    assert spec.listen_port == 4711
    assert spec.gateway_url == "http://127.0.0.1:4711"
    assert spec.upstream["apiKeyEnv"] == KEY_ENV
    assert spec.broker_js.name == "broker_main.js"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("listenPort"),                       # port must be pinned
        lambda d: d.update(listenPort=0),                    # invalid port
        lambda d: d.update(listenPort=True),                 # bool is not a port
        lambda d: d.pop("brokerJs"),                         # no bin
        lambda d: d.pop("maxOutputTokens"),                  # no budget
        lambda d: d.update(maxOutputTokens=0),               # bad budget
        lambda d: d["upstream"].pop("apiKeyEnv"),            # no credential name
        lambda d: d.update(upstream=[]),                     # not an object
        lambda d: d.update(unknownKey=1),                    # unknown key
        lambda d: d.update(identity="x"),                    # identity not object
        lambda d: d.update(auxiliaryPolicy="always"),        # policy not object
        lambda d: d.update(auxiliaryPolicy={"research": "allow"}),   # unknown purpose
        lambda d: d.update(auxiliaryPolicy={"compaction": "sometimes"}),  # bad decision
    ],
)
def test_parse_spec_rejects_broken_specs(tmp_path, monkeypatch, mutate):
    spec_path = _spec_json(tmp_path, port=4711)
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    mutate(data)
    spec_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(BROKER_SPEC_ENV, str(spec_path))
    with pytest.raises(BrokerSpecError):
        parse_broker_spec()


def test_parse_spec_missing_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(BROKER_SPEC_ENV, str(tmp_path / "nope.json"))
    with pytest.raises(BrokerSpecError, match="is not a file"):
        parse_broker_spec()


def test_parse_spec_resolves_control_root(tmp_path, monkeypatch):
    data = json.loads(_spec_json(tmp_path, port=4712).read_text(encoding="utf-8"))
    del data["brokerJs"]
    data["controlRoot"] = str(CONTROL_ROOT)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(BROKER_SPEC_ENV, str(path))
    spec = parse_broker_spec()
    assert spec.broker_js.name == "broker_main.js"


# --- lifecycle against the real bin ----------------------------------


async def test_start_composes_config_first_and_pins_digest(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    """The control config digest must exist BEFORE the broker starts and
    be the same value the broker lease carries (config-first chain)."""
    monkeypatch.setenv(KEY_ENV, KEY)
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    state = _state(tmp_path)
    # pin a free port so the gateway URL (and thus the control config
    # digest) is known before the broker process exists
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    spec_path = _spec_json(
        tmp_path, port=port, auxiliaryPolicy={"compaction": "allow"}
    )
    spec = parse_broker_spec(str(spec_path))
    assert spec.auxiliary_policy == {"compaction": "allow"}

    broker, config = start_trial_broker(spec, ctx, state)
    try:
        assert state.broker is broker
        assert state.control_config is config
        assert config["gatewayUrl"] == broker.url
        # D47: the served auxiliary policy reaches BOTH configs the sandbox
        # compares — the control config and the broker lease config.
        assert config["auxiliaryPolicy"] == {"compaction": "allow"}
        # broker config carries the control config digest, verbatim
        broker_cfg = json.loads(
            (tmp_path / "brokers" / state.trial_id / "broker.json").read_text("utf-8")
        )
        assert broker_cfg["configDigest"] == control_config_digest(config)
        assert broker_cfg["listen"]["port"] == port
        assert broker_cfg["auxiliaryPolicy"] == {"compaction": "allow"}
        assert broker.token_path.is_file()
    finally:
        stop_trial_broker(state)
    assert state.broker is None


async def test_stop_records_unclean_exit_as_infra_issue(
    demo_suite, runtime_lock, tmp_path, monkeypatch
):
    monkeypatch.setenv(KEY_ENV, KEY)
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    state = _state(tmp_path)
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    spec = parse_broker_spec(str(_spec_json(tmp_path, port=port)))

    broker, _ = start_trial_broker(spec, ctx, state)
    # simulate an unclean shutdown: kill without the graceful path
    import os
    import signal

    os.kill(broker.process.pid, signal.SIGKILL)
    broker.process.wait()
    stop_trial_broker(state)
    assert "model broker exited with code" in " ".join(state.infra_invalid_reasons)


async def test_url_mismatch_breaks_the_chain(demo_suite, runtime_lock, tmp_path, monkeypatch):
    """A broker announcing a different URL than the pinned control config
    must be torn down and the start refused."""
    monkeypatch.setenv(KEY_ENV, KEY)
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    state = _state(tmp_path)

    class LyingBroker:
        url = "http://127.0.0.1:9999"
        token_path = tmp_path / "nope"

        def start(self):
            return self

        def stop(self, reason=None):
            self.stopped = reason
            return 0

    # start with the real spec but intercept the process to lie about its URL
    spec = parse_broker_spec(str(_spec_json(tmp_path, port=1)))
    from aeval.hooks import broker_lifecycle as lifecycle

    real_cls = lifecycle.ModelBrokerProcess
    monkeypatch.setattr(
        lifecycle, "ModelBrokerProcess",
        lambda **kwargs: LyingBroker(),
    )
    with pytest.raises(BrokerSpecError, match="identity chain broken"):
        start_trial_broker(spec, ctx, state)
    monkeypatch.setattr(lifecycle, "ModelBrokerProcess", real_cls)
    assert state.broker is None


async def test_start_requires_trusted_run_binding(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    ctx.run_binding = None
    state = _state(tmp_path)
    spec = parse_broker_spec(str(_spec_json(tmp_path, port=4713)))
    with pytest.raises(BrokerSpecError, match="no trusted binding"):
        start_trial_broker(spec, ctx, state)


async def test_start_without_trial_dir_fails(demo_suite, runtime_lock, tmp_path):
    ctx = await _context(demo_suite, runtime_lock, tmp_path)
    state = TrialState(trial_id="t", phase="running")
    state.trial_dir = None
    spec = parse_broker_spec(str(_spec_json(tmp_path, port=4714)))
    with pytest.raises(BrokerSpecError, match="no directory"):
        start_trial_broker(spec, ctx, state)


def test_trial_control_paths_are_conventions(tmp_path):
    state = _state(tmp_path)
    paths = trial_control_paths(state, tmp_path)
    assert paths.sandbox_cwd == "/workspace"
    assert paths.dsh_home == "/logs/agent/dsh-home"
    assert paths.bundle_path == "/logs/agent/bundle_descriptor.json"
    assert paths.session_root == "dsh-home"
    assert paths.download_root == f"trials/{state.trial_id}/agent"


# --- plugin integration (owned_job fixture) ---------------------------


@pytest.fixture
async def broker_owned_job(owned_job, tmp_path, monkeypatch):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv(BROKER_SPEC_ENV, str(_spec_json(tmp_path, port=port)))
    monkeypatch.setenv(KEY_ENV, KEY)
    return owned_job, port


async def test_plugin_starts_and_stops_broker_per_trial(broker_owned_job):
    """START spawns the broker; the terminal event stops it and clears
    the token (cleanup by the bin)."""
    job, port = broker_owned_job
    plugin = AevalPlugin()
    await plugin.on_job_start(job)
    context = plugin._context
    assert context.broker_spec is not None
    assert context.broker_spec.listen_port == port

    start = event_for(job)
    await emit(job, start, TrialEvent.START)
    state = context.trials[str(start.trial_id)]
    assert state.broker is not None, "broker must be up before the model phase"
    assert state.broker.url == f"http://127.0.0.1:{port}"
    assert state.control_config["gatewayUrl"] == state.broker.url
    token = state.broker.token_path
    assert token.is_file()

    await emit(job, start, TrialEvent.END)
    assert state.broker is None
    assert not token.exists(), "token must be cleaned up on normal stop"
    assert not state.infra_invalid_reasons


async def test_plugin_broker_startup_failure_taints_the_trial(
    owned_job, tmp_path, monkeypatch
):
    """A broker that cannot start marks the trial infra_invalid — the
    model phase must not run uncontrolled (fail-closed).

    The failure is induced by an OCCUPIED port, not a privileged one:
    running as root on Linux binds port 1 successfully (found during the
    aarch64 environment verification), so privilege-based failure is not
    portable. An occupied loopback port fails to bind on every platform
    and for every user.
    """
    import socket

    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    try:
        monkeypatch.setenv(BROKER_SPEC_ENV, str(_spec_json(tmp_path, port=port)))
        monkeypatch.setenv(KEY_ENV, KEY)
        job = owned_job
        plugin = AevalPlugin()
        await plugin.on_job_start(job)
        context = plugin._context

        start = event_for(job)
        await emit(job, start, TrialEvent.START)
        state = context.trials[str(start.trial_id)]
        assert state.broker is None
        assert any(
            "model broker startup failed" in r for r in state.infra_invalid_reasons
        ), state.infra_invalid_reasons
    finally:
        occupied.close()


async def test_plugin_without_spec_runs_brokerless(owned_job, monkeypatch):
    monkeypatch.delenv(BROKER_SPEC_ENV, raising=False)
    job = owned_job
    plugin = AevalPlugin()
    await plugin.on_job_start(job)
    context = plugin._context
    assert context.broker_spec is None
    start = event_for(job)
    await emit(job, start, TrialEvent.START)
    state = context.trials[str(start.trial_id)]
    assert state.broker is None
    await emit(job, start, TrialEvent.END)
    assert not state.infra_invalid_reasons


async def test_plugin_broken_spec_fails_registration(owned_job, tmp_path, monkeypatch):
    """A configured-but-broken spec refuses to register the plugin at all."""
    bad = tmp_path / "bad-spec.json"
    bad.write_text(json.dumps({"listenPort": 0}), encoding="utf-8")
    monkeypatch.setenv(BROKER_SPEC_ENV, str(bad))
    job = owned_job
    plugin = AevalPlugin()
    with pytest.raises((HookRegistrationError, BrokerSpecError)):
        await plugin.on_job_start(job)


async def test_plugin_stops_broker_on_cancellation(broker_owned_job):
    job, port = broker_owned_job
    plugin = AevalPlugin()
    await plugin.on_job_start(job)
    context = plugin._context
    start = event_for(job)
    await emit(job, start, TrialEvent.START)
    state = context.trials[str(start.trial_id)]
    assert state.broker is not None
    await emit(job, start, TrialEvent.CANCEL)
    assert state.broker is None


def test_broker_that_died_is_reported_with_its_stderr(tmp_path):
    """A broker that exits on its own closes the lease, and every later
    model call fails with AEVAL_LEASE_CLOSED — the trial log must carry
    the reason rather than leaving it unexplained (real-chain finding)."""
    from aeval.hooks.broker_lifecycle import note_broker_unexpected_exit

    class _Process:
        def __init__(self, code):
            self._code = code

        def poll(self):
            return self._code

    class _Broker:
        def __init__(self, code, tail=""):
            self.process = _Process(code)
            self._stderr_tail = tail

    class _State:
        def __init__(self, broker):
            self.broker = broker

    assert note_broker_unexpected_exit(_State(None)) is None
    assert note_broker_unexpected_exit(_State(_Broker(None))) is None
    message = note_broker_unexpected_exit(
        _State(_Broker(1, "Error: listen EADDRINUSE: address already in use"))
    )
    assert message is not None
    assert "code 1" in message
    assert "EADDRINUSE" in message
    silent = note_broker_unexpected_exit(_State(_Broker(0)))
    assert silent is not None and "no stderr captured" in silent


def test_stop_trial_broker_keeps_the_brokers_own_stop_attribution():
    """A lease that closes for an unexplained reason must be attributable.

    The broker writes its lease-stop lines to stderr; the trial record is
    the only place they survive (real-chain finding: without them a closed
    lease could not be blamed on any path)."""
    from aeval.hooks.broker_lifecycle import stop_trial_broker
    from aeval.hooks.context import TrialState

    class _Broker:
        def __init__(self, code, tail):
            self._code = code
            self._stderr_tail = tail

        def stop(self, reason):
            return self._code

    state = TrialState(trial_id="t1")
    state.broker = _Broker(
        0, "[aeval-broker] lease stop reason=infra_error cause=client_abort_in_flight"
    )
    stop_trial_broker(state)
    assert state.broker is None
    assert len(state.broker_diagnostics) == 2
    assert "client_abort_in_flight" in state.broker_diagnostics[0]
    assert state.broker_diagnostics[1].startswith("owner stopped broker at=")
    # a silent broker adds no diagnostics and no infra reason
    quiet = TrialState(trial_id="t2")
    quiet.broker = _Broker(0, "")
    stop_trial_broker(quiet)
    assert quiet.broker_diagnostics == []
    assert quiet.infra_invalid_reasons == []


def test_audit_records_broker_diagnostics(tmp_path):
    """The trial's own audit file carries the broker diagnostics."""
    import asyncio
    import json

    from aeval.hooks.context import TrialState
    from aeval.hooks.evidence import finalize_trial_record

    state = TrialState(trial_id="t1", trial_dir=tmp_path)
    state.phase = "ended"
    state.broker_diagnostics = ["[aeval-broker] lease stop reason=infra_error cause=dispatch_incomplete"]

    class _Event:
        trial_id = "t1"

    class _Context:
        def trial_state(self, trial_id):
            assert trial_id == "t1"
            return state

    asyncio.run(finalize_trial_record(_Event(), _Context()))
    summary = json.loads((tmp_path / "aeval_audit.json").read_text(encoding="utf-8"))
    assert summary["broker_diagnostics"] == state.broker_diagnostics
    assert summary["trial_id"] == "t1"
