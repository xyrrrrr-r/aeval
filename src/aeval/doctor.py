"""``aeval doctor`` — a read-only wiring check from "installed" to "reportable".

Every failure this command looks for was met for real, and several of them
disguise themselves downstream: a host that never exported the upstream key
reaches the user as ``MISSING_CREDENTIAL`` inside the sandbox, a loopback
``listenHost`` reaches them as ``the trial's agent exposes no DSH session``,
and a missing token counter reaches them as a broker exit code buried under a
trial that was excluded for "infra". The doctor reports the ROOT cause, at the
host, before a single trial is dispatched.

The command writes nothing: it resolves the same inputs a run resolves, reads
the files it names, and (unless ``--no-probe``) asks the two endpoints a run
needs whether they answer. Output mirrors the control plugin's self-check —
one ``[ok|warn|fail] <id> <detail>`` line per check, with a copy-pasteable
``hint:`` after every check that is not ok — and ``--json`` carries the same
checks as a stable machine-readable document.

Reuse rather than re-derivation: the broker spec is parsed by
``aeval.hooks.broker_lifecycle.parse_broker_spec`` (the parser a run uses), the
suite provenance by ``suite_source_commit`` (the gate a run uses), and the
suite × agent pairing and runtime→image checks by exactly the functions
``aeval check`` uses.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from aeval.agents.control_doctor import (
    ControlDoctorFacts,
    collect_control_doctor_facts,
)

__all__ = [
    "Check",
    "DoctorReport",
    "DOCTOR_SCHEMA",
    "EXIT_DOCTOR_FAIL",
    "ProbeResult",
    "CommandResult",
    "run_doctor",
]

DOCTOR_SCHEMA = "aeval-doctor/1"
EXIT_DOCTOR_FAIL = 3

# The engine window the control stack publishes (its package.json
# ``engines.node``, reached through the declared flavor's doctor facts). This
# is the fallback so the check still has an authority when nothing is
# installed to read it from.
DEFAULT_NODE_ENGINES = "^22.19.0 || >=24.0.0"

# The three checks whose facts belong to a control stack (the compiled control
# package, its direct imports, its profile peer window). They are produced by
# the declared flavor's doctor hook; this module only places them.
CONTROL_FLAVOR_CHECK_IDS: tuple[str, ...] = (
    "control-dist",
    "control-deps",
    "dsh-profile",
)

# The checks, in report order. Fixed so ``--json`` consumers can rely on both
# the id set and the order.
CHECK_IDS: tuple[str, ...] = (
    "node",
    "control-dist",
    "control-deps",
    "broker-spec",
    "upstream-key",
    "token-count",
    "token-endpoint",
    "listen-tls",
    "cert-san",
    "dsh-profile",
    "agents-declaration",
    "suite-provenance",
    "backend",
    "pairing",
    "runtime-image",
    "evidence-trials",
)


# --------------------------------------------------------------------------- #
# report shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Check:
    """One wiring fact: what was looked at, what was found, and the fix."""

    id: str
    status: str          # 'ok' | 'warn' | 'fail'
    detail: str
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class DoctorReport:
    """The whole check, in a stable shape for humans and for ``--json``."""

    mode: str
    checks: tuple[Check, ...]
    tool: str = "aeval"
    schema: str = DOCTOR_SCHEMA

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.status == "fail")

    @property
    def exit_code(self) -> int:
        """0 when nothing failed; a validation error code when anything did."""
        return EXIT_DOCTOR_FAIL if self.failures else 0

    def summary(self) -> dict[str, int]:
        return {
            status: sum(1 for check in self.checks if check.status == status)
            for status in ("ok", "warn", "fail")
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "tool": self.tool,
            "mode": self.mode,
            "summary": self.summary(),
            "checks": [check.as_dict() for check in self.checks],
            "exit_code": self.exit_code,
        }

    def render(self) -> str:
        width = max((len(check.id) for check in self.checks), default=15)
        lines = [f"{self.tool} doctor — mode: {self.mode}"]
        for check in self.checks:
            lines.append(f"  [{check.status}] {check.id:<{width}} {check.detail}")
            if check.status != "ok" and check.hint:
                lines.append(f"  hint: {check.hint}")
        return "\n".join(lines)

    def render_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# injected leaf operations (network + processes live behind these)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeResult:
    """Whether an endpoint answered, and what it said (never its body)."""

    reachable: bool
    detail: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _http_probe(
    url: str, *, method: str = "GET", body: bytes | None = None, timeout: float = 5.0
) -> ProbeResult:
    """Ask one URL whether it is up. Any HTTP status counts as reachable.

    A 4xx still proves something is listening and speaking HTTP — which is what
    the token counter's contract promises ("an invalid body is answered too").
    Only transport failures count as unreachable.
    """
    request = urllib_request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("content-type", "application/json")
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return ProbeResult(True, f"HTTP {response.status}")
    except urllib_error.HTTPError as exc:
        return ProbeResult(True, f"HTTP {exc.code} (service is answering)")
    except (urllib_error.URLError, OSError, ValueError) as exc:
        return ProbeResult(False, f"{type(exc).__name__}: {exc}")


def _run_command(argv: Sequence[str], timeout: float = 30.0) -> CommandResult:
    import subprocess

    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{argv[0]}: command not found ({exc.strerror or exc})")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]}: timed out after {timeout:.0f}s")
    except OSError as exc:
        return CommandResult(126, "", f"{argv[0]}: {exc}")
    return CommandResult(done.returncode, done.stdout, done.stderr)


def _tail(text: str, limit: int = 300) -> str:
    squashed = " ".join(text.split())
    return squashed if len(squashed) <= limit else "…" + squashed[-limit:]


# --------------------------------------------------------------------------- #
# version ranges (semver, the subset the peer windows actually use)
# --------------------------------------------------------------------------- #


def _split_version(version: str) -> tuple[int, int, int, tuple[str, ...]]:
    """``'0.2.0-rc.2'`` → ``(0, 2, 0, ('rc', '2'))``.

    Build metadata is dropped (semver says it never affects precedence), and a
    leading ``v`` is tolerated because that is what ``node --version`` prints.
    """
    text = version.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    core, _, pre = text.partition("-")
    parts = core.split("+", 1)[0].split(".")
    numbers: list[int] = []
    for index in range(3):
        chunk = parts[index] if index < len(parts) else "0"
        digits = "".join(c for c in chunk if c.isdigit())
        numbers.append(int(digits) if digits else 0)
    identifiers = tuple(pre.split("+", 1)[0].split(".")) if pre else ()
    return numbers[0], numbers[1], numbers[2], identifiers


def _compare_identifiers(left: str, right: str) -> int:
    left_numeric = left.isdigit()
    right_numeric = right.isdigit()
    if left_numeric and right_numeric:
        return (int(left) > int(right)) - (int(left) < int(right))
    if left_numeric != right_numeric:
        # numeric identifiers have LOWER precedence than alphanumeric ones
        return -1 if left_numeric else 1
    return (left > right) - (left < right)


def _compare_versions(left: str, right: str) -> int:
    """Semver precedence: ``-1`` left < right, ``0`` equal, ``1`` left > right."""
    lmaj, lmin, lpatch, lpre = _split_version(left)
    rmaj, rmin, rpatch, rpre = _split_version(right)
    core = (lmaj, lmin, lpatch)
    other = (rmaj, rmin, rpatch)
    if core != other:
        return (core > other) - (core < other)
    if lpre == rpre:
        return 0
    if not lpre:
        return 1                      # a release outranks its prereleases
    if not rpre:
        return -1
    for lpart, rpart in zip(lpre, rpre):
        verdict = _compare_identifiers(lpart, rpart)
        if verdict:
            return verdict
    return (len(lpre) > len(rpre)) - (len(lpre) < len(rpre))


def _comparator_satisfied(version: str, operator: str, expected: str) -> bool:
    verdict = _compare_versions(version, expected)
    if operator in (">=", ">", "<", "<="):
        return {
            ">=": verdict >= 0,
            ">": verdict > 0,
            "<=": verdict <= 0,
            "<": verdict < 0,
        }[operator]
    return verdict == 0               # '=' or a bare version


def version_satisfies(version: str, range_text: str) -> bool:
    """Does ``version`` satisfy a node-style range (``||`` of space-joined sets)?

    Supports the comparators the peer windows and ``engines`` fields use —
    ``>= > <= < =`` and bare versions, plus ``^`` and ``~`` — because both of
    those fields are authored by the control package and must be read as npm
    reads them, not re-guessed.
    """
    text = (range_text or "").strip()
    if not text or text == "*":
        return True
    for alternative in text.split("||"):
        tokens = alternative.split()
        if not tokens:
            continue
        if all(_token_satisfied(version, token) for token in tokens):
            return True
    return False


def _token_satisfied(version: str, token: str) -> bool:
    token = token.strip()
    if not token:
        return True
    if token.startswith("^") or token.startswith("~"):
        base = token[1:]
        major, minor, patch, _ = _split_version(base)
        if token.startswith("^"):
            upper = (
                f"{major + 1}.0.0" if major > 0
                else f"0.{minor + 1}.0" if minor > 0
                else f"0.0.{patch + 1}"
            )
        else:
            upper = f"{major}.{minor + 1}.0"
        return (
            _comparator_satisfied(version, ">=", base)
            and _comparator_satisfied(version, "<", upper)
        )
    for operator in (">=", "<=", ">", "<", "="):
        if token.startswith(operator):
            return _comparator_satisfied(version, operator, token[len(operator):].lstrip("v"))
    return _comparator_satisfied(version, "=", token.lstrip("v"))


# --------------------------------------------------------------------------- #
# shared state derived once
# --------------------------------------------------------------------------- #


@dataclass
class _SpecState:
    path: Path | None = None
    raw: dict[str, Any] | None = None
    read_error: str | None = None
    parse_error: str | None = None
    parsed: Any = None            # BrokerSpec when parse succeeded


@dataclass
class _PairingState:
    resolved: Any = None
    job: Any = None
    error: str | None = None


@dataclass
class _Context:
    broker_json: Path | None
    suite: Path | None
    agent: str | None
    profile: str | None
    agents_dir: Path
    backend: str | None
    run_dir: Path | None
    node_bin: str
    no_probe: bool
    probe_timeout: float
    env: Mapping[str, str]
    probe: Callable[..., ProbeResult]
    command: Callable[..., CommandResult]
    control_facts: ControlDoctorFacts = field(default_factory=ControlDoctorFacts)
    spec: _SpecState = field(default_factory=_SpecState)
    pairing: _PairingState = field(default_factory=_PairingState)


def _plugins_env() -> str:
    from aeval.hooks.broker_lifecycle import BROKER_SPEC_ENV

    return BROKER_SPEC_ENV


def _read_spec(ctx: _Context) -> _SpecState:
    from aeval.hooks.broker_lifecycle import BrokerSpecError, parse_broker_spec

    state = _SpecState(path=ctx.broker_json)
    if state.path is None:
        return state
    path = Path(state.path)
    if not path.is_file():
        state.read_error = f"broker spec {_plugins_env()}={path} is not a file"
        return state
    try:
        loaded = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        state.read_error = f"broker spec {path} is unreadable: {exc}"
        return state
    if not isinstance(loaded, dict):
        state.read_error = f"broker spec {path} must be a JSON object"
        return state
    state.raw = loaded
    try:
        state.parsed = parse_broker_spec(str(path))
    except BrokerSpecError as exc:
        state.parse_error = str(exc)
    return state


def _pairing_scope(ctx: _Context) -> _PairingState:
    from aeval.suite_loader.composition import compose_harbor_job
    from aeval.suite_loader.loader import load_suite

    state = _PairingState()
    if ctx.suite is None or not ctx.agent:
        return state
    try:
        state.resolved = load_suite(Path(ctx.suite))
        state.job = compose_harbor_job(
            state.resolved,
            agent=ctx.agent,
            agent_profile=ctx.profile,
            agents_root=Path(ctx.agents_dir),
        )
    except Exception as exc:                      # SuiteError/ValueError/OSError
        state.error = f"{type(exc).__name__}: {exc}" if not str(exc) else str(exc)
    return state


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #


def _check_node(ctx: _Context, spec: _SpecState) -> Check:
    # The window the installed control stack declares wins; the fallback keeps
    # the check meaningful when nothing is installed to read it from.
    engines = ctx.control_facts.node_engines or DEFAULT_NODE_ENGINES
    result = ctx.command([ctx.node_bin, "--version"], timeout=ctx.probe_timeout)
    if result.returncode != 0:
        detail = (
            f"{ctx.node_bin} is not runnable: "
            f"{_tail(result.stderr) or f'exit {result.returncode}'}"
        )
        return Check("node", "fail", detail, _hint_install_node(engines))
    version = result.stdout.strip() or result.stderr.strip()
    if not version_satisfies(version, engines):
        return Check(
            "node",
            "fail",
            f"{version} — the control stack requires {engines}",
            _hint_install_node(engines),
        )
    return Check("node", "ok", f"{version} (requires {engines})")


def _hint_install_node(engines: str) -> str:
    return (
        f"nvm install 24 || nvm install 22   # then re-run: the control stack "
        f"requires {engines}"
    )


def _control_check(facts: ControlDoctorFacts, check_id: str, ctx: _Context) -> Check:
    """A control-stack check: the declared flavor's fact, or why there is none."""
    reported = facts.checks.get(check_id)
    if reported is not None:
        status, detail, hint = reported
        return Check(check_id, status, detail, hint)
    note = "; ".join(facts.notes) if facts.notes else (
        f"no control stack is registered by the declarations under {ctx.agents_dir}"
    )
    return Check(
        check_id,
        "warn",
        f"not checked — {note}",
        f"aeval agents --init --agents-dir {ctx.agents_dir}",
    )


