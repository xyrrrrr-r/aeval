#!/usr/bin/env python3
"""Pre-lab end-to-end check of the metered facade chain (P2-5b ⑤).

What it proves, with no external credentials and no e2b:

    OpenAI-shaped client → facade (in "sandbox") → real broker → stub upstream

against the PRODUCTION code paths: the broker starts through
``ModelBrokerProcess`` exactly as a trial does, the facade is deployed through
``bootstrap_trial_control`` / ``deploy_generic_facade`` (tar upload, detached
start, health gate), and the lock's recorded facade digest is verified before
anything is uploaded.

TWO chains run (AGENT-ABSTRACTION-2 §4.1), each against its own broker and
facade deployment:

- the chat arm: an agent declaring ``openai_chat``; the facade serves
  ``/v1/chat/completions`` and the broker's upstream speaks chat-completions;
- the responses arm: an agent declaring ``openai_responses`` (the deepagent
  adapter's own declaration); the facade serves ``/v1/responses`` only and
  the broker's upstream speaks the OpenAI Responses wire to the stub.

Each stub upstream speaks its protocol's SSE and reports usage, so the broker
really counts tokens — which is what makes the second half of each check
meaningful: once the lease's token cap is consumed, further calls must be
refused (that refusal is the metering being real, not declared).

This is a verification tool, not runtime code, and it belongs on the lab host:
the defaults are the production paths (/opt/aeval-facade, /run/aeval/trial-token)
and the sandbox tools (setsid, tar, node) are part of what is under test.
``--facade-root``/``--token-path`` exist only for a scratch re-run on the same host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from aeval.contracts import (  # noqa: E402
    RunBinding,
    RuntimeLock,
    TrialPaths,
    control_config_digest,
)
from aeval.control.bootstrap import (  # noqa: E402
    SANDBOX_TOKEN_PATH,
    bootstrap_trial_control,
    deploy_generic_facade,
    resolve_facade_dist,
)
from aeval.control.broker import ModelBrokerProcess, write_broker_config  # noqa: E402
from aeval.hooks.context import EvaluationContext, TrialState  # noqa: E402
from aeval.suite_loader.loader import load_suite  # noqa: E402

STUB_REPLY = "hello from the stub upstream"
STUB_COMPLETION_TOKENS = 80


def _estimated_input_tokens(raw: bytes) -> int:
    """A stand-in for the real tokenizer behind /tokens/count.

    Production counts the exact wire body with a provider tokenizer and the
    provider reports the same prompt size in its usage, so the two agree.
    The stub must be equally self-consistent: the broker fails a call whose
    reported prompt tokens exceed the counted bound (AEVAL_TOKEN_BOUND_VIOLATED),
    which is exactly what caught the first version of this harness.
    """
    return max(1, len(raw) // 4)


class _StubUpstream(BaseHTTPRequestHandler):
    """A provider stub that speaks BOTH upstream wires and reports usage."""

    calls = 0
    responses_calls = 0
    counts = 0

    def log_message(self, *args):  # noqa: ANN002 - silence the default logging
        return

    def do_POST(self) -> None:  # noqa: N802 - http.server's API
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        if self.path.endswith("/tokens/count"):
            # the broker's input-token bound: the count must be trusted, so a
            # failure here fails the call closed (which is the point of it)
            type(self).counts += 1
            payload = json.dumps({"inputTokens": _estimated_input_tokens(raw)}).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.endswith("/responses"):
            self._serve_responses(raw, body)
            return
        if not self.path.endswith("/chat/completions"):
            self.send_error(404, "the stub serves /chat/completions, /responses and /tokens/count")
            return
        self._serve_chat(raw, body)

    def _serve_chat(self, raw: bytes, body: dict) -> None:
        type(self).calls += 1
        # A real provider obeys the max_tokens the broker clamped to the lease
        # budget. Ignoring it made the broker fail the call with
        # AEVAL_TOKEN_BOUND_VIOLATED instead of running out of budget.
        requested = body.get("max_tokens")
        completion = STUB_COMPLETION_TOKENS if not isinstance(requested, int) else min(STUB_COMPLETION_TOKENS, requested)
        frame = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "model": body.get("model", "stub")}
        chunks = [
            {**frame, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            {**frame, "choices": [{"index": 0, "delta": {"content": STUB_REPLY}, "finish_reason": None}]},
            # usage arrives on its own chunk, the way a real gateway sends it
            {**frame, "choices": [],
             "usage": {"prompt_tokens": _estimated_input_tokens(raw),
                       "completion_tokens": completion,
                       "total_tokens": _estimated_input_tokens(raw) + completion}},
            {**frame, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        self._sse(payload)

    def _serve_responses(self, raw: bytes, body: dict) -> None:
        """The Responses wire (DeepSeek shape): semantic SSE, no [DONE].

        Self-consistency matters as much as shape: ``input_tokens`` is derived
        from the exact dispatch body the same way /tokens/count derives the
        bound, so the broker's cross-check (usage vs counted bound) passes —
        which is precisely what makes the responses arm's metering real.
        """
        type(self).responses_calls += 1
        requested = body.get("max_output_tokens")
        completion = STUB_COMPLETION_TOKENS if not isinstance(requested, int) else min(STUB_COMPLETION_TOKENS, requested)
        input_tokens = _estimated_input_tokens(raw)
        model = body.get("model", "stub")
        events = [
            {"type": "response.created", "response": {"id": "resp-stub", "status": "in_progress", "model": model}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "msg_0", "type": "message", "role": "assistant"}},
            {"type": "response.output_text.delta", "item_id": "msg_0", "output_index": 0, "delta": STUB_REPLY},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"id": "msg_0", "type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": STUB_REPLY}]}},
            {"type": "response.completed",
             "response": {"id": "resp-stub", "status": "completed", "model": model,
                          "usage": {"input_tokens": input_tokens, "output_tokens": completion,
                                    "total_tokens": input_tokens + completion}}},
        ]
        payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        self._sse(payload)

    def _sse(self, payload: str) -> None:
        encoded = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class LocalSandbox:
    """The production upload/exec calls, executed on THIS host.

    ``deploy_generic_facade`` only ever issues mkdir/tar/setsid-node/probe
    commands, so running them here exercises the real command strings —
    including the detached start and the node health probe. This is why the
    harness belongs on the lab host: setsid, tar and node are the environment
    under test, not incidental details of the operator's laptop.
    """

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.commands: list[str] = []

    async def upload_file(self, source: str, target: str) -> None:
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)

    async def exec(self, command: str):
        self.commands.append(command)
        completed = subprocess.run(  # noqa: S602 - the point is a real shell
            command, shell=True, capture_output=True, text=True, cwd=self.work_dir
        )
        return SimpleNamespace(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class _ChatFacadeAgent:
    """The chat arm's declared flavor (openai_chat: facade serves /v1/chat/completions)."""

    CONTROL_STACK = "deepagent-facade"
    MODEL_ROUTING = {
        "agent_protocol": "openai_chat",
        "env": {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY"},
    }


class _ResponsesFacadeAgent:
    """The responses arm's declared flavor — the deepagent adapter declares the same."""

    CONTROL_STACK = "deepagent-facade"
    MODEL_ROUTING = {
        "agent_protocol": "openai_responses",
        "env": {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY"},
    }


def _request(url: str, body: dict, timeout: float = 30.0) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer sk-whatever"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed localhost URL
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


async def _run(args) -> dict:
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    facade_root = Path(args.facade_root).resolve() if args.facade_root else None
    if args.token_path:
        token_path = args.token_path
    elif facade_root is not None:
        # a local run may not write /run; the token path is passed through to
        # the facade anyway, so the chain stays identical
        token_path = str(work_dir / "trial-token")
    else:
        token_path = SANDBOX_TOKEN_PATH.as_posix()

    # The target environment supplies these; refusing early beats a puzzling
    # "did not become healthy" thirty seconds later.
    missing = [tool for tool in ("setsid", "tar", args.node) if shutil.which(tool) is None]
    if missing:
        raise SystemExit(f"the harness runs where the sandbox tools exist; missing: {missing}")

    stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
    stub_port = stub.server_address[1]
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    run_binding = RunBinding(
        run_id="facade-smoke", job_config_hash="a" * 64,
        config_file_sha256="b" * 64, runtime_lock_digest="c" * 64,
    )
    trial_id, session_id = "facade-smoke", "facade-smoke-session"
    broker_port = args.broker_port
    # The lab pins the broker listener to a public address behind a private
    # signer (listenTls + controlCa in the operator's spec). ``--tls`` runs the
    # broker that way against a throwaway self-signed certificate: that is the
    # only difference that matters for the facade's outbound trust.
    listen_host = args.listen_host
    listen_tls = None
    ca_path: Path | None = None
    if args.tls:
        # The broker refuses TLS on a loopback listener ("the sandbox could not
        # trust it"), which is exactly why the operator's spec pins a public
        # address: a remote sandbox can only reach the host that way.
        if listen_host in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit(
                "the broker refuses TLS on a loopback listener — pass "
                "--listen-host with the address the sandbox must reach"
            )
        tls_dir = work_dir / "tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        ca_path, key_path = tls_dir / "cert.pem", tls_dir / "key.pem"
        subprocess.run(  # noqa: S603 - fixed argv, throwaway self-signed cert
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key_path), "-out", str(ca_path), "-days", "2",
             "-subj", f"/CN={listen_host}",
             "-addext", f"subjectAltName=IP:{listen_host},IP:127.0.0.1"],
            check=True, capture_output=True,
        )
        listen_tls = {"cert": str(ca_path), "key": str(key_path)}
    scheme = "https" if listen_tls else "http"
    identity = {"provider": "stub", "model": "stub-model"}
    limits = {"maxSteps": args.max_steps, "maxTokens": args.max_tokens}
    paths = TrialPaths(
        sandbox_cwd=str(work_dir / "workspace"),
        dsh_home=str(work_dir / "dsh-home"),
        bundle_path=str(work_dir / "bundle.json"),
        session_root="dsh-home",
        download_root=f"trials/{trial_id}/agent",
    )

    # ── the real broker, constructed exactly as a trial constructs it ──
    trial_dir = work_dir / "broker" / trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)
    control_config = {
        "run": run_binding.model_dump(mode="json"), "trialId": trial_id,
        "sessionId": session_id, "sessionRoot": paths.session_root,
        "bundlePath": paths.bundle_path,
        "gatewayUrl": f"{scheme}://{listen_host}:{broker_port}",
        "jobTokenFile": token_path, "provider": identity["provider"],
        "model": identity["model"], "refuseAuxiliaryCalls": True, **limits,
    }
    control_config["configDigest"] = control_config_digest(control_config)
    config_path = write_broker_config(
        trial_dir / "broker.json",
        run=run_binding.model_dump(mode="json"), trial_id=trial_id, session_id=session_id,
        config_digest=control_config["configDigest"], identity=identity, limits=limits,
        max_output_tokens=args.max_output_tokens, listen_host=listen_host,
        listen_port=broker_port, token_out=trial_dir / "job-token",
        upstream={"provider": "stub", "model": "stub-model",
                  "baseUrl": f"http://127.0.0.1:{stub_port}/v1",
                  "apiKeyEnv": "STUB_UPSTREAM_KEY"},
        token_count={"endpoint": f"http://127.0.0.1:{stub_port}/v1/tokens/count",
                     "margin": 8},
        listen_tls=listen_tls,
    )
    os.environ["STUB_UPSTREAM_KEY"] = "stub-key-not-a-secret"
    broker = ModelBrokerProcess(
        node_bin=args.node, broker_js=Path(args.broker_js), config_path=config_path,
    ).start()

    facade_dist = Path(args.facade_dist) if args.facade_dist else resolve_facade_dist()
    facade_url = f"http://127.0.0.1:{args.facade_port}"

    try:
        # ── the lock covers what gets deployed, and its bytes are re-checked ──
        from aeval.provenance import build_runtime_lock, fingerprint_control_dist

        lock = build_runtime_lock(images={}, agent_ids=["deepagent-facade-smoke"],
                                  facade_dist=facade_dist)
        assert lock.facade_dist is not None
        # a deliberately stale lock must be REFUSED before any upload
        stale_context = _context(work_dir, lock.model_copy(
            update={"facade_dist": lock.facade_dist.model_copy(update={"sha256": "f" * 64})}
        ))
        stale_refused = None
        stale_sandbox = LocalSandbox(work_dir)
        try:
            await bootstrap_trial_control(
                environment=stale_sandbox, context=stale_context, trial_id=trial_id,
                paths=paths, broker=broker, provider=identity["provider"],
                model=identity["model"], job_token_file=token_path, agent=_ChatFacadeAgent(),
                facade_dist=facade_dist,
                facade_root=facade_root.as_posix() if facade_root else None,
            )
        except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
            stale_refused = f"{type(exc).__name__}: {exc}"
        # nothing may have been uploaded or started for a dist the lock does
        # not cover: the refusal has to happen BEFORE the tree goes over
        stale_uploaded = any("facade_main.js" in command for command in stale_sandbox.commands)

        # ── the real deployment path: token upload, tree upload, start, health ──
        sandbox = LocalSandbox(work_dir)
        context = _context(work_dir, lock)
        _, config = await bootstrap_trial_control(
            environment=sandbox, context=context, trial_id=trial_id, paths=paths,
            broker=broker, provider=identity["provider"], model=identity["model"],
            job_token_file=token_path, agent=_ChatFacadeAgent(), facade_dist=facade_dist,
            facade_root=facade_root.as_posix() if facade_root else None,
            control_ca=ca_path,
        )
        health = _get(f"{facade_url}/healthz")
        info_limits = _lease_limits(broker.url, token_path, ca_path)

        # The CA is load-bearing, not decoration: the same deployment against
        # the same TLS listener without control_ca must fail its health gate
        # (node cannot verify the self-signed broker certificate).
        untrusted_outcome = "not_attempted"
        if args.tls:
            try:
                await deploy_generic_facade(
                    environment=LocalSandbox(work_dir), facade_dist=facade_dist,
                    gateway_url=config["gatewayUrl"], token_file=token_path,
                    port=args.facade_port + 1,
                    root=(Path(args.facade_root) / "untrusted" if args.facade_root
                          else Path("/opt/aeval-facade-untrusted")).as_posix(),
                    health_timeout_sec=8.0,
                )
                untrusted_outcome = "deployed_without_the_ca"
            except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
                untrusted_outcome = f"{type(exc).__name__}"

        # ── the chain: OpenAI in, metered completion out ──
        attempts: list[dict] = []
        stream_ok = False
        for index in range(args.max_calls):
            streaming = index == 1  # exercise SSE while the budget still allows a call
            status, body = _request(f"{facade_url}/v1/chat/completions", {
                "model": identity["model"],
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": streaming,
            })
            if streaming and status == 200:
                stream_ok = "[DONE]" in body or "data:" in body
            attempts.append({"call": index + 1, "stream": streaming, "status": status,
                             "code": _error_code(body)})
        codes = [attempt["code"] for attempt in attempts if attempt["code"]]
        successes = [attempt for attempt in attempts if attempt["status"] == 200]
        refused = [attempt for attempt in attempts if attempt["status"] == 402]
        first_refusal = attempts.index(refused[0]) if refused else None
        later_success = first_refusal is not None and any(
            attempt["status"] == 200 for attempt in attempts[first_refusal:]
        )

        # The chat facade must NOT have grown a responses endpoint: an existing
        # deployment's surface is unchanged until a routing declares more.
        chat_gate_status, _ = _request(f"{facade_url}/v1/responses", {
            "model": identity["model"], "input": "say hello",
        })

        # ── the responses arm: its own broker (upstream protocol responses),
        #    its own facade deployment (AEVAL_FACADE_PROTOCOLS=responses,
        #    derived from the agent's declaration), same production paths ──
        responses_chain = await _responses_chain(
            args, work_dir=work_dir, stub_port=stub_port, facade_dist=facade_dist,
            facade_root=facade_root, identity=identity, limits=limits,
        )
        return {
            "facade_dist": str(facade_dist),
            "facade_url": facade_url,
            "broker_url": broker.url,
            "stub_upstream_calls": _StubUpstream.calls,
            "stub_count_calls": _StubUpstream.counts,
            "health": health,
            "lease_limits": info_limits,
            "tls": bool(args.tls),
            "tls_without_ca_outcome": untrusted_outcome,
            "lock_facade_digest": lock.facade_dist.sha256[:16],
            "stale_lock_refused_with": stale_refused,
            "stale_lock_uploaded_anything": stale_uploaded,
            "model": identity["model"],
            "control_config_limits": {key: config.get(key) for key in ("maxSteps", "maxTokens")},
            "attempts": attempts,
            "refusal_codes": sorted(set(codes)),
            "streaming_ok": stream_ok,
            "chat_facade_responses_endpoint_status": chat_gate_status,
            "responses_chain": responses_chain,
            "facade_log_tail": _log_tail(
                "/tmp/aeval-facade.log"
            ) if not any(a["status"] == 200 for a in attempts) else "",
            "checks": {
                "stale_lock_refused": stale_refused is not None,
                "stale_lock_uploaded_nothing": not stale_uploaded,
                "deployed_entry_present": (Path(args.facade_root or "/opt/aeval-facade") / "dist" / "facade_main.js").is_file(),
                "health_ok": health.get("ok") is True,
                "health_serves_chat_only": health.get("protocols") == ["chat_completions"],
                "completion_ok": bool(successes),
                "streaming_ok": stream_ok,
                "stub_reached": _StubUpstream.calls > 0,
                "token_count_endpoint_used": _StubUpstream.counts > 0,
                "cap_fires_402": bool(refused),
                "no_success_after_cap": not later_success,
                # the chat facade's surface is unchanged: /v1/responses is 404
                "chat_facade_refuses_responses": chat_gate_status == 404,
                # the broker's published lease must pin exactly the cap under
                # test; in production the sandbox adapter compares this /info
                # payload field by field with the composed control config, and
                # here the suite's own budget is deliberately not the cap the
                # harness exercises.
                "info_limits_mirror_the_lease": info_limits == limits,
                "the_ca_is_load_bearing": (not args.tls) or untrusted_outcome != "deployed_without_the_ca",
                # the responses arm's own checks (flattened for the reporter)
                **{
                    key: value
                    for key, value in responses_chain["checks"].items()
                },
            },
        }
    finally:
        broker.stop("smoke_end")
        stub.shutdown()
        _stop_facade(Path(args.facade_root) if args.facade_root else Path("/opt/aeval-facade"))
        _stop_facade(_responses_facade_root(args))


