"""One command from a fresh install to a scored report.

``aeval quickstart`` is the framework's whole wiring chain — certificates, the
broker spec, the trusted token counter, the run, the evidence links, the
report — behind one command a person can type without reading the runbook
first. Every stage the runbook documents stays an individual command anyone can
read in the plan: this module composes them, it does not replace them.

Three properties are contracts, not conveniences:

* **Nothing is written unless asked.** ``--dry-run`` prints the plan, and
  running without ``--yes`` prints the same plan; neither creates the workspace
  directory itself, because "planned" and "partly done" must be distinguishable
  by looking at the disk.
* **A run that scored nothing is a failure.** Harbor exits 0 when every trial
  was excluded, and a run whose trials were all excluded measured nothing. The
  success criterion is therefore the valid-trial count from aeval's own
  reliability metric, never the subprocess exit code.
* **A stage that is a no-op says so.** A suite already pinned to the selected
  backend is used as it is; a complete run already in the workspace is
  re-reported instead of re-run.
* **The broker binds an address the sandbox is allowed to dial.** When the
  suite's tasks declare an egress allowlist, that list decides the address (and
  a host with no allowed address is refused before anything is written): the
  sandbox's own ``127.0.0.1`` is the sandbox, and an address outside its
  allowlist fails inside the agent as an unreachable broker, which reads like a
  broken install. ``--listen-host`` overrides the choice, never silently.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "EXIT_NO_VALID_TRIALS",
    "QUICKSTART_SCHEMA",
    "DEFAULT_INSTALL_PREFIX",
    "INSTALL_PREFIX_ENV",
    "Step",
    "QuickstartPlan",
    "QuickstartResult",
    "QuickstartError",
    "plan_quickstart",
    "run_quickstart",
    "scored_counts",
    "choose_listen_address",
    "local_addresses",
]

#: A completed run whose trials were all excluded exits with this code. It is
#: deliberately distinct from the wiring-failure codes: the wiring was fine and
#: the experiment still measured nothing.
EXIT_NO_VALID_TRIALS = 6

QUICKSTART_SCHEMA = "aeval-quickstart/1"

#: Inside the sandbox the CLI tree is installed here. The value is a CONTAINER
#: path (not a host path): ``/dev/shm`` is a 64 MiB tmpfs under docker and a
#: ~500 MB CLI tree does not fit in it, while ``/root`` is a writable part of
#: the task image. ``--install-prefix`` / ``AEVAL_QUICKSTART_INSTALL_PREFIX``
#: override it for images that keep their package elsewhere.
DEFAULT_INSTALL_PREFIX = "/root/dshpkg"
INSTALL_PREFIX_ENV = "AEVAL_QUICKSTART_INSTALL_PREFIX"
HARBOR_CLI_ENV = "AEVAL_QUICKSTART_HARBOR_CLI"
CONTROL_ROOT_ENV = "AEVAL_QUICKSTART_CONTROL_ROOT"
RUNTIME_IMAGES_ENV = "AEVAL_RUNTIME_IMAGES"

DEFAULT_LISTEN_PORT = 8447
DEFAULT_TOKEN_COUNT_PORT = 8792
TOKEN_COUNT_PATH = "/tokens/count"

#: The upstream the quickstart pins: a name, a base URL and the NAME of the
#: credential variable. No credential value is ever written into the workspace.
UPSTREAM = {
    "provider": "deepseek",
    "baseUrl": "https://api.deepseek.com/v1",
    "apiKeyEnv": "DEEPSEEK_API_KEY",
    "model": "deepseek-chat",
}
LIMITS = {"maxSteps": 60, "maxTokens": 2000000}
MAX_OUTPUT_TOKENS = 16384
TOKEN_COUNT_MARGIN = 8

BACKENDS = ("docker", "e2b")


class QuickstartError(RuntimeError):
    """The quickstart cannot proceed; the message names what is missing."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _exec(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> Completed:
    """Run one child process (or report that it could not be started)."""
    try:
        result = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=None if env is None else {**os.environ, **env},
            capture_output=capture,
            text=True,
        )
    except OSError as exc:
        return Completed(127, "", str(exc))
    return Completed(
        result.returncode,
        result.stdout if capture else "",
        result.stderr if capture else "",
    )


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Step:
    """One stage of the quickstart.

    ``dry`` says what the stage writes (or what command it runs), so the plan
    is readable — and reviewable — without executing any of it.
    """

    id: str
    title: str
    dry: str
    apply: Callable[[], None]


@dataclass
class _State:
    """What the stages hand to each other, and to the final verdict."""

    runner: Callable[..., Completed]
    env: Mapping[str, str]
    agents_dir: Path = Path("agents")
    address: str | None = None
    tls: Mapping[str, Path] = field(default_factory=dict)
    counter: Any = None
    suite: Path | None = None
    run_dir: Path | None = None
    store: Path | None = None
    run_id: str | None = None
    valid: int | None = None
    total: int | None = None


@dataclass
class QuickstartPlan:
    backend: str
    workspace: Path
    suite: Path
    agents_dir: Path
    repo_root: Path
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    state: _State | None = None

    def render(self) -> str:
        lines = [
            f"quickstart plan — backend={self.backend}",
            f"  workspace  {self.workspace}",
            f"  suite      {self.suite}",
            f"  agents-dir {self.agents_dir}",
            f"  repo root  {self.repo_root}",
        ]
        lines += [f"  note: {note}" for note in self.notes]
        lines.append("steps:")
        for index, step in enumerate(self.steps, start=1):
            lines.append(f"  {index:2d}. {step.id:<15} {step.title}")
            lines.append(f"      {step.dry}")
        return "\n".join(lines)