def _check_broker_spec(ctx: _Context, spec: _SpecState) -> Check:
    if spec.path is None:
        return Check(
            "broker-spec",
            "warn",
            f"{_plugins_env()} is not set — standalone: a run here would talk to the "
            "upstream directly, with no broker lease and no token bound",
            f"export {_plugins_env()}=/abs/path/to/broker.json",
        )
    if spec.read_error:
        return Check("broker-spec", "fail", spec.read_error, _hint_spec_file(ctx, valid=False))
    if spec.parse_error:
        return Check("broker-spec", "fail", spec.parse_error, _hint_spec_file(ctx))
    problems: list[str] = []
    upstream = (spec.raw or {}).get("upstream")
    if not isinstance(upstream, dict):
        problems.append("upstream must be an object")
    else:
        empty = [
            key
            for key in ("provider", "baseUrl", "model", "apiKeyEnv")
            if not isinstance(upstream.get(key), str) or not upstream.get(key, "").strip()
        ]
        if empty:
            problems.append(
                "upstream is missing " + ", ".join(empty)
                + " — the broker refuses with upstream.model: invalid identifier ''"
            )
    if problems:
        return Check("broker-spec", "fail", "; ".join(problems), _hint_spec_file(ctx))
    return Check(
        "broker-spec",
        "ok",
        f"{spec.path} (provider={upstream['provider']}, model={upstream['model']}, "
        f"apiKeyEnv={upstream['apiKeyEnv']})",
    )


