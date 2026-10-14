"""``aeval doctor`` unit tests — every check, every recorded failure form.

Nothing here touches the network, a docker daemon, a DSH home or the real
control checkout: the two leaf operations (a process and an HTTP probe) are
injected, and the DSH home discovery is patched to an empty list. The one real
filesystem dependency is ``git``, used by the suite-provenance cases exactly as
the run gate uses it.
"""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aeval import doctor as doctor_mod
from aeval.doctor import (
    CHECK_IDS,
    DOCTOR_SCHEMA,
    EXIT_DOCTOR_FAIL,
    CommandResult,
    ProbeResult,
    run_doctor,
    version_satisfies,
)

REPO = Path(__file__).resolve().parents[2]
# A real suite × agent pairing the runtime→image table covers, so the two
# checks `aeval check` owns are exercised through their real implementation.
PAIRED_SUITE = REPO / "suites" / "e2e-hello"

API_KEY_VALUE = "k" * 35


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


class FakeRuntime:
    """The injected process runner and HTTP probe, with the calls recorded."""

    def __init__(
        self,
        *,
        node_version: str = "v24.20.0",
        node_rc: int = 0,
        docker_rc: int = 0,
        docker_out: str = "25.0.5",
    ) -> None:
        self.node_version = node_version
        self.node_rc = node_rc
        self.docker_rc = docker_rc
        self.docker_out = docker_out
        self.calls: list[list[str]] = []
        self.probes: list[tuple[str, str]] = []
        self.probe_result = ProbeResult(True, "HTTP 200")

    def command(self, argv, timeout: float = 30.0) -> CommandResult:
        self.calls.append(list(argv))
        if argv[0] == "node":
            if self.node_rc:
                return CommandResult(self.node_rc, "", "node: command not found")
            return CommandResult(0, self.node_version, "")
        if argv[0] == "docker":
            if self.docker_rc:
                return CommandResult(
                    self.docker_rc, "", "Cannot connect to the Docker daemon"
                )
            return CommandResult(0, self.docker_out, "")
        return CommandResult(0, "", "")

    def probe(self, url, *, method: str = "GET", body=None, timeout: float = 5.0):
        self.probes.append((url, method))
        return self.probe_result


def _control_package(root: Path, *, reader: bool = True, deps: bool = True) -> Path:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "broker_main.js").write_text("// broker\n", encoding="utf-8")
    if reader:
        (dist / "session_reader.js").write_text("// reader\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "dsh-eval-control",
                "version": "0.2.6",
                "engines": {"node": "^22.19.0 || >=24.0.0"},
                "peerDependencies": {"@deepseek-ai/dsh": ">=0.2.0-0 <0.3.0-0"},
            }
        ),
        encoding="utf-8",
    )
    if deps:
        from aeval.agents.dsh.release import DSH_CONTROL_DIRECT_IMPORTS

        for name in DSH_CONTROL_DIRECT_IMPORTS:
            package_json = root / "node_modules" / name / "package.json"
            package_json.parent.mkdir(parents=True, exist_ok=True)
            package_json.write_text("{}", encoding="utf-8")
    return root


def _write_self_signed(directory: Path, host: str, *, sans: list[str] | None = None) -> Path:
    """A PEM cert whose SAN names ``host``; its own private key beside it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    import ipaddress

    names: list[object] = []
    for value in sans if sans is not None else [host, "localhost"]:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            names.append(x509.DNSName(value))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (directory / "gateway.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "gateway.crt"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path


class Scenario:
    """A healthy wiring: control package, spec, agents dir, runtime, cert."""

    def __init__(self, base: Path, *, host: str = "10.0.0.12") -> None:
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.ctrl = _control_package(base / "ctrl")
        self.agents = base / "agents"
        self.agents.mkdir()
        (self.agents / "mydsh.yaml").write_text("id: mydsh\n", encoding="utf-8")
        # The control-stack facts are declaration-driven: a flavor registers
        # when its adapter package loads, and only a real declaration loads one.
        self.declared_agents = REPO / "agents"
        (base / "ca.crt").write_text("ca\n", encoding="utf-8")
        try:
            _write_self_signed(base, host)
        except ImportError:                       # cryptography absent: still a readable PEM? no
            (base / "gateway.crt").write_text("placeholder\n", encoding="utf-8")
            (base / "gateway.key").write_text("placeholder\n", encoding="utf-8")
        self.spec = base / "broker.json"
        self.rewrite_spec()
        self.runtime = FakeRuntime()

    def spec_data(self) -> dict:
        return {
            "controlRoot": str(self.ctrl),
            "listenHost": self.host,
            "listenPort": 8447,
            "listenTls": {
                "cert": str(self.base / "gateway.crt"),
                "key": str(self.base / "gateway.key"),
            },
            "controlCa": str(self.base / "ca.crt"),
            "upstream": {
                "provider": "deepseek",
                "baseUrl": "https://api.deepseek.com/v1",
                "apiKeyEnv": "DEEPSEEK_API_KEY",
                "model": "deepseek-chat",
            },
            "identity": {"provider": "deepseek", "model": "deepseek-chat"},
            "limits": {"maxSteps": 60, "maxTokens": 2000000},
            "maxOutputTokens": 16384,
            "tokenCount": {
                "endpoint": "http://127.0.0.1:8791/tokens/count",
                "margin": 8,
            },
        }

    def rewrite_spec(self, **overrides) -> Path:
        data = self.spec_data()
        data.update(overrides)
        self.spec.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.spec

    def run(self, **overrides):
        kwargs = dict(
            broker_json=self.spec,
            agents_dir=self.declared_agents,
            no_probe=True,
            env={"DEEPSEEK_API_KEY": API_KEY_VALUE},
            probe=self.runtime.probe,
            command=self.runtime.command,
        )
        kwargs.update(overrides)
        return run_doctor(**kwargs)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No real DSH home, and a reader that resolves without the checkout."""
    import aeval.agents.dsh.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_dsh_home_dirs", lambda: [])
    monkeypatch.setattr(agent_mod, "_npm_global_prefixes", lambda: [])
    reader = tmp_path / "resolved_session_reader.js"
    reader.write_text("//\n", encoding="utf-8")
    monkeypatch.setattr(agent_mod, "resolve_session_reader", lambda explicit=None: reader)