def _responses_facade_root(facade_root: Path | None) -> Path:
    """The responses arm's deployment root (its own tree, its own log)."""
    base = facade_root if facade_root else Path("/opt/aeval-facade")
    return base.with_name(base.name + "-responses")


async def _responses_chain(
    args, *, work_dir: Path, stub_port: int, facade_dist: Path,
    facade_root: Path | None, identity: dict, limits: dict,
) -> dict:
    """The responses arm, end to end through the same production paths.

    A second broker whose upstream speaks the responses wire (protocol
    'responses' — the DeepSeek-shaped upstream of AGENT-ABSTRACTION-2 §4.4),
    deployed against by an agent declaring ``openai_responses`` so the facade
    serves ``/v1/responses`` only. Everything else — lock coverage, tar
    upload, detached start, health gate, budget refusal — is the same code
    the chat arm just exercised.
    """
    trial_id = "facade-smoke-responses"
    session_id = "facade-smoke-responses-session"
    broker_port = args.responses_broker_port
    facade_root_posix = _responses_facade_root(facade_root).as_posix()
    token_path = str(work_dir / "trial-token-responses")

    run_binding = RunBinding(
        run_id="facade-smoke", job_config_hash="a" * 64,
        config_file_sha256="b" * 64, runtime_lock_digest="c" * 64,
    )
    paths = TrialPaths(
        sandbox_cwd=str(work_dir / "workspace-r"), dsh_home=str(work_dir / "dsh-home-r"),
        bundle_path=str(work_dir / "bundle-r.json"), session_root="dsh-home-r",
        download_root=f"trials/{trial_id}/agent",
    )
    trial_dir = work_dir / "broker" / trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)
    control_config = {
        "run": run_binding.model_dump(mode="json"), "trialId": trial_id,
        "sessionId": session_id, "sessionRoot": paths.session_root,
        "bundlePath": paths.bundle_path,
        "gatewayUrl": f"http://127.0.0.1:{broker_port}",
        "jobTokenFile": token_path, "provider": identity["provider"],
        "model": identity["model"], "refuseAuxiliaryCalls": True, **limits,
    }
    control_config["configDigest"] = control_config_digest(control_config)
    config_path = write_broker_config(
        trial_dir / "broker.json",
        run=run_binding.model_dump(mode="json"), trial_id=trial_id, session_id=session_id,
        config_digest=control_config["configDigest"], identity=identity, limits=limits,
        max_output_tokens=args.max_output_tokens, listen_host="127.0.0.1",
        listen_port=broker_port, token_out=trial_dir / "job-token",
        upstream={"provider": "stub", "model": identity["model"],
                  "baseUrl": f"http://127.0.0.1:{stub_port}/v1",
                  "apiKeyEnv": "STUB_UPSTREAM_KEY",
                  "protocol": "responses"},
        token_count={"endpoint": f"http://127.0.0.1:{stub_port}/v1/tokens/count",
                     "margin": 8},
    )
    broker = ModelBrokerProcess(
        node_bin=args.node, broker_js=Path(args.broker_js), config_path=config_path,
    ).start()
    facade_url = f"http://127.0.0.1:{args.responses_facade_port}"
    try:
        from aeval.provenance import build_runtime_lock

        lock = build_runtime_lock(images={}, agent_ids=["deepagent-facade-smoke"],
                                  facade_dist=facade_dist)
        context = _context(work_dir, lock, trial_id=trial_id)
        await bootstrap_trial_control(
            environment=LocalSandbox(work_dir), context=context, trial_id=trial_id,
            paths=paths, broker=broker, provider=identity["provider"],
            model=identity["model"], job_token_file=token_path,
            agent=_ResponsesFacadeAgent(), facade_dist=facade_dist,
            facade_root=facade_root_posix,
        )
        health = _get(f"{facade_url}/healthz")

        attempts: list[dict] = []
        stream_ok = False
        completion_ok = False
        for index in range(args.max_calls):
            streaming = index == 1
            status, body = _request(f"{facade_url}/v1/responses", {
                "model": identity["model"],
                "input": "say hello",
                "stream": streaming,
            })
            if streaming and status == 200:
                stream_ok = "event: response.completed" in body
            if status == 200 and not streaming:
                try:
                    parsed = json.loads(body)
                    completion_ok = parsed.get("output", [{}])[0].get(
                        "content", [{}]
                    )[0].get("text") == STUB_REPLY
                except (json.JSONDecodeError, IndexError, AttributeError):
                    completion_ok = False
            attempts.append({"call": index + 1, "stream": streaming, "status": status,
                             "code": _error_code(body)})
        refused = [attempt for attempt in attempts if attempt["status"] == 402]
        first_refusal = attempts.index(refused[0]) if refused else None
        later_success = first_refusal is not None and any(
            attempt["status"] == 200 for attempt in attempts[first_refusal:]
        )
        # gating: the responses facade serves responses ONLY
        chat_status, _ = _request(f"{facade_url}/v1/chat/completions", {
            "model": identity["model"],
            "messages": [{"role": "user", "content": "say hello"}],
        })
        return {
            "facade_url": facade_url,
            "broker_url": broker.url,
            "stub_responses_calls": _StubUpstream.responses_calls,
            "health": health,
            "attempts": attempts,
            "chat_endpoint_status": chat_status,
            "checks": {
                "responses_health_ok": health.get("ok") is True,
                "responses_health_serves_responses_only": health.get("protocols") == ["responses"],
                "responses_completion_ok": completion_ok,
                "responses_streaming_ok": stream_ok,
                "responses_stub_reached": _StubUpstream.responses_calls > 0,
                "responses_cap_fires_402": bool(refused),
                "responses_no_success_after_cap": not later_success,
                "responses_facade_refuses_chat": chat_status == 404,
            },
        }
    finally:
        broker.stop("smoke_end_responses")