def _hint_spec_file(ctx: _Context, *, valid: bool = True) -> str:
    path = ctx.broker_json or Path("broker.json")
    if not valid:
        return f"python3 -m json.tool {path}   # reports the JSON error and its offset"
    return f"${{EDITOR:-vi}} {path}   # add the four upstream fields, then re-run: aeval doctor"


def _check_upstream_key(ctx: _Context, spec: _SpecState) -> Check:
    upstream = (spec.raw or {}).get("upstream")
    name = upstream.get("apiKeyEnv") if isinstance(upstream, dict) else None
    if not isinstance(name, str) or not name:
        return Check("upstream-key", "warn", "not checked — no upstream.apiKeyEnv in scope")
    value = ctx.env.get(name, "")
    if value.strip():
        return Check("upstream-key", "ok", f"{name} is exported on the host ({len(value)} chars)")
    return Check(
        "upstream-key",
        "fail",
        f"upstream.apiKeyEnv names {name}, which is not exported here — the broker starts "
        "on the host, so this later shows up inside the sandbox as MISSING_CREDENTIAL "
        "on an infra-invalid trial, not as a missing key",
        f"export {name}=<your key>",
    )


def _check_token_count(spec: _SpecState) -> Check:
    raw = spec.raw or {}
    limits = raw.get("limits")
    max_tokens = limits.get("maxTokens") if isinstance(limits, dict) else None
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        return Check(
            "token-count", "ok", "limits.maxTokens is not set — no trusted counter required"
        )
    token_count = raw.get("tokenCount")
    endpoint = token_count.get("endpoint") if isinstance(token_count, dict) else None
    if not isinstance(endpoint, str) or not endpoint.strip():
        return Check(
            "token-count",
            "fail",
            f"limits.maxTokens={max_tokens} requires a trusted tokenCount source and the "
            "spec declares no tokenCount.endpoint — the broker refuses to start "
            "(exit code 3)",
            "python3 suites/<suite>/tools/token_count.py --host 127.0.0.1 --port 8791 &  # "
            'then add "tokenCount": {"endpoint": "http://127.0.0.1:8791/tokens/count", '
            '"margin": 8} to the spec',
        )
    margin = token_count.get("margin")
    if not isinstance(margin, int) or isinstance(margin, bool):
        return Check(
            "token-count",
            "warn",
            f"tokenCount.endpoint is set but tokenCount.margin is missing for "
            f"limits.maxTokens={max_tokens}",
            f"python3 -c 'import json,sys;p=sys.argv[1];d=json.load(open(p));"
            "d[\"tokenCount\"][\"margin\"]=8;json.dump(d,open(p,\"w\"),indent=2)' "
            f"{_spec_path_text_suffix(spec)}",
        )
    return Check("token-count", "ok", f"tokenCount.endpoint={endpoint} (margin={margin})")