def _check(report, check_id):
    matches = [check for check in report.checks if check.id == check_id]
    assert len(matches) == 1, f"{check_id} appeared {len(matches)} times"
    return matches[0]


# --------------------------------------------------------------------------- #
# report shape and exit code
# --------------------------------------------------------------------------- #


def test_json_document_has_a_stable_shape(tmp_path):
    report = Scenario(tmp_path).run()
    document = report.as_dict()
    assert document["schema"] == DOCTOR_SCHEMA
    assert document["tool"] == "aeval"
    assert document["mode"] == "active"
    assert set(document) == {"schema", "tool", "mode", "summary", "checks", "exit_code"}
    assert set(document["summary"]) == {"ok", "warn", "fail"}
    assert [check["id"] for check in document["checks"]] == list(CHECK_IDS)
    for check in document["checks"]:
        assert set(check) == {"id", "status", "detail", "hint"}
        assert check["status"] in ("ok", "warn", "fail")
        assert isinstance(check["detail"], str) and check["detail"]
    # serializable as declared, and re-readable
    assert json.loads(report.render_json()) == document


def test_exit_code_is_zero_without_a_failure_and_nonzero_with_one(tmp_path):
    healthy = Scenario(tmp_path / "healthy").run()
    assert healthy.exit_code == 0, healthy.render()
    assert healthy.failures == ()

    broken = Scenario(tmp_path / "broken")
    broken.rewrite_spec(upstream={"provider": "deepseek", "baseUrl": "https://x/v1",
                                  "apiKeyEnv": "DEEPSEEK_API_KEY"})
    report = broken.run()
    assert report.exit_code == EXIT_DOCTOR_FAIL
    assert report.exit_code != 0


