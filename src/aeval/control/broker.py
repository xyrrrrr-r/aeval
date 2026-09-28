"""Trusted-host owner side of the model broker lifecycle (P0-4/P0-5).

The Python plugin (the P0-1 owner) starts the compiled TS broker bin
per trial, hands the sandbox nothing but the broker URL and a job
token file, and stops the broker with token cleanup when the trial
ends. The upstream API key NEVER enters the sandbox: it is read by the
broker process on the host from the configured environment variable.

Protocol (docs: dsh-eval-control docs/TESTS/P0-3-host-broker.md):

- spawn ``node <broker_main.js> <config.json>``;
- exactly one stdout line, JSON:
  ``{"ready":true,"url":…,"tokenPath":…,"protocol":"aeval-model-broker/3"}``;
- exit codes: 0 normal close (signal/TTL included), 1 runtime error,
  2 config/credential error, 3 hard budget without trusted metering.

The owner treats anything else as a startup failure: the trial must
not start with an unverified broker (fail-closed, P0-4: 插件激活失败
必须阻止未受控运行).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "BrokerConfigError",
    "BrokerStartupError",
    "BROKER_PROTOCOL",
    "write_broker_config",
    "ModelBrokerProcess",
    "broker_bin_candidates",
]

BROKER_PROTOCOL = "aeval-model-broker/3"


class BrokerConfigError(RuntimeError):
    """The broker configuration cannot be written or is inconsistent."""


class BrokerStartupError(RuntimeError):
    """The broker did not reach the readiness protocol."""


def _identifier(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not all(c.isalnum() or c in "-._" for c in value)
    ):
        raise BrokerConfigError(f"broker config {field_name}: invalid identifier {value!r}")
    return value


def _positive_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 2**53 - 1:
        raise BrokerConfigError(f"broker config {field_name}: must be a positive integer")
    return value


def write_broker_config(
    path: Path,
    *,
    run: Mapping[str, str],
    trial_id: str,
    session_id: str,
    config_digest: str,
    identity: Mapping[str, Any],
    limits: Mapping[str, int],
    max_output_tokens: int,
    listen_host: str,
    token_out: Path,
    listen_port: int | None = None,
    listen_tls: Mapping[str, str] | None = None,
    upstream: Mapping[str, Any],
    token_count: Mapping[str, Any] | None = None,
    timeout_ms: int | None = None,
    token_ttl_ms: int | None = None,
    auxiliary_policy: Mapping[str, str] | None = None,
) -> Path:
    """Write the strict broker config JSON (exact key set, no extras).

    Mirrors ``parseBrokerMainConfig`` in dsh-eval-control: unknown keys
    are a config error there, so the writer refuses to produce them.
    """
    for key in ("run_id", "job_config_hash", "config_file_sha256", "runtime_lock_digest"):
        if not isinstance(run.get(key), str) or not run[key]:
            raise BrokerConfigError(f"broker config run.{key}: required non-empty string")
    _identifier(str(identity.get("provider", "")), "identity.provider")
    _identifier(str(identity.get("model", "")), "identity.model")
    if "reasoningEffort" in identity:
        _identifier(str(identity["reasoningEffort"]), "identity.reasoningEffort")
    _positive_int(max_output_tokens, "maxOutputTokens")
    if not isinstance(listen_host, str) or not listen_host:
        raise BrokerConfigError("broker config listen.host: required")
    if listen_port is not None and (not isinstance(listen_port, int)
                                    or isinstance(listen_port, bool)
                                    or not 1 <= listen_port <= 65535):
        raise BrokerConfigError("broker config listen.port: must be a TCP port")
    if listen_tls is not None:
        for key in ("key", "cert"):
            if not isinstance(listen_tls.get(key), str) or not listen_tls[key]:
                raise BrokerConfigError(f"broker config listen.tls.{key}: required file path")
        if listen_host in ("127.0.0.1", "::1", "localhost") and listen_tls:
            raise BrokerConfigError(
                "broker config listen.tls: a loopback listener does not need TLS "
                "(and the sandbox could not trust it)"
            )
    _identifier(str(upstream.get("apiKeyEnv", "")), "upstream.apiKeyEnv")
    _identifier(str(upstream.get("model", "")), "upstream.model")
    for key in ("baseUrl",):
        if not isinstance(upstream.get(key), str) or not upstream[key]:
            raise BrokerConfigError(f"broker config upstream.{key}: required non-empty string")

    config: dict[str, Any] = {
        "run": dict(run),
        "trialId": _identifier(trial_id, "trialId"),
        "sessionId": _identifier(session_id, "sessionId"),
        "configDigest": config_digest,
        "identity": dict(identity),
        "limits": dict(limits),
        "maxOutputTokens": max_output_tokens,
        "listen": {
            "host": listen_host,
            **({"port": listen_port} if listen_port is not None else {}),
            # non-loopback listeners require TLS; the broker reads the PEM
            # files these paths point at (D20)
            **({"tls": dict(listen_tls)} if listen_tls else {}),
        },
        "tokenOut": str(token_out),
        "upstream": dict(upstream),
    }
    if timeout_ms is not None:
        config["timeoutMs"] = _positive_int(timeout_ms, "timeoutMs")
    if token_ttl_ms is not None:
        config["tokenTtlMs"] = _positive_int(token_ttl_ms, "tokenTtlMs")
    if token_count is not None:
        config["tokenCount"] = dict(token_count)
    if auxiliary_policy is not None:
        # D47: per-purpose decisions for advisory model calls. Only the two
        # known purposes with an explicit decision; missing purposes take the
        # broker's default (refuse), mirroring parseBrokerMainConfig exactly.
        policy = dict(auxiliary_policy)
        unknown = set(policy) - {"compaction", "session-title"}
        if unknown:
            raise BrokerConfigError(
                f"broker config auxiliaryPolicy: unknown purposes {sorted(unknown)}")
        for purpose, decision in policy.items():
            if decision not in ("refuse", "allow"):
                raise BrokerConfigError(
                    f"broker config auxiliaryPolicy.{purpose}: must be 'refuse' or 'allow'")
        config["auxiliaryPolicy"] = policy

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def broker_bin_candidates(control_root: Path) -> list[Path]:
    """Where the compiled broker bin lives under a control checkout."""
    root = Path(control_root)
    return [
        root / "dist" / "broker_main.js",
        root / "node_modules" / ".bin" / "aeval-model-broker",
    ]


@dataclass
class ModelBrokerProcess:
    """Own one broker subprocess from readiness to shutdown.

    Fail-closed semantics: ``start`` returns only after the readiness
    line with the exact protocol marker; any other first line, an
    early exit, or a timeout kills the process and raises
    ``BrokerStartupError`` with the captured output. ``stop`` always
    terminates the process (SIGTERM, then SIGKILL) and returns the
    exit code — shutdown failures are visible, never swallowed.
    """

    node_bin: str
    broker_js: Path
    config_path: Path
    ready_timeout_sec: float = 30.0
    stop_timeout_sec: float = 10.0
    process: subprocess.Popen | None = field(default=None, repr=False)
    url: str | None = None
    token_path: Path | None = None
    _stderr_tail: str = field(default="", repr=False)

    def start(self) -> "ModelBrokerProcess":
        if self.process is not None:
            raise BrokerStartupError("broker process already started")
        if not Path(self.broker_js).is_file():
            raise BrokerStartupError(
                f"broker bin not found: {self.broker_js} — build dsh-eval-control first"
            )
        try:
            self.process = subprocess.Popen(
                [self.node_bin, str(self.broker_js), str(self.config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                # Ask the broker for lease-stop attribution on stderr. It is
                # silent by default (the bin's clean-stderr contract), and
                # this process captures the stream, so a trial that ends
                # with an unexplained closed lease carries its own evidence.
                env={**os.environ, "AEVAL_BROKER_DIAG": "1"},
            )
        except OSError as exc:
            raise BrokerStartupError(f"cannot spawn broker: {exc}") from exc
        try:
            line = self._read_ready_line()
        except BaseException:
            self.kill()
            raise
        self._parse_ready_line(line)
        return self

    def _read_ready_line(self) -> str:
        import selectors

        assert self.process is not None and self.process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            deadline = self.ready_timeout_sec
            import time

            end = time.monotonic() + deadline
            chunks: list[str] = []
            while time.monotonic() < end:
                if self.process.poll() is not None:
                    self._stderr_tail = self._collect_stderr()
                    raise BrokerStartupError(
                        f"broker exited before readiness (code "
                        f"{self.process.returncode}): {self._stderr_tail[-800:]}"
                    )
                ready = selector.select(timeout=max(0.05, end - time.monotonic()))
                if not ready:
                    continue
                chunk = self.process.stdout.readline()
                if not chunk:
                    continue
                chunks.append(chunk)
                if "\n" in chunk:
                    return "".join(chunks).strip()
            self.kill()
            raise BrokerStartupError(
                f"broker did not announce readiness within {self.ready_timeout_sec}s"
            )
        finally:
            selector.close()

    def _parse_ready_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BrokerStartupError(
                f"broker readiness line is not JSON: {line[:200]!r}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            raise BrokerStartupError(f"broker readiness payload is not ready: {payload!r}")
        protocol = payload.get("protocol")
        if protocol != BROKER_PROTOCOL:
            raise BrokerStartupError(
                f"broker protocol mismatch: expected {BROKER_PROTOCOL!r}, "
                f"actual {protocol!r}"
            )
        url = payload.get("url")
        token_path = payload.get("tokenPath")
        if not isinstance(url, str) or not url:
            raise BrokerStartupError(f"broker readiness has no url: {payload!r}")
        if not isinstance(token_path, str) or not token_path:
            raise BrokerStartupError(f"broker readiness has no tokenPath: {payload!r}")
        self.url = url
        self.token_path = Path(token_path)

    def _collect_stderr(self, process: subprocess.Popen | None = None) -> str:
        target = process if process is not None else self.process
        if target is None:
            return self._stderr_tail
        try:
            if target.stderr is not None:
                return target.stderr.read() or ""
        except OSError:
            pass
        return self._stderr_tail

    def stop(self, reason: str = "owner_stop") -> int:
        """Terminate the broker; return its exit code (never raise)."""
        process = self.process
        if process is None:
            return 0
        self.process = None
        if process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=self.stop_timeout_sec)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                process.wait()
        # The process is gone, so the pipe holds every diagnostic it wrote
        # before exiting: keep the tail as evidence for the trial record.
        tail = self._collect_stderr(process).strip()
        if tail:
            self._stderr_tail = tail
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return process.returncode if process.returncode is not None else 0

    def kill(self) -> None:
        """Last-resort teardown for startup failures."""
        if self.process is not None:
            self.stop("startup_failed")

    def __enter__(self) -> "ModelBrokerProcess":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop("context_exit")


def find_node() -> str:
    """Locate a node runtime; the broker bin requires Node."""
    node = shutil.which("node")
    if node is None:
        raise BrokerStartupError(
            "node is not on PATH — the model broker bin requires a Node runtime"
        )
    return node