def _spec_path_text_suffix(spec: _SpecState) -> str:
    return str(spec.path) if spec.path else "$AEVAL_BROKER_JSON"


def _check_token_endpoint(ctx: _Context, spec: _SpecState) -> Check:
    raw = spec.raw or {}
    token_count = raw.get("tokenCount")
    endpoint = token_count.get("endpoint") if isinstance(token_count, dict) else None
    if not isinstance(endpoint, str) or not endpoint.strip():
        return Check("token-endpoint", "ok", "no tokenCount.endpoint declared — nothing to probe")
    if ctx.no_probe:
        return Check("token-endpoint", "warn", f"probe skipped (--no-probe): {endpoint}")
    result = ctx.probe(
        endpoint, method="POST", body=b'{"x":1}', timeout=ctx.probe_timeout
    )
    if result.reachable:
        return Check("token-endpoint", "ok", f"POST {endpoint} -> {result.detail}")
    return Check(
        "token-endpoint",
        "fail",
        f"POST {endpoint} unreachable — {result.detail}",
        _hint_start_counter(ctx, endpoint),
    )


def _hint_start_counter(ctx: _Context, endpoint: str) -> str:
    parts = urlsplit(endpoint)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 8791
    suite = Path(ctx.suite) if ctx.suite is not None else Path("suites/<suite>")
    return f"python3 {suite}/tools/token_count.py --host {host} --port {port} &"