def _log_tail(path: str, lines: int = 12) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def _stop_facade(root: Path) -> None:
    """Stop the detached facade this run started (scoped to its own root).

    The bracket keeps the pattern from matching this harness's OWN command
    line, which would make pkill kill the run that asked for the cleanup.
    """
    subprocess.run(  # noqa: S602 - a scoped pkill, no other facade is touched
        f"pkill -f '{root.as_posix()}/dist/[f]acade_main.js' || true",
        shell=True, capture_output=True, text=True,
    )


def _context(work_dir: Path, lock: RuntimeLock, trial_id: str = "facade-smoke") -> EvaluationContext:
    context = EvaluationContext(
        run_id="facade-smoke", runtime_lock=lock,
        suite=load_suite(REPO / "suites" / "deepagent-budget"),
        run_dir=work_dir, store_path=work_dir / "store.sqlite3",
    )
    context.run_binding = RunBinding(
        run_id="facade-smoke", job_config_hash="a" * 64,
        config_file_sha256="b" * 64, runtime_lock_digest=lock.digest(),
    )
    state = TrialState(trial_id=trial_id, phase="running")
    state.trial_dir = work_dir / "trials" / trial_id
    state.trial_dir.mkdir(parents=True, exist_ok=True)
    context.trials[trial_id] = state
    return context


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - localhost
        return json.loads(response.read().decode("utf-8"))