@dataclass
class QuickstartResult:
    plan: QuickstartPlan
    exit_code: int
    valid: int | None = None
    total: int | None = None
    report_path: Path | None = None
    steps_run: list[str] = field(default_factory=list)

    def scored_line(self) -> str | None:
        if self.valid is None or self.total is None:
            return None
        return f"{self.valid}/{self.total} trial(s) scored"


# --------------------------------------------------------------------------- #
# anchors: the repo root, the suite, the declarations
# --------------------------------------------------------------------------- #


def _looks_like_repo(root: Path) -> bool:
    return (root / "agents").is_dir() and (root / "suites").is_dir()


def resolve_repo_root(
    *, agents_dir: Path, suite: Path | None, cwd: Path | None = None
) -> Path:
    """The checkout the run must stand in.

    ``agents/_runtime/images.yaml`` is resolved relative to the process's
    working directory (and a suite's ``extends`` against its own parent), so a
    command that inherited the caller's cwd works from one directory and fails
    from the next. Resolving the root explicitly — and running every child
    there — is what makes the quickstart position-independent.
    """
    here = (cwd or Path.cwd()).resolve()
    candidates: list[Path] = []
    if suite is not None:
        resolved = suite.resolve()
        candidates += [resolved, resolved.parent]
    candidates.append(agents_dir.resolve().parent)
    candidates += [here, *here.parents]
    package = Path(__file__).resolve()
    candidates += [package.parent.parent.parent, package.parent.parent, package.parent]
    for candidate in candidates:
        if _looks_like_repo(candidate):
            return candidate
    return here


def _load_yaml(path: Path) -> Any:
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QuickstartError(f"{path} is unreadable: {exc}") from exc


def _job_environment(suite: Path) -> tuple[str | None, list[Path]]:
    """``(environment.type, job files)`` for the suite's declared jobs."""
    manifest = _load_yaml(suite / "suite.yaml") or {}
    harbor = manifest.get("harbor") or {}
    declared = harbor.get("job")
    jobs: list[Path] = []
    if isinstance(declared, str) and declared:
        jobs.append(suite / declared)
    jobs_dir = suite / "jobs"
    if jobs_dir.is_dir():
        jobs += sorted(jobs_dir.glob("*.yaml"))
    found: list[Path] = []
    for job in jobs:
        if job.is_file() and job not in found:
            found.append(job)
    kinds: set[str] = set()
    for job in found:
        data = _load_yaml(job) or {}
        environment = data.get("environment") or {}
        if isinstance(environment.get("type"), str):
            kinds.add(environment["type"])
    if len(kinds) > 1:
        raise QuickstartError(
            f"{suite} pins more than one environment type ({sorted(kinds)}) — "
            "a quickstart runs one backend"
        )
    return (kinds.pop() if kinds else None), found


def _task_network_modes(suite: Path) -> list[str]:
    modes: list[str] = []
    tasks = suite / "tasks"
    if not tasks.is_dir():
        return modes
    for task_toml in sorted(tasks.glob("*/task.toml")):
        try:
            data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The vendored tasks state it under [environment]; the top level is
        # accepted too, because a hand-written task may put it there.
        environment = data.get("environment")
        mode = data.get("network_mode")
        if mode is None and isinstance(environment, dict):
            mode = environment.get("network_mode")
        if isinstance(mode, str):
            modes.append(mode)
    return modes


def _task_allowed_hosts(suite: Path) -> list[str]:
    """The egress allowlist the suite's tasks declare (union, declaration order).

    With ``network_mode = "allowlist"`` the sandbox may dial exactly these
    hosts. That makes the broker's address a property of the SUITE, not of the
    host: binding an address the sandbox is not allowed to dial fails inside
    the sandbox as a TLS/transport error, which reads like a broken broker.
    """
    hosts: list[str] = []
    tasks = suite / "tasks"
    if not tasks.is_dir():
        return hosts
    for task_toml in sorted(tasks.glob("*/task.toml")):
        try:
            data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        environment = data.get("environment")
        declared = environment.get("allowed_hosts") if isinstance(environment, dict) else None
        if not isinstance(declared, list):
            continue
        for host in declared:
            if isinstance(host, str) and host and host not in hosts:
                hosts.append(host)
    return hosts


def _docker_refusal(suite: Path) -> str | None:
    """Why this suite cannot run on the local docker backend, if it cannot."""
    _, jobs = _job_environment(suite)
    if not jobs:
        return f"{suite} declares no job file"
    if "no-network" in set(_task_network_modes(suite)):
        return (
            f"{suite} pins network_mode=no-network, which the local docker backend "
            "cannot enforce (its own job comment records that the run aborts) — pick a "
            "suite whose tasks declare network_mode=allowlist"
        )
    return None


def default_suite(repo_root: Path, backend: str) -> Path:
    """The built-in suite for a backend, when the caller names none.

    A silent pick would be worse than an error: the choice is printed, and the
    refusal is copy-pasteable when nothing qualifies.
    """
    suites = repo_root / "suites"
    if not suites.is_dir():
        raise QuickstartError(
            f"no suites under {repo_root} — pass --suite <dir>",
            "aeval quickstart --suite suites/<id>",
        )
    usable = [
        candidate
        for candidate in sorted(p for p in suites.iterdir() if (p / "suite.yaml").is_file())
        if not (backend == "docker" and _docker_refusal(candidate) is not None)
    ]
    if not usable:
        raise QuickstartError(
            f"no suite under {suites} can run on the {backend} backend",
            "aeval quickstart --suite <dir>",
        )
    # Prefer the pilot: its docker arm is the one verified end to end, and it is
    # recognised by its declared shape (an allowlist task set with a real agent
    # entry), not by a hardcoded id.
    for candidate in usable:
        if candidate.name.startswith("tbench-pilot"):
            return candidate
    return usable[0]