def _is_loopback(host: str) -> bool:
    if host.strip().lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_listen_tls(ctx: _Context, spec: _SpecState) -> Check:
    if spec.path is None:
        return Check("listen-tls", "ok", "not in scope — no broker spec is configured")
    raw = spec.raw or {}
    host = raw.get("listenHost") or "127.0.0.1"
    if _is_loopback(str(host)):
        return Check(
            "listen-tls",
            "fail",
            f"listenHost={host} is loopback: inside a sandbox 127.0.0.1 is the sandbox "
            "itself, so the DSH session cannot reach the broker (ECONNREFUSED, then "
            '"the trial\'s agent exposes no DSH session")',
            _hint_listen_tls(),
        )
    tls = raw.get("listenTls")
    problems: list[str] = []
    cert = key = None
    if not isinstance(tls, dict) or not tls.get("cert") or not tls.get("key"):
        problems.append("listenTls needs {cert, key} (a non-loopback listen is TLS-only)")
    else:
        cert, key = str(tls["cert"]), str(tls["key"])
        for label, path in (("cert", cert), ("key", key)):
            if not Path(path).is_file():
                problems.append(f"listenTls.{label} is not a file: {path}")
            elif not os.access(path, os.R_OK):
                problems.append(f"listenTls.{label} is not readable: {path}")
    ca = raw.get("controlCa")
    if not isinstance(ca, str) or not ca:
        problems.append("controlCa is required so the sandbox can trust the broker")
    elif not Path(ca).is_file():
        problems.append(f"controlCa is not a file: {ca}")
    elif not os.access(ca, os.R_OK):
        problems.append(f"controlCa is not readable: {ca}")
    if problems:
        return Check(
            "listen-tls", "fail", "; ".join(problems), _hint_listen_tls()
        )
    return Check("listen-tls", "ok", f"listenHost={host} with TLS ({cert}) and CA ({ca})")


def _host_ip_assignment() -> str:
    """A copy-pasteable host-IP assignment for the platform in use.

    The sandbox has to reach this host, and the two supported operating systems
    expose the address differently — a Linux-only one-liner is not a fix on the
    macOS machines most of this plugin's users are on.
    """
    if sys.platform == "darwin":
        return "HOST_IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)"
    return "HOST_IP=$(hostname -I | awk '{print $1}')"


def _hint_listen_tls() -> str:
    return (
        _host_ip_assignment() + "; "
        'openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout ca.key -out ca.crt '
        '-subj "/CN=aeval-eval-ca"; '
        'openssl req -newkey rsa:2048 -nodes -keyout gateway.key -out gateway.csr '
        '-subj "/CN=$HOST_IP" -addext "subjectAltName=IP:$HOST_IP,DNS:localhost"; '
        "openssl x509 -req -in gateway.csr -CA ca.crt -CAkey ca.key -CAcreateserial "
        "-out gateway.crt -days 365 -copy_extensions copy; "
        "python3 -c 'import json,os,sys;h=sys.argv[1];p=os.environ[\"AEVAL_BROKER_JSON\"];"
        "d=json.load(open(p));d[\"listenHost\"]=h;"
        "d[\"listenTls\"]={\"cert\":os.path.abspath(\"gateway.crt\"),"
        "\"key\":os.path.abspath(\"gateway.key\")};"
        "d[\"controlCa\"]=os.path.abspath(\"ca.crt\");"
        "json.dump(d,open(p,\"w\"),indent=2)' \"$HOST_IP\""
    )


class _CertUnreadable(RuntimeError):
    """The certificate could not be decoded here (not a wiring failure)."""


def _certificate_sans(path: Path) -> list[str]:
    try:
        from cryptography import x509
    except ImportError as exc:                       # pragma: no cover - dependency present
        raise _CertUnreadable(f"cryptography is not importable: {exc}") from exc
    try:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except Exception as exc:                          # unreadable/der/absent extension
        raise _CertUnreadable(f"{path}: {type(exc).__name__}: {exc}") from exc
    names: list[str] = []
    for name in extension:
        if isinstance(name, x509.IPAddress):
            names.append(str(name.value))
        elif isinstance(name, x509.DNSName):
            names.append(name.value)
        else:
            names.append(f"{name.__class__.__name__}:{name.value}")
    return names


