"""Broker lifecycle tests (Python owner side).

The positive path runs the REAL compiled TS broker bin
(``dsh-eval-control/dist/broker_main.js``) against a dead loopback
upstream URL — startup never contacts the upstream, so the readiness
protocol is exercised end-to-end offline. Negative cases pin the exact
failure shapes.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from aeval.control.broker import (
    BrokerConfigError,
    BrokerStartupError,
    BROKER_PROTOCOL,
    ModelBrokerProcess,
    broker_bin_candidates,
    find_node,
    write_broker_config,
)

REPO = Path(__file__).parents[3]
KEY_ENV = "AEVAL_BROKER_TEST_KEY"
KEY = "offline-test-key-0123456789abcdef"
RUN = {
    "run_id": "run-py",
    "job_config_hash": "b" * 64,
    "config_file_sha256": "c" * 64,
    "runtime_lock_digest": "d" * 64,
}


def _broker_js() -> Path:
    for candidate in broker_bin_candidates(REPO.parent / "dsh-eval-control"):
        if candidate.is_file():
            return candidate
    pytest.skip("dsh-eval-control dist not built — run npm run build there first")


def _config(tmp_path: Path, **overrides) -> Path:
    data = {
        "run": RUN,
        "trialId": "trial-py",
        "sessionId": "session-py",
        "configDigest": "a" * 64,
        "identity": {"provider": "offline-openai", "model": "test-model"},
        "limits": {"maxSteps": 5},
        "maxOutputTokens": 64,
        "listen": {"host": "127.0.0.1"},
        "tokenOut": str(tmp_path / "token"),
        "upstream": {
            "provider": "offline-openai",
            # a dead loopback port: startup must not contact the upstream
            "baseUrl": "http://127.0.0.1:9",
            "apiKeyEnv": KEY_ENV,
            "model": "test-model",
        },
    }
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_write_broker_config_round_trips(tmp_path):
    path = write_broker_config(
        tmp_path / "cfg" / "broker.json",
        run=RUN,
        trial_id="trial-1",
        session_id="session-1",
        config_digest="a" * 64,
        identity={"provider": "p", "model": "m"},
        limits={"maxSteps": 5},
        max_output_tokens=64,
        listen_host="127.0.0.1",
        token_out=tmp_path / "token",
        upstream={"provider": "p", "baseUrl": "https://x", "apiKeyEnv": "K", "model": "m"},
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {
        "run", "trialId", "sessionId", "configDigest", "identity",
        "limits", "maxOutputTokens", "listen", "tokenOut", "upstream",
    }
    assert data["upstream"]["apiKeyEnv"] == "K"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_write_broker_config_carries_the_upstream_protocol(tmp_path):
    """The responses mode reaches the bin's config verbatim; junk is refused.

    Absent protocol stays absent — a spec written before the key existed must
    produce byte-identical config (sealed evidence keeps recomputing).
    """
    path = write_broker_config(
        tmp_path / "broker-responses.json",
        run=RUN,
        trial_id="trial-1",
        session_id="session-1",
        config_digest="a" * 64,
        identity={"provider": "p", "model": "m"},
        limits={"maxSteps": 5},
        max_output_tokens=64,
        listen_host="127.0.0.1",
        token_out=tmp_path / "token",
        upstream={
            "provider": "p",
            "baseUrl": "https://api.deepseek.com",
            "apiKeyEnv": "K",
            "model": "m",
            "protocol": "responses",
        },
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["upstream"]["protocol"] == "responses"
    with pytest.raises(BrokerConfigError):
        write_broker_config(
            tmp_path / "broker-junk.json",
            run=RUN,
            trial_id="trial-1",
            session_id="session-1",
            config_digest="a" * 64,
            identity={"provider": "p", "model": "m"},
            limits={"maxSteps": 5},
            max_output_tokens=64,
            listen_host="127.0.0.1",
            token_out=tmp_path / "token",
            upstream={"provider": "p", "baseUrl": "https://x", "apiKeyEnv": "K", "model": "m", "protocol": "gopher"},
        )


def test_write_broker_config_carries_the_context_window(tmp_path):
    """The declared context capacity reaches the bin's config verbatim.

    Absent window stays absent — a spec written before the key existed must
    produce byte-identical config (sealed evidence keeps recomputing). A
    non-positive/non-integer value is refused here, not at broker startup
    inside a running trial.
    """
    path = write_broker_config(
        tmp_path / "broker-window.json",
        run=RUN,
        trial_id="trial-1",
        session_id="session-1",
        config_digest="a" * 64,
        identity={"provider": "p", "model": "m"},
        limits={"maxSteps": 5},
        max_output_tokens=64,
        listen_host="127.0.0.1",
        token_out=tmp_path / "token",
        upstream={
            "provider": "p",
            "baseUrl": "https://api.deepseek.com",
            "apiKeyEnv": "K",
            "model": "m",
            "contextWindow": 65536,
        },
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["upstream"]["contextWindow"] == 65536

    plain = write_broker_config(
        tmp_path / "broker-nowindow.json",
        run=RUN,
        trial_id="trial-1",
        session_id="session-1",
        config_digest="a" * 64,
        identity={"provider": "p", "model": "m"},
        limits={"maxSteps": 5},
        max_output_tokens=64,
        listen_host="127.0.0.1",
        token_out=tmp_path / "token",
        upstream={"provider": "p", "baseUrl": "https://x", "apiKeyEnv": "K", "model": "m"},
    )
    assert "contextWindow" not in json.loads(plain.read_text(encoding="utf-8"))["upstream"]

    for bad in (0, -1, 2.5, "65536", True):
        with pytest.raises(BrokerConfigError):
            write_broker_config(
                tmp_path / "broker-badwindow.json",
                run=RUN,
                trial_id="trial-1",
                session_id="session-1",
                config_digest="a" * 64,
                identity={"provider": "p", "model": "m"},
                limits={"maxSteps": 5},
                max_output_tokens=64,
                listen_host="127.0.0.1",
                token_out=tmp_path / "token",
                upstream={
                    "provider": "p",
                    "baseUrl": "https://x",
                    "apiKeyEnv": "K",
                    "model": "m",
                    "contextWindow": bad,
                },
            )


@pytest.mark.parametrize(
    "kwargs",
    [
        # missing run fields
        {"run": {"run_id": "r"}},
        # invalid identifiers
        {"trial_id": "not an id!"},
        {"identity": {"provider": "p", "model": "../etc"}},
        # non-positive ints
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        # missing upstream requirements
        {"upstream": {"provider": "p", "baseUrl": "", "apiKeyEnv": "K", "model": "m"}},
        {"upstream": {"provider": "p", "baseUrl": "https://x", "apiKeyEnv": " ", "model": "m"}},
    ],
)
def test_write_broker_config_rejects_invalid(tmp_path, kwargs):
    base = dict(
        run=RUN,
        trial_id="trial-1",
        session_id="session-1",
        config_digest="a" * 64,
        identity={"provider": "p", "model": "m"},
        limits={"maxSteps": 5},
        max_output_tokens=64,
        listen_host="127.0.0.1",
        token_out=tmp_path / "token",
        upstream={"provider": "p", "baseUrl": "https://x", "apiKeyEnv": "K", "model": "m"},
    )
    base.update(kwargs)
    with pytest.raises(BrokerConfigError):
        write_broker_config(tmp_path / "broker.json", **base)


def test_broker_bin_candidates_order():
    root = Path("/ctrl")
    candidates = broker_bin_candidates(root)
    assert candidates[0] == root / "dist" / "broker_main.js"


async def test_real_broker_starts_announces_and_stops_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    config = _config(tmp_path)
    broker = ModelBrokerProcess(
        node_bin=find_node(),
        broker_js=_broker_js(),
        config_path=config,
    )
    broker.start()
    try:
        assert broker.url is not None and broker.url.startswith("http://127.0.0.1:")
        assert broker.token_path is not None
        assert broker.token_path == Path(str(tmp_path / "token"))
        token = broker.token_path.read_text(encoding="utf-8").strip()
        assert token, "broker must have written the job token"
    finally:
        code = broker.stop("test_done")
    assert code == 0, "SIGTERM shutdown is a normal close"
    # the bin performs its own token cleanup on normal shutdown
    assert not broker.token_path.exists()


async def test_real_broker_without_credentials_exits_before_readiness(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    config = _config(tmp_path)
    broker = ModelBrokerProcess(
        node_bin=find_node(),
        broker_js=_broker_js(),
        config_path=config,
        ready_timeout_sec=15.0,
    )
    with pytest.raises(BrokerStartupError, match="exited before readiness.*code 2"):
        broker.start()
    assert broker.process is None or broker.process.poll() is not None


async def test_real_broker_hard_budget_without_metering_exits_3(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    config = _config(
        tmp_path,
        limits={"maxSteps": 5, "maxTokens": 4096},  # hard budget, no tokenCount
    )
    broker = ModelBrokerProcess(
        node_bin=find_node(),
        broker_js=_broker_js(),
        config_path=config,
        ready_timeout_sec=15.0,
    )
    with pytest.raises(BrokerStartupError, match="code 3"):
        broker.start()


def _fake_broker_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-broker.mjs"
    script.write_text(body, encoding="utf-8")
    return script


async def test_wrong_protocol_marker_is_rejected(tmp_path):
    script = _fake_broker_script(
        tmp_path,
        'process.stdout.write(JSON.stringify({ready: true, url: "http://x", '
        'tokenPath: "/t", protocol: "something-else/1"}) + "\\n");\n'
        "process.stdin.resume();\n",
    )
    broker = ModelBrokerProcess(node_bin=find_node(), broker_js=script,
                                config_path=_config(tmp_path))
    with pytest.raises(BrokerStartupError, match="protocol mismatch"):
        broker.start()


async def test_non_json_readiness_line_is_rejected(tmp_path):
    script = _fake_broker_script(tmp_path, 'process.stdout.write("hello\\n");\nprocess.stdin.resume();\n')
    broker = ModelBrokerProcess(node_bin=find_node(), broker_js=script,
                                config_path=_config(tmp_path))
    with pytest.raises(BrokerStartupError, match="not JSON"):
        broker.start()


async def test_not_ready_payload_is_rejected(tmp_path):
    script = _fake_broker_script(
        tmp_path,
        'process.stdout.write(JSON.stringify({ready: false}) + "\\n");\nprocess.stdin.resume();\n',
    )
    broker = ModelBrokerProcess(node_bin=find_node(), broker_js=script,
                                config_path=_config(tmp_path))
    with pytest.raises(BrokerStartupError, match="not ready"):
        broker.start()


async def test_readiness_timeout_kills_the_process(tmp_path):
    # a held-open event loop: alive, but never announcing readiness
    script = _fake_broker_script(tmp_path, "setInterval(() => {}, 1000);\n")
    broker = ModelBrokerProcess(
        node_bin=find_node(), broker_js=script,
        config_path=_config(tmp_path), ready_timeout_sec=0.5,
    )
    with pytest.raises(BrokerStartupError, match="did not announce readiness"):
        broker.start()
    assert broker.process is None or broker.process.poll() is not None


async def test_missing_bin_is_a_startup_error(tmp_path):
    broker = ModelBrokerProcess(
        node_bin=find_node(),
        broker_js=tmp_path / "does-not-exist.js",
        config_path=_config(tmp_path),
    )
    with pytest.raises(BrokerStartupError, match="broker bin not found"):
        broker.start()


async def test_double_start_and_stop_are_safe(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    broker = ModelBrokerProcess(node_bin=find_node(), broker_js=_broker_js(),
                                config_path=_config(tmp_path))
    broker.start()
    with pytest.raises(BrokerStartupError, match="already started"):
        broker.start()
    assert broker.stop("first") == 0
    assert broker.stop("second") == 0  # idempotent


async def test_sigkill_escalation_on_stubborn_process(tmp_path):
    script = _fake_broker_script(
        tmp_path,
        'process.stdout.write(JSON.stringify({ready: true, url: "http://x", '
        'tokenPath: "/t", protocol: process.env.AEVAL_TEST_PROTOCOL}) + "\\n");\n'
        "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);\n",
    )
    broker = ModelBrokerProcess(
        node_bin=find_node(), broker_js=script,
        config_path=_config(tmp_path),
        ready_timeout_sec=5.0, stop_timeout_sec=0.4,
    )
    import os as _os
    old = _os.environ.get("AEVAL_TEST_PROTOCOL")
    _os.environ["AEVAL_TEST_PROTOCOL"] = BROKER_PROTOCOL
    try:
        broker.start()
        code = broker.stop("escalate")
    finally:
        if old is None:
            _os.environ.pop("AEVAL_TEST_PROTOCOL", None)
        else:
            _os.environ["AEVAL_TEST_PROTOCOL"] = old
    assert code != 0 and code is not None, "SIGKILLed process reports its signal exit"


def test_find_node_rejects_missing_runtime(monkeypatch):
    import aeval.control.broker as broker_mod

    monkeypatch.setattr(broker_mod.shutil, "which", lambda name: None)
    with pytest.raises(BrokerStartupError, match="node is not on PATH"):
        find_node()


def test_broker_config_auxiliary_policy_round_trip(tmp_path):
    """The strict broker config carries the per-purpose policy exactly
    as parseBrokerMainConfig accepts it, and refuses anything else."""
    base = dict(
        run={"run_id": "r", "job_config_hash": "b", "config_file_sha256": "c",
             "runtime_lock_digest": "d"},
        trial_id="t", session_id="s", config_digest="a" * 64,
        identity={"provider": "p", "model": "m"},
        limits={"maxSteps": 5}, max_output_tokens=64,
        listen_host="0.0.0.0", token_out=tmp_path / "token",
        upstream={"provider": "p", "baseUrl": "http://127.0.0.1:9",
                  "apiKeyEnv": "AEVAL_KEY", "model": "m"},
    )
    path = write_broker_config(
        tmp_path / "broker.json", **base,
        auxiliary_policy={"compaction": "allow"},
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["auxiliaryPolicy"] == {"compaction": "allow"}

    with pytest.raises(BrokerConfigError, match="unknown purposes"):
        write_broker_config(
            tmp_path / "broker2.json", **base,
            auxiliary_policy={"research": "allow"},
        )
    with pytest.raises(BrokerConfigError, match="refuse' or 'allow'"):
        write_broker_config(
            tmp_path / "broker3.json", **base,
            auxiliary_policy={"compaction": "sometimes"},
        )