def _lease_limits(broker_url: str, token_path: str, ca_path: Path | None = None) -> dict:
    """The lease limits the broker publishes on /info (bearer-authenticated)."""
    token = Path(token_path).read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"{broker_url}/info", headers={"authorization": f"Bearer {token}"}, method="GET"
    )
    context = None
    if broker_url.startswith("https") and ca_path is not None:
        context = ssl.create_default_context(cafile=str(ca_path))
    with urllib.request.urlopen(request, timeout=10, context=context) as response:  # noqa: S310
        return dict(json.loads(response.read().decode("utf-8")).get("limits", {}))


def _error_code(body: str) -> str | None:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
        return str(parsed["error"].get("code"))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=os.environ.get("AEVAL_SMOKE_NODE", "node"))
    parser.add_argument("--broker-js", default=str(REPO / "control" / "dist" / "broker_main.js"))
    parser.add_argument("--facade-dist", default=None)
    parser.add_argument("--work-dir", default="/tmp/aeval-facade-smoke")
    parser.add_argument("--facade-root", default=None,
                        help="sandbox root for the facade (default: production /opt/aeval-facade)")
    parser.add_argument("--token-path", default=None)
    parser.add_argument("--broker-port", type=int, default=5199)
    parser.add_argument("--facade-port", type=int, default=8787)
    parser.add_argument("--responses-broker-port", type=int, default=5198,
                        help="the responses arm's broker listener")
    parser.add_argument("--responses-facade-port", type=int, default=8786,
                        help="the responses arm's facade listener")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=250)
    parser.add_argument("--max-output-tokens", type=int, default=200)
    parser.add_argument("--max-calls", type=int, default=5)
    parser.add_argument("--listen-host", default="127.0.0.1",
                        help="the broker listener the sandbox must reach (TLS needs a non-loopback one)")
    parser.add_argument("--tls", action="store_true",
                        help="run the broker behind a private signer, as the lab spec does")
    args = parser.parse_args()

    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    failed = [name for name, ok in result["checks"].items() if not ok]
    if failed:
        print(f"FAILED CHECKS: {failed}", file=sys.stderr)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