def _san_matches(host: str, sans: Sequence[str]) -> bool:
    try:
        wanted = ipaddress.ip_address(host)
    except ValueError:
        lowered = host.lower()
        for name in sans:
            if name.lower() == lowered:
                return True
            if name.startswith("*.") and lowered.endswith(name[1:].lower()):
                return True
        return False
    for name in sans:
        try:
            if ipaddress.ip_address(name) == wanted:
                return True
        except ValueError:
            continue
    return False


def _hint_regenerate_cert(host: str) -> str:
    return (
        f"openssl req -newkey rsa:2048 -nodes -keyout gateway.key -out gateway.csr "
        f'-subj "/CN={host}" -addext "subjectAltName=IP:{host},DNS:localhost" && '
        "openssl x509 -req -in gateway.csr -CA ca.crt -CAkey ca.key -CAcreateserial "
        "-out gateway.crt -days 365 -copy_extensions copy  # then point listenTls.cert at it"
    )


def _check_cert_san(ctx: _Context, spec: _SpecState) -> Check:
    if spec.path is None:
        return Check("cert-san", "ok", "not in scope — no broker spec is configured")
    raw = spec.raw or {}
    tls = raw.get("listenTls")
    cert = tls.get("cert") if isinstance(tls, dict) else None
    if not isinstance(cert, str) or not cert:
        return Check("cert-san", "ok", "not checked — no listenTls.cert (see listen-tls)")
    host = str(raw.get("listenHost") or "127.0.0.1")
    try:
        sans = _certificate_sans(Path(cert))
    except _CertUnreadable as exc:
        return Check("cert-san", "warn", f"could not inspect {cert}: {exc}")
    if _san_matches(host, sans):
        return Check("cert-san", "ok", f"SAN covers listenHost={host} ({', '.join(sans)})")
    return Check(
        "cert-san",
        "fail",
        f"{cert} SAN [{', '.join(sans) or 'none'}] does not contain listenHost={host} — "
        "the sandbox's fetch will reject the broker's certificate",
        _hint_regenerate_cert(host),
    )


def _check_agents_declaration(ctx: _Context) -> Check:
    from aeval.agents.declaration import discover_agent_declarations

    try:
        ids = sorted(discover_agent_declarations(Path(ctx.agents_dir)))
    except OSError as exc:
        return Check("agents-declaration", "fail", f"{ctx.agents_dir}: {exc}")
    if not ids:
        return Check(
            "agents-declaration",
            "fail",
            f"no agent declarations under {ctx.agents_dir}",
            f"aeval agents --init --agents-dir {ctx.agents_dir}",
        )
    shown = ", ".join(ids[:8]) + ("…" if len(ids) > 8 else "")
    return Check(
        "agents-declaration", "ok", f"{len(ids)} declaration(s) under {ctx.agents_dir}: {shown}"
    )


def _check_suite_provenance(ctx: _Context) -> Check:
    if ctx.suite is None:
        return Check(
            "suite-provenance",
            "ok",
            "not in scope — pass --suite to check its source provenance",
        )
    from aeval.suite_loader.composition import suite_source_commit
    from aeval.suite_models import SuiteError

    suite = Path(ctx.suite)
    if not (suite / "suite.yaml").is_file():
        return Check(
            "suite-provenance",
            "fail",
            f"no suite.yaml under {suite}",
            f"ls {suite}   # --suite points at the directory holding suite.yaml",
        )
    try:
        commit = suite_source_commit(suite)
    except SuiteError as exc:
        return Check(
            "suite-provenance",
            "fail",
            str(exc),
            f"git -C {suite} add -A && git -C {suite} commit -m 'pin the suite source'",
        )
    except OSError as exc:
        return Check("suite-provenance", "fail", f"{suite}: {exc}")
    return Check("suite-provenance", "ok", f"{suite} at {commit[:12]} (clean)")


def _selected_backend(ctx: _Context, pairing: _PairingState) -> str | None:
    if ctx.backend:
        return ctx.backend.strip().lower()
    job = pairing.job
    environment = getattr(job, "environment", None)
    raw_type = getattr(environment, "type", None)
    if raw_type is None:
        return None
    return str(getattr(raw_type, "value", raw_type)).strip().lower()