def test_render_uses_the_selfcheck_line_format_and_hint(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(
        upstream={"provider": "deepseek", "baseUrl": "https://x/v1",
                  "apiKeyEnv": "DEEPSEEK_API_KEY"}
    )
    rendered = scenario.run().render()
    assert rendered.splitlines()[0] == "aeval doctor — mode: active"
    body = [line for line in rendered.splitlines() if line.startswith("  [")]
    assert len(body) == len(CHECK_IDS)
    assert all(line.lstrip().startswith(("[ok]", "[warn]", "[fail]")) for line in body)
    assert "  [fail] broker-spec" in rendered
    assert "  hint: " in rendered


def test_doctor_writes_nothing(tmp_path):
    scenario = Scenario(tmp_path)
    run_dir = scenario.base / "runs" / "1"
    (run_dir / "trials" / "t1").mkdir(parents=True)
    (run_dir / "trials" / "t1" / "collection_manifest.json").write_text("{}", encoding="utf-8")

    def snapshot(root: Path) -> dict:
        return {
            str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }

    before = snapshot(scenario.base)
    scenario.run(run_dir=run_dir)
    assert snapshot(scenario.base) == before


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #


def test_node_outside_the_window_fails_with_an_install_hint(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.runtime.node_version = "v22.18.9"
    check = _check(scenario.run(), "node")
    assert check.status == "fail"
    assert "v22.18.9" in check.detail
    assert "^22.19.0 || >=24.0.0" in check.detail
    assert check.hint.startswith("nvm")


def test_node_inside_the_window_passes(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.runtime.node_version = "v24.20.0"
    assert _check(scenario.run(), "node").status == "ok"


def test_node_missing_from_path_fails(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.runtime.node_rc = 127
    check = _check(scenario.run(), "node")
    assert check.status == "fail" and "not runnable" in check.detail


def test_node_window_is_read_from_the_control_package(tmp_path):
    """When the installed plugin declares its own engines, that is the authority."""
    scenario = Scenario(tmp_path)
    (scenario.ctrl / "package.json").write_text(
        json.dumps({"name": "dsh-eval-control", "engines": {"node": "^20.0.0"}}),
        encoding="utf-8",
    )
    scenario.runtime.node_version = "v24.20.0"
    check = _check(scenario.run(), "node")
    assert check.status == "fail" and "requires ^20.0.0" in check.detail


# --------------------------------------------------------------------------- #
# control package
# --------------------------------------------------------------------------- #


def test_missing_session_reader_is_the_root_cause(tmp_path):
    """Recorded form: ``official DSH session reader not found``."""
    scenario = Scenario(tmp_path)
    scenario.ctrl = _control_package(scenario.base / "ctrl-no-reader", reader=False)
    scenario.rewrite_spec(controlRoot=str(scenario.ctrl))
    check = _check(scenario.run(), "control-dist")
    assert check.status == "fail"
    assert "session_reader.js" in check.detail
    assert check.hint.startswith("cd ")


def test_control_root_without_a_built_dist_is_refused(tmp_path):
    """Recorded form: ``carries no controlDist and no controlRoot with a built dist/``."""
    scenario = Scenario(tmp_path)
    bare = scenario.base / "ctrl-bare"
    bare.mkdir()
    scenario.rewrite_spec(controlRoot=str(bare))
    report = scenario.run()
    assert report.exit_code != 0
    assert _check(report, "control-dist").status == "fail"
    assert "not a built dist/" in _check(report, "control-dist").detail
    spec_check = _check(report, "broker-spec")
    assert spec_check.status == "fail"
    assert "needs brokerJs" in spec_check.detail  # the broker's own refusal, verbatim


def test_undeclared_control_package_falls_back_to_the_run_resolver(tmp_path, monkeypatch):
    import aeval.agents.dsh.agent as agent_mod
    from aeval.agents.dsh.agent import DshRunError

    scenario = Scenario(tmp_path)
    monkeypatch.setattr(
        agent_mod,
        "resolve_session_reader",
        lambda explicit=None: (_ for _ in ()).throw(
            DshRunError("official DSH session reader not found; tried a, b")
        ),
    )
    # no spec at all: the fallback resolution a run performs is what doctor reports
    report = scenario.run(broker_json=None, env={})
    check = _check(report, "control-dist")
    assert check.status == "fail"
    assert "official DSH session reader not found" in check.detail
    assert check.hint.startswith("export AEVAL_DSH_CONTROL_ROOT=")


def test_control_deps_missing_peers_fail(tmp_path):
    """Recorded form: ``Cannot find package '@deepseek-ai/dsh-llm'``."""
    scenario = Scenario(tmp_path)
    scenario.ctrl = _control_package(scenario.base / "ctrl-nodeps", deps=False)
    scenario.rewrite_spec(controlRoot=str(scenario.ctrl))
    check = _check(scenario.run(), "control-deps")
    assert check.status == "fail"
    assert "@deepseek-ai/dsh-llm" in check.detail
    assert check.hint.startswith("cd ")


def test_control_deps_present_pass(tmp_path):
    check = _check(Scenario(tmp_path).run(), "control-deps")
    assert check.status == "ok" and "direct package(s) present" in check.detail


def test_control_deps_without_a_named_package_warns(tmp_path):
    scenario = Scenario(tmp_path)
    report = scenario.run(broker_json=None, env={})
    check = _check(report, "control-deps")
    assert check.status == "warn" and "not checked" in check.detail


# --------------------------------------------------------------------------- #
# broker spec
# --------------------------------------------------------------------------- #


def test_standalone_mode_warns_and_still_exits_zero(tmp_path):
    report = Scenario(tmp_path).run(broker_json=None, env={})
    assert report.mode == "standalone"
    assert report.exit_code == 0
    check = _check(report, "broker-spec")
    assert check.status == "warn"
    assert "AEVAL_BROKER_JSON is not set" in check.detail


def test_unreadable_spec_fails(tmp_path):
    scenario = Scenario(tmp_path)
    report = scenario.run(broker_json=scenario.base / "missing.json")
    check = _check(report, "broker-spec")
    assert check.status == "fail" and "is not a file" in check.detail


def test_spec_with_invalid_json_fails_with_a_validator_hint(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.spec.write_text("{not json", encoding="utf-8")
    report = scenario.run()
    check = _check(report, "broker-spec")
    assert check.status == "fail" and "unreadable" in check.detail
    assert check.hint.startswith("python3 -m json.tool")


def test_upstream_missing_a_field_fails(tmp_path):
    """Recorded form: ``broker config upstream.model: invalid identifier ''``."""
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(
        upstream={
            "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
        }
    )
    check = _check(scenario.run(), "broker-spec")
    assert check.status == "fail"
    assert "model" in check.detail
    assert "invalid identifier" in check.detail


def test_spec_structural_rules_come_from_the_run_parser(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(unknownKey=True)
    check = _check(scenario.run(), "broker-spec")
    assert check.status == "fail" and "unknown keys" in check.detail


# --------------------------------------------------------------------------- #
# upstream key
# --------------------------------------------------------------------------- #


def test_missing_upstream_key_reports_the_masquerade(tmp_path):
    """Recorded root cause: the host never exported the key the spec names."""
    scenario = Scenario(tmp_path)
    report = scenario.run(env={})
    check = _check(report, "upstream-key")
    assert check.status == "fail"
    assert "DEEPSEEK_API_KEY" in check.detail
    assert "MISSING_CREDENTIAL" in check.detail
    assert check.hint == "export DEEPSEEK_API_KEY=<your key>"


def test_exported_upstream_key_passes_and_never_leaks_the_value(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(env={"DEEPSEEK_API_KEY": API_KEY_VALUE}), "upstream-key")
    assert check.status == "ok"
    assert API_KEY_VALUE not in check.detail
    assert "35 chars" in check.detail


def test_upstream_key_not_declared_warns(tmp_path):
    scenario = Scenario(tmp_path)
    report = scenario.run(broker_json=None, env={})
    assert _check(report, "upstream-key").status == "warn"


# --------------------------------------------------------------------------- #
# token counter
# --------------------------------------------------------------------------- #


def test_max_tokens_without_a_counter_fails(tmp_path):
    """Recorded form: ``limits.maxTokens requires a trusted tokenCount source``."""
    scenario = Scenario(tmp_path)
    data = scenario.spec_data()
    data.pop("tokenCount")
    scenario.spec.write_text(json.dumps(data), encoding="utf-8")
    check = _check(scenario.run(), "token-count")
    assert check.status == "fail"
    assert "requires a trusted tokenCount source" in check.detail
    assert "token_count.py" in check.hint


def test_max_tokens_with_a_counter_passes(tmp_path):
    assert _check(Scenario(tmp_path).run(), "token-count").status == "ok"


def test_no_max_tokens_needs_no_counter(tmp_path):
    scenario = Scenario(tmp_path)
    data = scenario.spec_data()
    data["limits"] = {"maxSteps": 60}
    data.pop("tokenCount")
    scenario.spec.write_text(json.dumps(data), encoding="utf-8")
    check = _check(scenario.run(), "token-count")
    assert check.status == "ok" and "not set" in check.detail


def test_counter_without_a_margin_warns(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(tokenCount={"endpoint": "http://127.0.0.1:8791/tokens/count"})
    check = _check(scenario.run(), "token-count")
    assert check.status == "warn" and "margin" in check.detail


def test_unreachable_counter_endpoint_fails(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.runtime.probe_result = ProbeResult(False, "URLError: connection refused")
    check = _check(scenario.run(no_probe=False), "token-endpoint")
    assert check.status == "fail"
    assert "unreachable" in check.detail
    assert check.hint == "python3 suites/<suite>/tools/token_count.py --host 127.0.0.1 --port 8791 &"


def test_reachable_counter_endpoint_passes_and_is_probed_with_post(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(no_probe=False), "token-endpoint")
    assert check.status == "ok" and "HTTP 200" in check.detail
    assert scenario.runtime.probes == [("http://127.0.0.1:8791/tokens/count", "POST")]


def test_no_probe_skips_the_counter_probe(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(no_probe=True), "token-endpoint")
    assert check.status == "warn" and "--no-probe" in check.detail
    assert scenario.runtime.probes == []


# --------------------------------------------------------------------------- #
# listen host / TLS / certificate
# --------------------------------------------------------------------------- #


def test_loopback_listen_host_fails(tmp_path):
    """Recorded form: ``ECONNREFUSED 127.0.0.1:8447`` then no DSH session."""
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(listenHost="127.0.0.1")
    check = _check(scenario.run(), "listen-tls")
    assert check.status == "fail"
    assert "loopback" in check.detail and "ECONNREFUSED" in check.detail
    assert check.hint.startswith("HOST_IP=$(")
    assert "openssl req" in check.hint


@pytest.mark.parametrize(
    "platform, expected",
    [
        ("darwin", "ipconfig getifaddr en0"),
        ("linux", "hostname -I"),
    ],
)
def test_the_listen_hint_reads_the_host_ip_the_way_this_platform_does(
    tmp_path, monkeypatch, platform, expected
):
    """The hint must be copy-pasteable on macOS AND Linux, not only one."""
    monkeypatch.setattr(doctor_mod.sys, "platform", platform)
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(listenHost="127.0.0.1")
    check = _check(scenario.run(), "listen-tls")
    assert expected in check.hint
    assert check.hint.startswith("HOST_IP=$(")


def test_non_loopback_listen_requires_tls_and_ca(tmp_path):
    scenario = Scenario(tmp_path)
    data = scenario.spec_data()
    data.pop("listenTls")
    data.pop("controlCa")
    scenario.spec.write_text(json.dumps(data), encoding="utf-8")
    check = _check(scenario.run(), "listen-tls")
    assert check.status == "fail"
    assert "listenTls" in check.detail and "controlCa" in check.detail


def test_non_loopback_listen_requires_readable_tls_files(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.rewrite_spec(
        listenTls={"cert": str(scenario.base / "nope.crt"),
                   "key": str(scenario.base / "nope.key")}
    )
    check = _check(scenario.run(), "listen-tls")
    assert check.status == "fail" and "not a file" in check.detail


def test_unreadable_tls_file_is_reported(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)
    monkeypatch.setattr(doctor_mod.os, "access", lambda path, mode: False)
    check = _check(scenario.run(), "listen-tls")
    assert check.status == "fail" and "not readable" in check.detail


def test_complete_non_loopback_tls_passes(tmp_path):
    assert _check(Scenario(tmp_path).run(), "listen-tls").status == "ok"


def test_certificate_san_must_cover_the_listen_host(tmp_path):
    scenario = Scenario(tmp_path)
    _write_self_signed(scenario.base, "10.0.0.12", sans=["10.0.0.99"])
    check = _check(scenario.run(), "cert-san")
    assert check.status == "fail"
    assert "does not contain listenHost=10.0.0.12" in check.detail
    assert "subjectAltName" in check.hint


def test_certificate_san_covering_the_host_passes(tmp_path):
    scenario = Scenario(tmp_path)
    _write_self_signed(scenario.base, "10.0.0.12", sans=["10.0.0.12", "localhost"])
    check = _check(scenario.run(), "cert-san")
    assert check.status == "ok" and "SAN covers listenHost=10.0.0.12" in check.detail


def test_uninspectable_certificate_only_warns(tmp_path):
    scenario = Scenario(tmp_path)
    (scenario.base / "gateway.crt").write_text("not a pem\n", encoding="utf-8")
    assert _check(scenario.run(), "cert-san").status == "warn"


def test_cert_san_skipped_without_a_spec(tmp_path):
    report = Scenario(tmp_path).run(broker_json=None, env={})
    assert _check(report, "cert-san").status == "ok"


# --------------------------------------------------------------------------- #
# DSH profile peer window
# --------------------------------------------------------------------------- #


def _dsh_home(base: Path, version: str, *, profile: str = "headless") -> Path:
    home = base / "dshhome"
    package = home / "profiles" / profile / "node_modules" / "@deepseek-ai" / "dsh"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    return home


def _patch_dsh_home(monkeypatch, home: Path) -> None:
    import aeval.agents.dsh.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_dsh_home_dirs", lambda: [home])


def test_installed_dsh_outside_the_peer_window_fails(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)
    _patch_dsh_home(monkeypatch, _dsh_home(scenario.base, "0.4.1"))
    check = _check(scenario.run(), "dsh-profile")
    assert check.status == "fail"
    assert "0.4.1" in check.detail and ">=0.2.0-0 <0.3.0-0" in check.detail
    assert check.hint.startswith("dsh plugin add")


def test_installed_dsh_inside_the_peer_window_passes(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)
    _patch_dsh_home(monkeypatch, _dsh_home(scenario.base, "0.2.0-rc.2"))
    check = _check(scenario.run(), "dsh-profile")
    assert check.status == "ok" and "0.2.0-rc.2" in check.detail


def test_peer_window_comes_from_the_control_package(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)
    (scenario.ctrl / "package.json").write_text(
        json.dumps({"name": "dsh-eval-control",
                    "peerDependencies": {"@deepseek-ai/dsh": ">=9.0.0"}}),
        encoding="utf-8",
    )
    _patch_dsh_home(monkeypatch, _dsh_home(scenario.base, "0.2.0-rc.2"))
    check = _check(scenario.run(), "dsh-profile")
    assert check.status == "fail" and ">=9.0.0" in check.detail


def test_no_dsh_install_warns(tmp_path):
    assert _check(Scenario(tmp_path).run(), "dsh-profile").status == "warn"


@pytest.mark.parametrize(
    ("version", "range_text", "expected"),
    [
        ("0.2.0-rc.2", ">=0.2.0-0 <0.3.0-0", True),
        ("0.3.0-rc.1", ">=0.2.0-0 <0.3.0-0", False),
        ("0.1.7-alpha.1", ">=0.1.7-alpha.1 <0.1.8-0 || >=0.2.0-0 <0.3.0-0", True),
        ("0.1.8-beta.1", ">=0.1.7-alpha.1 <0.1.8-0 || >=0.1.8-alpha.1 <0.2.0-0", True),
        ("0.1.8", ">=0.1.7-alpha.1 <0.1.8-0 || >=0.1.8-alpha.1 <0.2.0-0", True),
        ("0.2.0", ">=0.1.7-alpha.1 <0.1.8-0 || >=0.1.8-alpha.1 <0.2.0-0", False),
        ("v22.21.0", "^22.19.0 || >=24.0.0", True),
        ("v22.18.9", "^22.19.0 || >=24.0.0", False),
        ("v23.5.0", "^22.19.0 || >=24.0.0", False),
        ("v24.20.0", "^22.19.0 || >=24.0.0", True),
        ("1.4.2", "~1.4.0", True),
        ("1.5.0", "~1.4.0", False),
    ],
)
def test_version_ranges_match_npm_semantics(version, range_text, expected):
    assert version_satisfies(version, range_text) is expected


# --------------------------------------------------------------------------- #
# agents, provenance, evidence
# --------------------------------------------------------------------------- #


def test_missing_agent_declarations_fail_with_the_initializer(tmp_path):
    """Recorded form: ``error: no agent declarations under agents``."""
    scenario = Scenario(tmp_path)
    empty = scenario.base / "no-agents"
    empty.mkdir()
    check = _check(scenario.run(agents_dir=empty), "agents-declaration")
    assert check.status == "fail"
    assert "no agent declarations under" in check.detail
    assert check.hint == f"aeval agents --init --agents-dir {empty}"


def test_declared_agents_pass(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(agents_dir=scenario.agents), "agents-declaration")
    assert check.status == "ok" and "mydsh" in check.detail


def test_the_repo_declarations_name_a_control_stack(tmp_path):
    """Without a declared stack the control-stack checks can only say so."""
    scenario = Scenario(tmp_path)
    for check_id in ("control-dist", "control-deps", "dsh-profile"):
        check = _check(scenario.run(agents_dir=scenario.agents), check_id)
        assert check.status == "warn" and "not checked" in check.detail
    loaded = _check(scenario.run(), "control-dist")
    assert loaded.status in {"ok", "fail"} and "not checked" not in loaded.detail


def _git_repo(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=directory, check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                       ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(directory), "config", key, value], check=True)


def test_suite_provenance_not_in_scope_is_ok(tmp_path):
    check = _check(Scenario(tmp_path).run(), "suite-provenance")
    assert check.status == "ok" and "not in scope" in check.detail


def test_missing_suite_yaml_fails(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(suite=scenario.base / "nope"), "suite-provenance")
    assert check.status == "fail" and "no suite.yaml" in check.detail


def test_clean_suite_commit_passes(tmp_path):
    scenario = Scenario(tmp_path)
    suite = scenario.base / "suites" / "s1"
    _git_repo(suite)
    (suite / "suite.yaml").write_text("id: s1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(suite), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(suite), "commit", "-q", "-m", "s"], check=True)
    check = _check(scenario.run(suite=suite), "suite-provenance")
    assert check.status == "ok" and "(clean)" in check.detail


def test_dirty_suite_fails_with_a_commit_hint(tmp_path):
    scenario = Scenario(tmp_path)
    suite = scenario.base / "suites" / "s1"
    _git_repo(suite)
    (suite / "suite.yaml").write_text("id: s1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(suite), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(suite), "commit", "-q", "-m", "s"], check=True)
    (suite / "extra.txt").write_text("uncommitted\n", encoding="utf-8")
    check = _check(scenario.run(suite=suite), "suite-provenance")
    assert check.status == "fail" and "uncommitted" in check.detail
    assert check.hint.startswith(f"git -C {suite} add -A")


def test_suite_outside_git_fails(tmp_path):
    scenario = Scenario(tmp_path)
    suite = scenario.base / "plain"
    suite.mkdir()
    (suite / "suite.yaml").write_text("id: p\n", encoding="utf-8")
    check = _check(scenario.run(suite=suite), "suite-provenance")
    assert check.status == "fail" and "Git" in check.detail


def test_evidence_trials_missing_fails(tmp_path):
    """Recorded form: the report shows ``证据覆盖 0/N``."""
    scenario = Scenario(tmp_path)
    run_dir = scenario.base / "runs" / "1"
    run_dir.mkdir(parents=True)
    check = _check(scenario.run(run_dir=run_dir), "evidence-trials")
    assert check.status == "fail"
    assert "0/N" in check.detail
    assert check.hint.startswith("mkdir -p ")


def test_linked_evidence_trials_pass(tmp_path):
    scenario = Scenario(tmp_path)
    run_dir = scenario.base / "runs" / "1"
    trial = run_dir / "trials" / "t1"
    trial.mkdir(parents=True)
    (trial / "collection_manifest.json").write_text("{}", encoding="utf-8")
    check = _check(scenario.run(run_dir=run_dir), "evidence-trials")
    assert check.status == "ok" and "1 trial dir(s)" in check.detail


def test_quickstart_layout_evidence_trials_pass(tmp_path):
    """``aeval quickstart`` links evidence into ``<workspace>/runs/trials``.

    The check has to look there too, otherwise it reports a coverage problem
    the report does not have (the reader accepts that layout).
    """
    scenario = Scenario(tmp_path)
    run_dir = scenario.base / "run"
    run_dir.mkdir(parents=True)
    trial = scenario.base / "runs" / "trials" / "hello-world__WS6Zd7V"
    trial.mkdir(parents=True)
    (trial / "collection_manifest.json").write_text("{}", encoding="utf-8")
    check = _check(scenario.run(run_dir=run_dir), "evidence-trials")
    assert check.status == "ok" and "1 trial dir(s)" in check.detail
    assert "runs/trials" in check.detail


def test_evidence_trials_fails_when_the_dir_holds_no_manifest(tmp_path):
    """A link directory without a manifest is still not evidence."""
    scenario = Scenario(tmp_path)
    run_dir = scenario.base / "run"
    (run_dir / "trials" / "t1").mkdir(parents=True)
    check = _check(scenario.run(run_dir=run_dir), "evidence-trials")
    assert check.status == "fail" and "0/N" in check.detail


def test_missing_run_dir_fails(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(run_dir=scenario.base / "absent"), "evidence-trials")
    assert check.status == "fail" and "does not exist" in check.detail


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #


def test_no_backend_selected_warns(tmp_path):
    check = _check(Scenario(tmp_path).run(), "backend")
    assert check.status == "warn" and "no sandbox backend selected" in check.detail


def test_docker_daemon_reachable_passes(tmp_path):
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(backend="docker", no_probe=False), "backend")
    assert check.status == "ok" and "25.0.5" in check.detail


def test_docker_daemon_unreachable_fails(tmp_path):
    scenario = Scenario(tmp_path)
    scenario.runtime.docker_rc = 1
    check = _check(scenario.run(backend="docker", no_probe=False), "backend")
    assert check.status == "fail" and "docker daemon unreachable" in check.detail
    assert check.hint.startswith("docker info")


def test_no_probe_does_not_contact_the_docker_daemon(tmp_path):
    """--no-probe means no reachability probe, the backend daemon included."""
    scenario = Scenario(tmp_path)
    scenario.runtime.docker_rc = 1          # would fail loudly if it were called
    check = _check(scenario.run(backend="docker", no_probe=True), "backend")
    assert check.status == "warn" and "skipped (--no-probe)" in check.detail
    assert check.hint.startswith("aeval doctor")


def test_no_probe_does_not_contact_the_e2b_endpoint(tmp_path):
    pytest.importorskip("e2b")
    scenario = Scenario(tmp_path)
    scenario.runtime.probe_result = ProbeResult(False, "URLError: refused")
    check = _check(
        scenario.run(
            backend="e2b",
            env={"E2B_API_KEY": "k", "E2B_API_URL": "https://e2b.example.test"},
            no_probe=True,
        ),
        "backend",
    )
    assert check.status == "warn" and "skipped (--no-probe)" in check.detail


def test_e2b_backend_without_a_key_fails(tmp_path):
    pytest.importorskip("e2b")
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(backend="e2b", env={}), "backend")
    assert check.status == "fail" and "E2B_API_KEY" in check.detail
    assert check.hint.startswith("export E2B_API_KEY=")


def test_e2b_backend_with_a_key_passes(tmp_path):
    pytest.importorskip("e2b")
    scenario = Scenario(tmp_path)
    check = _check(scenario.run(backend="e2b", env={"E2B_API_KEY": "k"}), "backend")
    assert check.status == "ok" and "e2b SDK" in check.detail


def test_e2b_endpoint_unreachable_fails(tmp_path):
    pytest.importorskip("e2b")
    scenario = Scenario(tmp_path)
    scenario.runtime.probe_result = ProbeResult(False, "URLError: refused")
    check = _check(
        scenario.run(backend="e2b", env={"E2B_API_KEY": "k",
                                         "E2B_API_URL": "https://e2b.example.test"},
                     no_probe=False),
        "backend",
    )
    assert check.status == "fail" and "unreachable" in check.detail


def test_unknown_backend_fails(tmp_path):
    check = _check(Scenario(tmp_path).run(backend="firecracker"), "backend")
    assert check.status == "fail" and "unknown backend" in check.detail


def test_backend_is_inferred_from_the_composed_job(tmp_path):
    from harbor.models.environment_type import EnvironmentType

    job = SimpleNamespace(environment=SimpleNamespace(type=EnvironmentType.DOCKER))
    ctx = SimpleNamespace(backend=None)
    assert doctor_mod._selected_backend(ctx, doctor_mod._PairingState(job=job)) == "docker"


# --------------------------------------------------------------------------- #
# pairing / runtime image (the paths aeval check uses)
# --------------------------------------------------------------------------- #


def test_pairing_not_in_scope_is_ok(tmp_path):
    check = _check(Scenario(tmp_path).run(), "pairing")
    assert check.status == "ok" and "not in scope" in check.detail


def test_uncomposable_pairing_fails_with_the_check_hint(tmp_path):
    scenario = Scenario(tmp_path)
    report = scenario.run(suite=scenario.base / "absent-suite", agent="dsh")
    check = _check(report, "pairing")
    assert check.status == "fail"
    assert check.hint.startswith("aeval check --suite ")
    assert _check(report, "runtime-image").status == "ok"     # nothing composed


def test_pairing_and_runtime_image_reuse_the_check_paths(tmp_path):
    scenario = Scenario(tmp_path)
    report = scenario.run(
        suite=PAIRED_SUITE,
        agent="dsh",
        agents_dir=REPO / "agents",
        env={"DEEPSEEK_API_KEY": API_KEY_VALUE},
    )
    pairing = _check(report, "pairing")
    assert pairing.status == "ok" and "composed" in pairing.detail
    image = _check(report, "runtime-image")
    assert image.status == "ok" and "dsh" in image.detail


def test_runtime_image_fails_when_the_table_has_no_row(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path)
    table = scenario.base / "images.yaml"
    table.write_text(
        "schema_version: 1\nimages:\n  - {suite: other-suite, runtime: dsh, "
        'image: "ref@sha256:' + "a" * 64 + '", platform: arm64}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AEVAL_RUNTIME_IMAGES", str(table))
    report = scenario.run(
        suite=PAIRED_SUITE,
        agent="dsh",
        agents_dir=REPO / "agents",
    )
    check = _check(report, "runtime-image")
    assert check.status == "fail"
    assert "images.yaml" in check.detail
    assert check.hint.startswith("aeval check --suite ")


# --------------------------------------------------------------------------- #
# every recorded failure form, one injection each
# --------------------------------------------------------------------------- #


def test_each_recorded_failure_form_reports_its_root_cause(tmp_path):
    """One injection per failure form met in the field, each naming its root cause.

    The first four are the plugin's own manifest, the rest are the failures the
    README's table records from real runs.
    """
    forms: dict[str, str] = {}

    # (1) official DSH session reader not found
    s = Scenario(tmp_path / "f1")
    s.ctrl = _control_package(s.base / "ctrl-no-reader", reader=False)
    s.rewrite_spec(controlRoot=str(s.ctrl))
    check = _check(s.run(), "control-dist")
    assert check.status == "fail" and "session_reader.js" in check.detail
    forms["1-session-reader-not-found"] = check.hint

    # (2) carries no controlDist and no controlRoot with a built dist/
    s2 = Scenario(tmp_path / "f2")
    bare = s2.base / "bare"
    bare.mkdir()
    s2.rewrite_spec(controlRoot=str(bare))
    report2 = s2.run()
    check = _check(report2, "control-dist")
    assert check.status == "fail" and "not a built dist/" in check.detail
    assert "needs brokerJs" in _check(report2, "broker-spec").detail
    forms["2-no-control-dist"] = check.hint

    # (3) upstream API key is not set (host), masquerading as MISSING_CREDENTIAL
    s3 = Scenario(tmp_path / "f3")
    check = _check(s3.run(env={}), "upstream-key")
    assert check.status == "fail" and "MISSING_CREDENTIAL" in check.detail
    forms["3-host-key-not-exported"] = check.hint

    # (4) Cannot find package '@deepseek-ai/dsh-llm'
    s4 = Scenario(tmp_path / "f4")
    s4.ctrl = _control_package(s4.base / "ctrl-nodeps", deps=False)
    s4.rewrite_spec(controlRoot=str(s4.ctrl))
    check = _check(s4.run(), "control-deps")
    assert check.status == "fail" and "@deepseek-ai/dsh-llm" in check.detail
    forms["4-missing-peer-packages"] = check.hint

    # (5) limits.maxTokens requires a trusted tokenCount source
    s5 = Scenario(tmp_path / "f5")
    data = s5.spec_data()
    data.pop("tokenCount")
    s5.spec.write_text(json.dumps(data), encoding="utf-8")
    check = _check(s5.run(), "token-count")
    assert check.status == "fail" and "requires a trusted tokenCount source" in check.detail
    forms["5-no-token-counter"] = check.hint

    # (6) ECONNREFUSED 127.0.0.1:8447 -> no DSH session
    s6 = Scenario(tmp_path / "f6")
    s6.rewrite_spec(listenHost="127.0.0.1")
    check = _check(s6.run(), "listen-tls")
    assert check.status == "fail" and "ECONNREFUSED" in check.detail
    forms["6-loopback-listen-host"] = check.hint

    # (7) no agent declarations under agents
    s7 = Scenario(tmp_path / "f7")
    empty = s7.base / "empty-agents"
    empty.mkdir()
    check = _check(s7.run(agents_dir=empty), "agents-declaration")
    assert check.status == "fail" and "no agent declarations under" in check.detail
    forms["7-no-agent-declarations"] = check.hint

    # (8) report evidence coverage 0/N: the collection artifacts are not linked
    s8 = Scenario(tmp_path / "f8")
    run_dir = s8.base / "runs" / "1"
    run_dir.mkdir(parents=True)
    check = _check(s8.run(run_dir=run_dir), "evidence-trials")
    assert check.status == "fail" and "0/N" in check.detail
    forms["8-evidence-not-linked"] = check.hint

    # each hint is a single line whose first token names a real command
    executable = {
        "nvm", "export", "cd", "python3", "${EDITOR:-vi}",
        "openssl", "dsh", "aeval", "git", "ls", "docker", "uv", "curl", "mkdir",
    }
    assert len(forms) == 8
    for name, hint in forms.items():
        assert hint and "\n" not in hint, name
        # the listen-tls hint opens with a platform-specific $HOST_IP=… binding
        assert hint.split()[0] in executable or hint.startswith("HOST_IP=$("), (name, hint)


def test_every_report_hint_is_a_command_not_a_sentence(tmp_path):
    """Whatever the state, a hint either is absent or starts with a command."""
    executable = {
        "nvm", "export", "cd", "python3", "${EDITOR:-vi}", "HOST_IP=$(hostname",
        "openssl", "dsh", "aeval", "git", "ls", "docker", "uv", "curl", "mkdir",
    }
    scenario = Scenario(tmp_path)
    for report in (
        scenario.run(),
        scenario.run(broker_json=None, env={}),
        scenario.run(backend="docker"),
        scenario.run(run_dir=scenario.base / "absent"),
    ):
        for check in report.checks:
            if check.hint:
                assert "\n" not in check.hint
                assert check.hint.split()[0] in executable, check.id