def _agent_declarations(agents_dir: Path) -> list[str]:
    if not agents_dir.is_dir():
        return []
    return sorted(p.stem for p in agents_dir.glob("*.yaml") if not p.name.startswith("_"))


# --------------------------------------------------------------------------- #
# host address, TLS material, token counter
# --------------------------------------------------------------------------- #


def local_addresses(
    *, platform: str | None = None, runner: Callable[..., Completed] | None = None
) -> list[str]:
    """Every non-loopback address of this host, in the platform's own order.

    A sandbox's ``127.0.0.1`` is the sandbox itself, so the broker has to bind
    an address the sandbox can route to. The two supported platforms expose
    their addresses differently; a Linux-only one-liner is not a fix for this
    plugin's macOS users.
    """
    platform = platform or sys.platform
    run = runner or _exec
    if platform == "darwin":
        raw: list[str] = []
        for iface in ("en0", "en1", "en2", "en3"):
            result = run(["ipconfig", "getifaddr", iface], cwd=None, env=None, capture=True)
            if result.returncode == 0 and (result.stdout or "").strip():
                raw.append(result.stdout.strip())
    else:
        result = run(["hostname", "-I"], cwd=None, env=None, capture=True)
        raw = (result.stdout or "").split() if result.returncode == 0 else []
    addresses: list[str] = []
    for address in raw:
        if address and address not in addresses and not address.startswith("127."):
            addresses.append(address)
    return addresses


def host_ip(
    *, platform: str | None = None, runner: Callable[..., Completed] | None = None
) -> str:
    """The first address the sandbox could dial (see ``choose_listen_address``)."""
    addresses = local_addresses(platform=platform, runner=runner)
    if addresses:
        return addresses[0]
    raise QuickstartError(
        "could not determine this host's address for the sandbox to dial",
        "assign a routable address to an interface, or run the broker on a reachable host",
    )


def choose_listen_address(
    *,
    allowed: Sequence[str] = (),
    platform: str | None = None,
    runner: Callable[..., Completed] | None = None,
    requested: str | None = None,
) -> tuple[str, str | None]:
    """``(address the broker binds, why)`` — the sandbox has to be able to dial it.

    When the suite's tasks declare an egress allowlist, the sandbox may reach
    exactly those hosts. Binding anything else is not a networking detail: the
    agent dies inside the sandbox with an unreachable broker, which looks like a
    broken install. So the allowlist decides, and a host with no allowed
    address is refused here rather than half-executed.
    """
    if requested:
        return requested, None
    addresses = local_addresses(platform=platform, runner=runner)
    if not addresses:
        raise QuickstartError(
            "could not determine this host's address for the sandbox to dial",
            "aeval quickstart --listen-host <the address the sandbox can reach>",
        )
    if allowed:
        match = next((address for address in addresses if address in allowed), None)
        if match is None:
            raise QuickstartError(
                "the suite's tasks allow the sandbox to dial "
                f"{', '.join(allowed)}, and none of this host's addresses "
                f"({', '.join(addresses)}) is among them — the sandbox could not "
                "reach the broker",
                "aeval quickstart --listen-host <one of the suite's allowed hosts>",
            )
        return match, (
            f"the suite lets the sandbox dial {', '.join(allowed)}; "
            f"the broker binds {match} so that route exists"
        )
    return addresses[0], None