def _check_backend(ctx: _Context, pairing: _PairingState) -> Check:
    backend = _selected_backend(ctx, pairing)
    if backend is None:
        return Check(
            "backend",
            "warn",
            "no sandbox backend selected — pass --backend docker|e2b, or --suite/--agent "
            "so it is read from the job",
            "aeval doctor --backend docker   # or: e2b",
        )
    if backend == "docker":
        if ctx.no_probe:
            # --no-probe means no reachability probe at all, the daemon
            # included: the report must not depend on a contact it was told
            # not to make.
            return Check(
                "backend",
                "warn",
                "probe skipped (--no-probe): the docker daemon was not contacted",
                "aeval doctor --backend docker   # without --no-probe",
            )
        result = ctx.command(
            ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=ctx.probe_timeout
        )
        if result.returncode != 0:
            detail = _tail(result.stderr) or f"docker info exited {result.returncode}"
            return Check(
                "backend",
                "fail",
                f"docker daemon unreachable: {detail}",
                "docker info   # start the daemon (or fix DOCKER_HOST) and re-run",
            )
        return Check(
            "backend",
            "ok",
            f"docker daemon {result.stdout.strip() or 'reachable'}",
        )
    if backend == "e2b":
        from aeval.provenance import BackendNotAvailableError, verify_e2b_backend

        try:
            version = verify_e2b_backend()
        except BackendNotAvailableError as exc:
            return Check(
                "backend",
                "fail",
                str(exc),
                'uv pip install "harbor[e2b]==0.23.0"   # or: pip install "harbor[e2b]"',
            )
        if not ctx.env.get("E2B_API_KEY", "").strip():
            return Check(
                "backend",
                "fail",
                "E2B_API_KEY is not exported — a managed sandbox cannot be created",
                "export E2B_API_KEY=<your key>",
            )
        endpoint = ctx.env.get("E2B_API_URL", "").strip()
        if endpoint and ctx.no_probe:
            return Check(
                "backend",
                "warn",
                f"probe skipped (--no-probe): e2b SDK {version} and E2B_API_KEY are fine, "
                f"endpoint {endpoint} was not contacted",
                "aeval doctor --backend e2b   # without --no-probe",
            )
        if endpoint:
            result = ctx.probe(endpoint, timeout=ctx.probe_timeout)
            if not result.reachable:
                return Check(
                    "backend",
                    "fail",
                    f"E2B_API_URL={endpoint} unreachable — {result.detail}",
                    "curl -sI " + endpoint + "   # check the endpoint and its credentials",
                )
        detail = f"e2b SDK {version}; E2B_API_KEY exported"
        if endpoint:
            detail += f"; endpoint {endpoint}"
        return Check("backend", "ok", detail)
    return Check(
        "backend",
        "fail",
        f"unknown backend {backend!r} (expected docker or e2b)",
        "aeval doctor --backend docker   # or: e2b",
    )


def _check_pairing(ctx: _Context, pairing: _PairingState) -> Check:
    if ctx.suite is None or not ctx.agent:
        return Check(
            "pairing",
            "ok",
            "not in scope — pass --suite and --agent (same inputs as `aeval check`)",
        )
    if pairing.error:
        return Check(
            "pairing",
            "fail",
            pairing.error,
            f"aeval check --suite {ctx.suite} --agent {ctx.agent} "
            f"--agents-dir {ctx.agents_dir}",
        )
    job = pairing.job
    return Check(
        "pairing",
        "ok",
        f"{ctx.agent} × {pairing.resolved.id}: composed "
        f"(impl={job.agents[0].import_path}, attempts={job.n_attempts})",
    )


def _check_runtime_image(ctx: _Context, pairing: _PairingState) -> Check:
    if pairing.job is None:
        return Check(
            "runtime-image",
            "ok",
            "not in scope — needs a composed pairing (--suite and --agent)",
        )
    # Exactly what `aeval check` does: the run's own declaration→runtime
    # selection, then the table lookup.
    from aeval.agents.runtime import (
        default_runtime_images_path,
        image_problem_for,
        load_runtime_images,
    )
    from aeval.cli import _selected_runtime_keys

    table = load_runtime_images()
    picks, unresolved = _selected_runtime_keys(pairing.job, agents_root=Path(ctx.agents_dir))
    if not picks:
        note = f"; {len(unresolved)} adapter(s) have no declaration" if unresolved else ""
        return Check("runtime-image", "ok", f"no runtime declared — nothing to host{note}")
    problems: list[str] = []
    for agent_id, runtime in picks:
        problem = image_problem_for(
            table,
            suite_id=pairing.resolved.id,
            runtime=runtime,
            image=None,
            platform=None,
            table_path=default_runtime_images_path(),
        )
        if problem:
            problems.append(f"agent {agent_id!r}: {problem}")
    if problems:
        return Check(
            "runtime-image",
            "fail",
            "; ".join(problems),
            f"aeval check --suite {ctx.suite} --agent {ctx.agent} "
            f"--agents-dir {ctx.agents_dir}   # prints the mapped image to pin",
        )
    runtime_keys = ", ".join(runtime.key for _, runtime in picks)
    return Check("runtime-image", "ok", f"runtime(s) hosted: {runtime_keys}")