def _write_tls_material(workspace: Path, address: str) -> dict[str, Path]:
    """A self-signed CA plus a gateway certificate whose SAN covers ``address``.

    The sandbox verifies the broker's certificate against the CA it is handed,
    so a certificate without the address in its SAN is rejected at the first
    request — a connection error far from its cause.
    """
    import datetime as _datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    tls = workspace / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    now = _datetime.datetime.now(_datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aeval-quickstart-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _datetime.timedelta(minutes=5))
        .not_valid_after(now + _datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    gateway_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans: list[x509.GeneralName] = []
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(address)))
    except ValueError:
        sans.append(x509.DNSName(address))
    sans += [x509.DNSName("localhost"),
             x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    gateway_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address)]))
        .issuer_name(ca_name).public_key(gateway_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _datetime.timedelta(minutes=5))
        .not_valid_after(now + _datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    paths = {
        "ca_cert": tls / "ca.crt",
        "ca_key": tls / "ca.key",
        "gateway_cert": tls / "gateway.crt",
        "gateway_key": tls / "gateway.key",
    }
    paths["ca_cert"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["ca_key"].write_bytes(
        ca_key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    )
    paths["gateway_cert"].write_bytes(gateway_cert.public_bytes(serialization.Encoding.PEM))
    paths["gateway_key"].write_bytes(
        gateway_key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    )
    return paths


class TokenCounter:
    """The trusted input-token counter the broker binds every request to.

    The bound must never UNDERCOUNT: the broker compares what this endpoint
    answered with the usage the upstream reports for the same request, and a
    bound that is too small fails the request closed (``AEVAL_TOKEN_BOUND_VIOLATED``).
    One token cannot encode less than one byte, so the byte length of the exact
    wire body is a conservative, deterministic bound needing no tokenizer.
    """

    def __init__(self, httpd: Any, thread: Any, port: int, requested: int) -> None:
        self._httpd = httpd
        self._thread = thread
        self.port = port
        #: What the caller asked for, when the host had to answer on another
        #: port. Reported, never hidden: the spec carries the bound port, so a
        #: substitution is a fact the operator should see once.
        self.requested = requested

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}{TOKEN_COUNT_PATH}"

    @classmethod
    def start(cls, port: int = DEFAULT_TOKEN_COUNT_PORT) -> "TokenCounter":
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:       # keep the log clean
                pass

            def _reply(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:                           # noqa: N802
                if not self.path.endswith(TOKEN_COUNT_PATH):
                    self._reply(404, {"error": "the counter serves " + TOKEN_COUNT_PATH})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                self._reply(200, {"inputTokens": max(1, len(body))})

        bound = port
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            # A fixed port is a convenience, not the contract: the broker spec
            # names the port that ended up bound. Something else holding the
            # default (a counter from an earlier session, most often) must not
            # stop a one-command run — but it is said out loud.
            if port == 0:
                raise QuickstartError(
                    f"cannot serve the token counter on 127.0.0.1: {exc}",
                    "aeval quickstart --token-count-port 0",
                ) from exc
            bound = 0
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            except OSError as retry:
                raise QuickstartError(
                    f"cannot serve the token counter: {retry}",
                    "aeval doctor",
                ) from retry
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return cls(httpd, thread, int(httpd.server_address[1]), port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --------------------------------------------------------------------------- #
# the broker spec
# --------------------------------------------------------------------------- #


def _suite_budgets(suite: Path) -> dict[str, Any]:
    """The run's budget, declared once by the suite when it declares one.

    The broker's ``limits`` and the trajectory grader's thresholds must be the
    same numbers, so a suite that ships ``budgets.yaml`` decides both.
    """
    path = suite / "budgets.yaml"
    if not path.is_file():
        return {}
    data = _load_yaml(path) or {}
    broker = data.get("broker")
    if not isinstance(broker, dict):
        return {}
    limits = dict(LIMITS)
    for key in ("maxSteps", "maxTokens"):
        if isinstance(broker.get(key), int):
            limits[key] = broker[key]
    max_output = broker.get("maxOutputTokens")
    return {
        "limits": limits,
        "maxOutputTokens": max_output if isinstance(max_output, int) else MAX_OUTPUT_TOKENS,
        "auxiliaryPolicy": broker.get("auxiliaryPolicy"),
    }


def build_broker_spec(
    *,
    address: str,
    token_count_endpoint: str,
    tls: Mapping[str, Path],
    suite: Path,
    control_root: Path | None,
    listen_port: int = DEFAULT_LISTEN_PORT,
) -> dict[str, Any]:
    """The operator's broker spec for the run this quickstart wires."""
    budgets = _suite_budgets(suite)
    spec: dict[str, Any] = {
        "listenHost": address,
        "listenPort": listen_port,
        "listenTls": {
            "cert": str(tls["gateway_cert"]),
            "key": str(tls["gateway_key"]),
        },
        "controlCa": str(tls["ca_cert"]),
        "upstream": dict(UPSTREAM),
        "identity": {"provider": UPSTREAM["provider"], "model": UPSTREAM["model"]},
        "limits": budgets.get("limits", dict(LIMITS)),
        "maxOutputTokens": budgets.get("maxOutputTokens", MAX_OUTPUT_TOKENS),
        "tokenCount": {"endpoint": token_count_endpoint, "margin": TOKEN_COUNT_MARGIN},
    }
    if control_root is not None:
        spec["controlRoot"] = str(control_root)
        spec["controlDist"] = str(Path(control_root) / "dist")
    policy = budgets.get("auxiliaryPolicy")
    if isinstance(policy, dict):
        spec["auxiliaryPolicy"] = policy
    return spec


# --------------------------------------------------------------------------- #
# the docker suite: a sibling conversion when the suite is not built for docker
# --------------------------------------------------------------------------- #


def suite_tree_root(workspace: Path) -> Path:
    """The self-contained suites tree a conversion is written into.

    Inside the workspace, never next to the user's checkout: the copy has to be
    committed to a git repository of its own (a run refuses a suite that is not
    a committed source), and a derived tree left in someone's repository is both
    untracked and in their way. The directory is named ``suites`` because the
    suite loader resolves ``extends`` against the nearest ancestor called
    ``suites`` — that is what keeps ``_base`` a sibling.
    """
    return workspace / "suites"


def docker_suite_dir(suite: Path, workspace: Path) -> Path:
    return suite_tree_root(workspace) / f"{suite.name}-docker"


def _copy_extended_bases(suite: Path, target_root: Path) -> list[Path]:
    """Mirror every base file the suite inherits, preserving its relative path."""
    from aeval.suite_loader.inheritance import default_suites_root
    from aeval.suite_loader.loader import load_suite

    resolved = load_suite(suite)
    source_root = default_suites_root(suite)
    copied: list[Path] = []
    for source in resolved.sources:
        if source.role != "base":
            continue
        origin = source_root / source.path
        if not origin.is_file():
            raise QuickstartError(
                f"{suite} extends {source.path!r}, which is not at {origin}",
                "aeval quickstart --suite <a suite whose bases are next to it>",
            )
        destination = target_root / source.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
        copied.append(destination)
    return copied


def _commit_tree(root: Path, message: str) -> None:
    """Commit a tree, because a run refuses a suite that is not a git source.

    The commit must come AFTER every patch: the provenance check rejects a tree
    with anything uncommitted, untracked or ignored, and ``-f`` is required
    because a tracked-ignored file still counts as "absent from its source
    commit".
    """
    def git(*argv: str) -> Completed:
        return _exec(
            ("git", "-C", str(root), "-c", "user.email=quickstart@aeval",
             "-c", "user.name=aeval-quickstart", "-c", "commit.gpgsign=false", *argv),
            capture=True,
        )

    if not (root / ".git").is_dir():
        initialised = _exec(("git", "init", "-q", str(root)), capture=True)
        if initialised.returncode != 0:
            raise QuickstartError(
                f"git init failed in {root}: {initialised.stderr.strip()}",
                "git --version",
            )
    added = git("add", "-A", "-f")
    if added.returncode != 0:
        raise QuickstartError(f"git add failed in {root}: {added.stderr.strip()}", "git status")
    committed = git("commit", "-q", "-m", message)
    if committed.returncode != 0 and "nothing to commit" not in (committed.stdout or ""):
        raise QuickstartError(
            f"git commit failed in {root}: {committed.stderr.strip() or committed.stdout.strip()}",
            f"git -C {root} status",
        )


def convert_suite_for_docker(suite: Path, install_prefix: str, *, workspace: Path) -> Path:
    """Write a docker-pinned copy of ``suite``, committed, under the workspace.

    Three changes, each one a failure met in the field:

    * ``environment.type=docker`` — the suite was pinned to a managed sandbox;
    * ``n_attempts=1`` — a smoke run, not the pilot's full trial count;
    * ``install_prefix`` off ``/dev/shm`` — a ~500 MB CLI tree does not fit the
      64 MiB tmpfs docker gives ``/dev/shm``.

    An existing conversion is reused as it is, never re-derived over what may be
    a hand-edited copy.
    """
    import yaml

    target = docker_suite_dir(suite, workspace)
    if target.exists():
        return target
    target_root = suite_tree_root(workspace)
    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(suite, target)
    try:
        _copy_extended_bases(suite, target_root)
        for job in sorted((target / "jobs").glob("*.yaml")):
            data = yaml.safe_load(job.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            environment = data.setdefault("environment", {})
            if not isinstance(environment, dict):
                raise QuickstartError(f"{job} declares a non-mapping environment")
            environment["type"] = "docker"
            data["n_attempts"] = 1
            for entry in data.get("agents") or []:
                if not isinstance(entry, dict):
                    continue
                kwargs = entry.setdefault("kwargs", {})
                if not isinstance(kwargs, dict):
                    raise QuickstartError(f"{job} declares non-mapping agent kwargs")
                kwargs["install_prefix"] = install_prefix
            job.write_text(
                yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
        _commit_tree(target_root, f"quickstart: docker arm of {suite.name}")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


# --------------------------------------------------------------------------- #
# validity: aeval's own metric, never the subprocess exit code
# --------------------------------------------------------------------------- #


def scored_counts(store_path: Path, run_id: str) -> tuple[int, int]:
    """``(valid, total)`` from the classifier aeval's own reports use."""
    from aeval.metrics.reliability import exclusion_summary
    from aeval.store.sqlite import TrialStore

    store = TrialStore(Path(store_path))
    try:
        records = store.list_trials([run_id])
    finally:
        store.close()
    summary = exclusion_summary(records)
    return summary.valid, summary.total


def _run_id_of(run_dir: Path) -> str | None:
    manifest = run_dir / "run_manifest.json"
    if not manifest.is_file():
        return None
    try:
        run_id = json.loads(manifest.read_text(encoding="utf-8")).get("run_id")
    except (OSError, ValueError):
        return None
    return run_id if isinstance(run_id, str) and run_id else None


def _completed_run(workspace: Path) -> tuple[Path, Path, str] | None:
    """A previous run in this workspace that scored at least one valid trial."""
    run_dir = workspace / "run"
    store = workspace / "run.sqlite3"
    if not store.is_file():
        return None
    run_id = _run_id_of(run_dir)
    if run_id is None:
        return None
    try:
        valid, _total = scored_counts(store, run_id)
    except Exception:                                    # noqa: BLE001 — unreadable store
        return None
    return (run_dir, store, run_id) if valid >= 1 else None


def _next_run_name(workspace: Path) -> str:
    if not (workspace / "run").exists():
        return "run"
    index = 2
    while (workspace / f"run-{index}").exists():
        index += 1
    return f"run-{index}"


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


def plan_quickstart(
    *,
    backend: str = "docker",
    suite: Path | None = None,
    agents_dir: Path = Path("agents"),
    workspace: Path = Path("aeval-quickstart"),
    install_prefix: str | None = None,
    listen_port: int = DEFAULT_LISTEN_PORT,
    listen_host: str | None = None,
    token_count_port: int = DEFAULT_TOKEN_COUNT_PORT,
    k: int = 1,
    harbor_cli: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., Completed] | None = None,
) -> QuickstartPlan:
    """What running this quickstart would do — computed without writing."""
    if backend not in BACKENDS:
        raise QuickstartError(
            f"unknown backend {backend!r} (expected docker or e2b)",
            "aeval quickstart --backend docker   # or: e2b",
        )
    environment: Mapping[str, str] = os.environ if env is None else env
    run = runner or _exec
    workspace = Path(workspace)
    agents_dir = Path(agents_dir)
    repo_root = resolve_repo_root(agents_dir=agents_dir, suite=suite, cwd=cwd)
    chosen_suite = suite is None
    suite = Path(suite) if suite is not None else default_suite(repo_root, backend)
    if not (suite / "suite.yaml").is_file():
        raise QuickstartError(
            f"{suite} is not a suite directory (no suite.yaml)",
            "aeval quickstart --suite <dir>",
        )
    install_prefix = (
        install_prefix
        or environment.get(INSTALL_PREFIX_ENV, "").strip()
        or DEFAULT_INSTALL_PREFIX
    )
    harbor_cli = harbor_cli or environment.get(HARBOR_CLI_ENV, "").strip() or "harbor"
    state = _State(runner=run, env=environment, agents_dir=agents_dir)
    plan = QuickstartPlan(
        backend=backend, workspace=workspace, suite=suite, agents_dir=agents_dir,
        repo_root=repo_root, state=state,
    )
    self_argv = _self_argv()

    # The address is a property of the suite as much as of this host (its tasks
    # say who the sandbox may dial), so it is settled here — before anything is
    # written — and never re-guessed by a later stage.
    address, address_note = choose_listen_address(
        allowed=_task_allowed_hosts(suite),
        platform=sys.platform,
        runner=run,
        requested=listen_host,
    )
    state.address = address
    if address_note:
        plan.notes.append(address_note)
    if chosen_suite:
        # A silent pick would be worse than an error: the choice is printed, so
        # a script and a person both know which suite runs.
        plan.notes.append(
            f"no --suite was given; the built-in {backend} suite is {suite} "
            "(pass --suite to choose another)"
        )

    def _stage_argv(
        step_id: str,
        argv_of: Callable[[], Sequence[str]],
        *,
        capture_to: Path | None,
    ):
        def apply() -> None:
            argv = tuple(argv_of())
            completed = run(argv, cwd=repo_root, env=None, capture=capture_to is not None)
            if capture_to is not None:
                capture_to.parent.mkdir(parents=True, exist_ok=True)
                capture_to.write_text(completed.stdout, encoding="utf-8")
            if completed.returncode != 0:
                raise QuickstartError(
                    f"{step_id} failed (exit {completed.returncode})"
                    + (f": {completed.stderr.strip()}" if completed.stderr else ""),
                    " ".join(argv),
                )

        return apply

    if not _agent_declarations(agents_dir):
        plan.steps.append(
            Step(
                id="agents-init",
                title=f"write the starter declarations into {agents_dir}",
                dry=f"writes {agents_dir}/{{_base, _runtime, declared adapter}}.yaml",
                apply=lambda: _init_agents(agents_dir),
            )
        )
    else:
        plan.notes.append(
            f"{agents_dir} already declares {', '.join(_agent_declarations(agents_dir))}"
        )

    if backend == "docker":
        plan.steps.append(
            Step(
                id="docker-preflight",
                title="probe the local docker daemon",
                dry="runs: docker info --format {{.ServerVersion}}",
                apply=lambda: _require_docker(run),
            )
        )
        refusal = _docker_refusal(suite)
        if refusal is not None:
            raise QuickstartError(
                refusal,
                "aeval quickstart --backend docker --suite suites/tbench-pilot",
            )
        if _job_environment(suite)[0] != "docker":
            converted = docker_suite_dir(suite, workspace)
            plan.notes.append(
                f"{suite.name} is not pinned to docker; the quickstart writes and "
                f"commits a docker-pinned copy at {converted} "
                f"(environment.type=docker, n_attempts=1, install_prefix={install_prefix})"
            )
            plan.steps.append(
                Step(
                    id="suite-prepare",
                    title=f"convert {suite.name} for the docker backend",
                    dry=f"writes {converted}/ (jobs patched) and commits it "
                    "(a run refuses an uncommitted suite)",
                    apply=lambda: _prepare_suite(state, suite, install_prefix, workspace),
                )
            )
        else:
            plan.notes.append(f"{suite.name} is already pinned to docker — used as it is")
    else:
        plan.steps.append(
            Step(
                id="e2b-preflight",
                title="require the e2b SDK, the credential and the endpoint",
                dry="reads E2B_API_KEY / E2B_API_URL and the installed SDK; writes nothing",
                apply=lambda: _require_e2b(environment),
            )
        )
        kind = _job_environment(suite)[0]
        if kind is not None and kind != "e2b":
            plan.notes.append(
                f"{suite.name} pins environment.type={kind!r}; the suite is used as it "
                "is — the run's own backend wins"
            )

    reusable = _completed_run(workspace)
    if reusable is not None:
        run_dir, store, run_id = reusable
        state.run_dir, state.store, state.run_id = run_dir, store, run_id
        plan.notes.append(
            f"{run_dir} already holds a complete run — it is re-reported, not re-run "
            "(remove the workspace to run again)"
        )
    else:
        state.run_dir = workspace / _next_run_name(workspace)
        state.store = workspace / f"{state.run_dir.name}.sqlite3"
        plan.steps += [
            Step(
                id="tls",
                title="generate the CA and the gateway certificate",
                dry=f"writes {workspace}/tls/{{ca.crt,ca.key,gateway.crt,gateway.key}} "
                "(the SAN covers the sandbox-facing host address)",
                apply=lambda: _make_tls(state, workspace),
            ),
            Step(
                id="token-counter",
                title=f"serve the trusted token counter on 127.0.0.1:{token_count_port}",
                dry="starts an in-process HTTP server; writes nothing",
                apply=lambda: _start_counter(state, token_count_port),
            ),
            Step(
                id="broker-spec",
                title="write the broker spec",
                dry=f"writes {workspace}/broker.json (0600: upstream identity, limits, "
                "TLS material, token-counter endpoint)",
                apply=lambda: _make_spec(state, workspace, suite, listen_port),
            ),
            Step(
                id="run",
                title=f"run the suite end to end (harbor {harbor_cli} + the aeval plugin)",
                dry=f"writes {state.run_dir}/ and {state.store}",
                apply=lambda: _do_run(state, plan, harbor_cli=harbor_cli),
            ),
        ]

    plan.steps += [
        Step(
            id="evidence-links",
            title="link the trial evidence into runs/trials/",
            dry=f"writes {workspace}/runs/trials/<trial> symlinks",
            apply=lambda: _link_evidence(state, workspace),
        ),
        Step(
            id="report",
            title="render report.md from the store",
            dry=f"runs: aeval report <run-id> --store {state.store} --k {k} "
            f"> {workspace}/report.md",
            apply=_stage_argv(
                "report",
                lambda: (*self_argv, "report", _run_id_arg(state),
                         "--store", str(state.store), "--k", str(k)),
                capture_to=workspace / "report.md",
            ),
        ),
        Step(
            id="dashboard",
            title="render dashboard.html from the store",
            dry=f"writes {workspace}/dashboard.html",
            apply=_stage_argv(
                "dashboard",
                lambda: (*self_argv, "dashboard", _run_id_arg(state),
                         "--store", str(state.store), "--k", str(k)),
                capture_to=workspace / "dashboard.html",
            ),
        ),
        Step(
            id="recompute",
            title="recompute the sealed bundle (independent re-derivation)",
            dry=f"runs: aeval recompute {state.run_dir}",
            apply=_stage_argv(
                "recompute",
                lambda: (*self_argv, "recompute", str(state.run_dir)),
                capture_to=None,
            ),
        ),
        Step(
            id="verdict",
            title="score the run with aeval's own reliability metric",
            dry="reads the sealed run and its store; writes nothing",
            apply=lambda: _verdict(state, workspace),
        ),
    ]
    return plan


def _run_id_arg(state: _State) -> str:
    """The run id the store knows, resolved when the stage runs.

    The id is minted by the run itself, so a plan cannot bake it in; reading it
    back from the sealed manifest is what keeps the report and the store in
    agreement even when the directory name is not the id.
    """
    if state.run_id:
        return state.run_id
    if state.run_dir is not None:
        run_id = _run_id_of(state.run_dir)
        if run_id:
            return run_id
        return f"run-{state.run_dir.name}"
    return "run"


def _self_argv() -> tuple[str, ...]:
    console = shutil.which("aeval")
    if console:
        return (console,)
    return (sys.executable, "-m", "aeval.cli")


# --------------------------------------------------------------------------- #
# stage implementations
# --------------------------------------------------------------------------- #


def _init_agents(agents_dir: Path) -> None:
    from aeval.agents.scaffold import write_starter_declarations

    agents_dir.mkdir(parents=True, exist_ok=True)
    write_starter_declarations(agents_dir)


def _require_docker(run: Callable[..., Completed]) -> None:
    result = run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd=None, env=None,
                 capture=True)
    if result.returncode != 0:
        raise QuickstartError(
            "docker daemon unreachable: "
            + ((result.stderr or "").strip() or f"docker info exited {result.returncode}"),
            "docker info   # start the daemon (or use --backend e2b)",
        )


def _require_e2b(environment: Mapping[str, str]) -> None:
    from aeval.provenance import BackendNotAvailableError, verify_e2b_backend

    try:
        verify_e2b_backend()
    except BackendNotAvailableError as exc:
        raise QuickstartError(str(exc), 'uv pip install "harbor[e2b]"') from exc
    if not environment.get("E2B_API_KEY", "").strip():
        raise QuickstartError(
            "E2B_API_KEY is not exported — a managed sandbox cannot be created",
            "export E2B_API_KEY=<your key>",
        )
    endpoint = environment.get("E2B_API_URL", "").strip()
    if endpoint:
        from aeval.doctor import _http_probe

        probe = _http_probe(endpoint, timeout=5.0)
        if not probe.reachable:
            raise QuickstartError(
                f"E2B_API_URL={endpoint} unreachable — {probe.detail}",
                f"curl -sI {endpoint}",
            )


def _prepare_suite(
    state: _State, suite: Path, install_prefix: str, workspace: Path
) -> None:
    state.suite = convert_suite_for_docker(suite, install_prefix, workspace=workspace)


def _make_tls(state: _State, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if state.address is None:
        raise QuickstartError(
            "the host address was not resolved",
            "aeval quickstart --listen-host <the address the sandbox can reach>",
        )
    state.tls = _write_tls_material(workspace, state.address)


def _start_counter(state: _State, port: int) -> str | None:
    state.counter = TokenCounter.start(port)
    if state.counter.requested != state.counter.port:
        return (
            f"port {state.counter.requested} is taken on this host; the counter runs on "
            f"{state.counter.port} instead and the spec names it"
        )
    return None


def _broker_spec_path(workspace: Path) -> Path:
    return workspace / "broker.json"


def control_package_root(
    *, env: Mapping[str, str], agents_dir: Path
) -> Path | None:
    """Where the compiled control package is, or ``None``.

    Two layers can name it, and neither replaces the other:

    * ``AEVAL_QUICKSTART_CONTROL_ROOT`` is this command's own override: it
      answers "which control checkout does the broker spec point at";
    * the declared control stack's doctor facts answer it by themselves — that
      is where the adapter's own lookup lives (including the package-root
      variable the adapter documents), and core may not name one.

    This is why the hint below names the first variable and points at
    ``aeval doctor`` for the second: a variable nothing reads is worse than no
    hint at all.
    """
    override = env.get(CONTROL_ROOT_ENV, "").strip()
    if override:
        return Path(override)
    from aeval.agents.control_doctor import collect_control_doctor_facts

    facts = collect_control_doctor_facts(spec=None, env=env, agents_dir=Path(agents_dir))
    return facts.control_root


def _make_spec(state: _State, workspace: Path, suite: Path, listen_port: int) -> None:
    if state.address is None:
        raise QuickstartError("the host address was not resolved")
    if state.counter is None:
        raise QuickstartError("the token counter is not running")
    control_root = control_package_root(env=state.env, agents_dir=state.agents_dir)
    if control_root is None:
        raise QuickstartError(
            "the compiled control package could not be located, so the broker spec "
            "cannot name the broker entry it must run",
            # The variable named here is the one THIS command reads, and the
            # pointer covers the other layer: which package the adapter itself
            # looks for is the stack's knowledge, so `aeval doctor` reports it.
            f"export {CONTROL_ROOT_ENV}=/abs/path/to/a/control/checkout   "
            "# its dist/ must be built; aeval doctor names the package the adapter looks for",
        )
    spec = build_broker_spec(
        address=state.address,
        token_count_endpoint=state.counter.endpoint,
        tls=state.tls,
        suite=state.suite or suite,
        control_root=control_root,
        listen_port=listen_port,
    )
    path = _broker_spec_path(workspace)
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _do_run(state: _State, plan: QuickstartPlan, *, harbor_cli: str) -> None:
    from aeval.hooks.broker_lifecycle import BROKER_SPEC_ENV

    assert state.run_dir is not None and state.store is not None
    plan.workspace.mkdir(parents=True, exist_ok=True)
    argv = [
        *_self_argv(), "run",
        "--suite", str(state.suite or plan.suite),
        "--run-dir", str(state.run_dir),
        "--store", str(state.store),
        "--agents-dir", str(plan.agents_dir),
        "--harbor-cli", harbor_cli,
    ]
    if not list(plan.workspace.glob("run*/run_manifest.json")):
        # The first run in a workspace builds the sandbox template; Harbor would
        # otherwise reuse an existing alias and silently run a stale image.
        argv.append("--force-build")
    env = {BROKER_SPEC_ENV: str(_broker_spec_path(plan.workspace))}
    # The suite/runtime -> sandbox-image table is resolved from the working
    # directory by default, which is exactly what makes the same command behave
    # differently depending on where the caller stood. Naming it keeps the table
    # and --agents-dir consistent.
    images = plan.repo_root / "agents" / "_runtime" / "images.yaml"
    if images.is_file():
        env[RUNTIME_IMAGES_ENV] = str(images)
    completed = state.runner(
        tuple(argv), cwd=plan.repo_root, env=env, capture=False
    )
    if completed.returncode != 0:
        raise QuickstartError(
            f"the run failed (exit {completed.returncode}) — its outputs are in "
            f"{state.run_dir}",
            f"aeval doctor --run-dir {state.run_dir}",
        )


def _link_evidence(state: _State, workspace: Path) -> None:
    """Symlink each trial's evidence directory into ``runs/trials/``.

    The report reads the store, but the trial evidence is what a person comes
    back for; linking it keeps the artifacts reachable from one place instead
    of under a nested harbor directory whose name changes per run.
    """
    run_dir = state.run_dir
    if run_dir is None or not run_dir.is_dir():
        return
    links = workspace / "runs" / "trials"
    links.mkdir(parents=True, exist_ok=True)
    for candidate in sorted(run_dir.glob("harbor/*/*/")):
        if not (candidate / "collection_manifest.json").is_file():
            continue
        link = links / candidate.name
        if link.is_symlink() or link.exists():
            continue
        link.symlink_to(candidate, target_is_directory=True)


def _verdict(state: _State, workspace: Path) -> None:
    run_dir = state.run_dir or workspace / "run"
    store = state.store or workspace / f"{run_dir.name}.sqlite3"
    run_id = state.run_id or _run_id_of(run_dir)
    if run_id is None:
        raise QuickstartError(
            f"no sealed run in {run_dir} — the run did not complete",
            f"aeval doctor --run-dir {run_dir}",
        )
    state.run_id = run_id
    state.valid, state.total = scored_counts(store, run_id)


def _write_state(plan: QuickstartPlan, state: _State, result: QuickstartResult) -> None:
    if state.run_dir is None:
        return
    payload = {
        "schema": QUICKSTART_SCHEMA,
        "backend": plan.backend,
        "suite": str(state.suite or plan.suite),
        "run_dir": str(state.run_dir),
        "run_id": state.run_id,
        "store": str(state.store) if state.store else None,
        "report": str(result.report_path) if result.report_path else None,
        "valid": result.valid,
        "total": result.total,
        "exit_code": result.exit_code,
    }
    (plan.workspace / "quickstart.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


def run_quickstart(
    *,
    backend: str = "docker",
    suite: Path | None = None,
    agents_dir: Path = Path("agents"),
    workspace: Path = Path("aeval-quickstart"),
    install_prefix: str | None = None,
    listen_port: int = DEFAULT_LISTEN_PORT,
    listen_host: str | None = None,
    token_count_port: int = DEFAULT_TOKEN_COUNT_PORT,
    k: int = 1,
    harbor_cli: str | None = None,
    execute: bool = False,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    echo: Callable[[str], None] | None = None,
    runner: Callable[..., Completed] | None = None,
) -> QuickstartResult:
    """Plan the quickstart, and carry it out only when asked to."""
    emit = echo or (lambda text: print(text))
    workspace = Path(workspace)
    workspace_before = workspace.exists()
    plan = plan_quickstart(
        backend=backend, suite=suite, agents_dir=agents_dir, workspace=workspace,
        install_prefix=install_prefix, listen_port=listen_port,
        listen_host=listen_host, token_count_port=token_count_port, k=k,
        harbor_cli=harbor_cli, cwd=cwd, env=env, runner=runner,
    )
    if not execute:
        emit(plan.render())
        if not workspace_before and workspace.exists():
            raise QuickstartError(
                f"internal error: planning created {workspace} — the dry run wrote "
                "into the workspace"
            )
        emit("nothing was written — re-run with --yes to carry this out")
        return QuickstartResult(plan=plan, exit_code=0)

    state = plan.state
    assert state is not None
    result = QuickstartResult(plan=plan, exit_code=0)
    try:
        for step in plan.steps:
            emit(f"== {step.id}: {step.title}")
            note = step.apply()
            if isinstance(note, str) and note:
                emit(f"   {note}")
            result.steps_run.append(step.id)
            if step.id == "verdict":
                result.valid, result.total = state.valid, state.total
    finally:
        if state.counter is not None:
            state.counter.stop()

    result.report_path = plan.workspace / "report.md"
    line = result.scored_line()
    if line is not None:
        emit(line)
    if (result.valid or 0) < 1:
        emit(
            "the run scored no valid trial — nothing was measured; the cause of each "
            f"excluded trial is in {state.run_dir}/aeval_run_summary.json"
        )
        result.exit_code = EXIT_NO_VALID_TRIALS
    _write_state(plan, state, result)
    return result