def _check_evidence_trials(ctx: _Context) -> Check:
    if ctx.run_dir is None:
        return Check(
            "evidence-trials",
            "ok",
            "not in scope — pass --run-dir to check the report's trial evidence",
        )
    run = Path(ctx.run_dir)
    if not run.is_dir():
        return Check(
            "evidence-trials", "fail", f"{run} does not exist", f"ls -la {run}"
        )
    # The report reads every layout aeval publishes trial-dir links in
    # (``cli._trial_dir_candidates``): ``<run>/trials``, ``<run.parent>/runs/
    # trials`` (what ``aeval quickstart`` writes) and ``<run.parent>/trials``.
    # This check has to agree with that reader — otherwise it would report a
    # coverage problem the report does not actually have, or send an operator
    # to populate a directory the report never looks at.
    candidates = (
        run / "trials",
        run.parent / "runs" / "trials",
        run.parent / "trials",
    )
    trials = candidates[0]
    linked = 0
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        count = sum(
            1
            for entry in sorted(candidate.iterdir())
            if (entry / "collection_manifest.json").is_file()
        )
        if count:
            trials, linked = candidate, count
            break
    if linked:
        return Check("evidence-trials", "ok", f"{linked} trial dir(s) with a manifest under {trials}")
    return Check(
        "evidence-trials",
        "fail",
        f"no trial evidence under {run}/trials (nor {run.parent}/runs/trials) — "
        "the report will show 0/N evidence coverage "
        "even though the trials ran",
        f'mkdir -p {run}/trials && for t in {run}/harbor/*/*/; do '
        f'[ -f "$t/collection_manifest.json" ] && ln -sfn "$PWD/$t" '
        f'"{run}/trials/$(basename "$t")"; done',
    )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def run_doctor(
    *,
    broker_json: str | Path | None = None,
    suite: str | Path | None = None,
    agent: str | None = None,
    profile: str | None = None,
    agents_dir: str | Path = "agents",
    backend: str | None = None,
    run_dir: str | Path | None = None,
    node_bin: str = "node",
    no_probe: bool = False,
    probe_timeout: float = 5.0,
    env: Mapping[str, str] | None = None,
    probe: Callable[..., ProbeResult] | None = None,
    command: Callable[..., CommandResult] | None = None,
) -> DoctorReport:
    """Run every check and return the report (read-only, never raises for a bad env).

    ``env``/``probe``/``command`` are the injection points: the tests drive
    every branch without a network, a docker daemon or a real DSH home.
    """
    resolved_env: Mapping[str, str] = os.environ if env is None else env
    spec_path = broker_json
    if spec_path is None:
        override = resolved_env.get(_plugins_env(), "").strip()
        spec_path = Path(override) if override else None
    ctx = _Context(
        broker_json=Path(spec_path) if spec_path is not None else None,
        suite=Path(suite) if suite is not None else None,
        agent=agent,
        profile=profile,
        agents_dir=Path(agents_dir),
        backend=backend,
        run_dir=Path(run_dir) if run_dir is not None else None,
        node_bin=node_bin,
        no_probe=no_probe,
        probe_timeout=probe_timeout,
        env=resolved_env,
        probe=probe or _http_probe,
        command=command or _run_command,
    )
    ctx.spec = _read_spec(ctx)
    # The adapter-owned facts reach core through the declared control stacks
    # (flavor registry), never through an adapter import.
    ctx.control_facts = collect_control_doctor_facts(
        spec=ctx.spec.raw, env=ctx.env, agents_dir=ctx.agents_dir
    )
    ctx.pairing = _pairing_scope(ctx)

    checks = (
        _check_node(ctx, ctx.spec),
        _control_check(ctx.control_facts, "control-dist", ctx),
        _control_check(ctx.control_facts, "control-deps", ctx),
        _check_broker_spec(ctx, ctx.spec),
        _check_upstream_key(ctx, ctx.spec),
        _check_token_count(ctx.spec),
        _check_token_endpoint(ctx, ctx.spec),
        _check_listen_tls(ctx, ctx.spec),
        _check_cert_san(ctx, ctx.spec),
        _control_check(ctx.control_facts, "dsh-profile", ctx),
        _check_agents_declaration(ctx),
        _check_suite_provenance(ctx),
        _check_backend(ctx, ctx.pairing),
        _check_pairing(ctx, ctx.pairing),
        _check_runtime_image(ctx, ctx.pairing),
        _check_evidence_trials(ctx),
    )
    mode = "active" if ctx.broker_json is not None else "standalone"
    return DoctorReport(mode=mode, checks=checks)
