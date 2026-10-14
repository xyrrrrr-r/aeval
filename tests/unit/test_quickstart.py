"""``aeval quickstart``: the plan, the writes, and the success criterion.

Every test here is about one of the three contracts: nothing is written unless
asked, a stage that is a no-op says so, and a run that scored nothing is a
failure. The process execution is injected everywhere, so none of this starts a
container, a Harbor job or a network call.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import aeval.quickstart as qs
from aeval.contracts import TrialRecord

REPO = Path(__file__).resolve().parents[2]

MINI_SUITE_YAML = """\
schema_version: 2
id: mini
version: 1.0.0
harbor:
  job: jobs/mini.yaml
"""

MINI_JOB_YAML = """\
job_name: mini
n_attempts: 1
environment:
  type: {kind}
agents:
  - import_path: "aeval.agents.dsh.agent:DshAgent"
    kwargs:
      install_prefix: "/dev/shm/pkg"
"""


def _mini_suite(
    base: Path, *, kind: str = "docker", allowed_hosts: list[str] | None = None
) -> Path:
    suite = base / "mini"
    (suite / "jobs").mkdir(parents=True, exist_ok=True)
    (suite / "suite.yaml").write_text(MINI_SUITE_YAML, encoding="utf-8")
    (suite / "jobs" / "mini.yaml").write_text(
        MINI_JOB_YAML.format(kind=kind), encoding="utf-8"
    )
    if allowed_hosts is not None:
        (suite / "tasks" / "hello").mkdir(parents=True, exist_ok=True)
        (suite / "tasks" / "hello" / "task.toml").write_text(
            'schema_version = "1.4"\n\n[environment]\n'
            'network_mode = "allowlist"\n'
            f"allowed_hosts = {json.dumps(allowed_hosts)}\n",
            encoding="utf-8",
        )
    else:
        shutil.rmtree(suite / "tasks", ignore_errors=True)
    return suite


class FakeRunner:
    """Records argv, and simulates the two stages that produce artifacts."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, *, cwd=None, env=None, capture=False):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(dict(env or {}))
        if Path(argv[0]).name == "docker":
            return qs.Completed(0, "25.0.5\n", "")
        if Path(argv[0]).name == "ipconfig":
            return qs.Completed(0, "193.126.4.2\n", "" if argv[2] == "en0" else "")
        if Path(argv[0]).name == "hostname":
            return qs.Completed(0, "10.0.0.9 193.126.4.2 \n", "")
        sub = next((t for t in argv if t in {"run", "report", "dashboard", "recompute"}), None)
        if sub == "run":
            run_dir = Path(argv[argv.index("--run-dir") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": f"run-{run_dir.name}"}), encoding="utf-8"
            )
            (run_dir / "aeval_run_summary.json").write_text("{}", encoding="utf-8")
            return qs.Completed(0)
        if sub == "report":
            return qs.Completed(0, "# report\n\n1/1 trial(s) scored\n")
        if sub == "dashboard":
            return qs.Completed(0, "<html></html>\n")
        if sub == "recompute":
            return qs.Completed(0, "{}\n")
        return qs.Completed(0)

    def subcommands(self) -> list[str]:
        found = []
        for argv in self.calls:
            found += [t for t in argv if t in {"run", "report", "dashboard", "recompute"}]
            if Path(argv[0]).name == "docker":
                found.append("docker")
        return found


@pytest.fixture(autouse=True)
def _fake_control_package(monkeypatch):
    """No test resolves a real control package off this machine."""
    monkeypatch.setattr(qs, "control_package_root", lambda **_: None)


_MISSING = object()


def _plan(tmp_path: Path, **overrides):
    named = overrides.pop("suite", _MISSING)
    kwargs = dict(
        backend="docker",
        suite=_mini_suite(tmp_path) if named is _MISSING else named,
        agents_dir=REPO / "agents",
        workspace=tmp_path / "ws",
        env={},
        runner=FakeRunner(),
    )
    kwargs.update(overrides)
    return qs.plan_quickstart(**kwargs)


# --------------------------------------------------------------------------- #
# A. nothing is written unless asked
# --------------------------------------------------------------------------- #


def test_dry_run_writes_nothing_not_even_the_workspace(tmp_path):
    plan = _plan(tmp_path)
    workspace = plan.workspace
    result = qs.run_quickstart(
        backend="docker",
        suite=plan.suite,
        agents_dir=plan.agents_dir,
        workspace=workspace,
        env={},
        echo=lambda _line: None,
        runner=FakeRunner(),
    )
    assert result.exit_code == 0
    assert not workspace.exists(), "a dry run must not create the workspace directory"


def test_dry_run_does_not_change_an_existing_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    before = sorted(p.name for p in workspace.iterdir())
    qs.run_quickstart(
        backend="docker",
        suite=_mini_suite(tmp_path),
        agents_dir=REPO / "agents",
        workspace=workspace,
        env={},
        echo=lambda _line: None,
        runner=FakeRunner(),
    )
    assert sorted(p.name for p in workspace.iterdir()) == before


def test_dry_run_wins_over_yes(tmp_path):
    workspace = tmp_path / "ws"
    qs.run_quickstart(
        backend="docker",
        suite=_mini_suite(tmp_path),
        agents_dir=REPO / "agents",
        workspace=workspace,
        env={},
        execute=False,
        echo=lambda _line: None,
        runner=FakeRunner(),
    )
    assert not workspace.exists()


def test_the_plan_is_printed_with_every_stage_and_its_writes(tmp_path, capsys):
    qs.run_quickstart(
        backend="docker",
        suite=_mini_suite(tmp_path),
        agents_dir=REPO / "agents",
        workspace=tmp_path / "ws",
        env={},
        echo=print,
        runner=FakeRunner(),
    )
    out = capsys.readouterr().out
    for stage in ("docker-preflight", "tls", "token-counter", "broker-spec", "run",
                  "evidence-links", "report", "recompute", "verdict"):
        assert stage in out
    assert "nothing was written" in out


# --------------------------------------------------------------------------- #
# 2. a stage that is a no-op says so
# --------------------------------------------------------------------------- #


def test_a_docker_pinned_suite_is_used_as_it_is(tmp_path):
    plan = _plan(tmp_path)
    assert [step.id for step in plan.steps].count("suite-prepare") == 0
    assert any("already pinned to docker" in note for note in plan.notes)


def test_an_e2b_suite_is_converted_and_committed_for_docker(tmp_path):
    suite = _mini_suite(tmp_path, kind="e2b")
    plan = _plan(tmp_path, suite=suite)
    assert "suite-prepare" in [step.id for step in plan.steps]
    note = next(n for n in plan.notes if "not pinned to docker" in n)
    assert "commits" in note and "tbench-pilot" not in note


def test_a_docker_run_from_an_e2b_suite_uses_the_derived_copy(tmp_path, monkeypatch):
    """The path a real user walks: a repo suite pinned to e2b, docker selected."""
    runner = FakeRunner()
    monkeypatch.setattr(qs, "scored_counts", lambda store, run_id: (1, 1))
    monkeypatch.setattr(qs, "control_package_root", lambda **_: tmp_path / "ctrl")
    workspace = tmp_path / "ws"
    result = qs.run_quickstart(
        backend="docker",
        suite=REPO / "suites" / "tbench-pilot",
        agents_dir=REPO / "agents",
        workspace=workspace,
        env={},
        execute=True,
        echo=lambda _line: None,
        runner=runner,
    )
    assert result.exit_code == 0
    assert "suite-prepare" in result.steps_run
    derived = workspace / "suites" / "tbench-pilot-docker"
    assert (derived / "jobs" / "tbench-m0.yaml").is_file()
    run_argv = next(a for a in runner.calls if "run" in a)
    assert run_argv[run_argv.index("--suite") + 1] == str(derived)
    status = subprocess.run(
        ["git", "-C", str(workspace / "suites"), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", "the derived tree must be committed"


def test_a_suite_whose_tasks_forbid_network_is_refused_for_docker(tmp_path):
    with pytest.raises(qs.QuickstartError) as caught:
        _plan(tmp_path, suite=REPO / "suites" / "e2e-hello")
    assert "no-network" in str(caught.value)
    assert caught.value.hint and "tbench-pilot" in caught.value.hint


def test_the_chosen_suite_is_reported_when_none_is_named(tmp_path):
    # the real pilot suite allows 193.126.4.2, which the fake host reports
    plan = _plan(tmp_path, suite=None)
    assert plan.suite.name.startswith("tbench-pilot")
    assert plan.state is not None and plan.state.address == "193.126.4.2"


# --------------------------------------------------------------------------- #
# 3. a run that scored nothing is a failure
# --------------------------------------------------------------------------- #


def _execute(tmp_path, monkeypatch, *, counts, runner=None, workspace=None, **overrides):
    suite = _mini_suite(tmp_path, kind="docker")
    monkeypatch.setattr(qs, "_run_id_of", lambda run_dir: f"run-{Path(run_dir).name}")
    monkeypatch.setattr(qs, "scored_counts", lambda store, run_id: counts)
    monkeypatch.setattr(qs, "control_package_root", lambda **_: tmp_path / "ctrl")
    workspace = workspace or tmp_path / "ws"
    result = qs.run_quickstart(
        backend="docker",
        suite=overrides.pop("suite", suite),
        agents_dir=REPO / "agents",
        workspace=workspace,
        env={},
        execute=True,
        echo=lambda _line: None,
        runner=runner or FakeRunner(),
    )
    return result, workspace


def test_a_run_that_scored_nothing_exits_non_zero(tmp_path, monkeypatch):
    result, workspace = _execute(tmp_path, monkeypatch, counts=(0, 3))
    assert result.exit_code == qs.EXIT_NO_VALID_TRIALS
    assert result.scored_line() == "0/3 trial(s) scored"
    # the artifacts of a failed run are still there to diagnose
    assert (workspace / "report.md").is_file()


def test_a_run_that_scored_exits_zero(tmp_path, monkeypatch):
    result, workspace = _execute(tmp_path, monkeypatch, counts=(2, 3))
    assert result.exit_code == 0
    assert result.scored_line() == "2/3 trial(s) scored"
    assert (workspace / "report.md").read_text(encoding="utf-8").startswith("# report")
    state = json.loads((workspace / "quickstart.json").read_text(encoding="utf-8"))
    assert state["valid"] == 2 and state["total"] == 3
    assert state["run_dir"].endswith("/ws/run")


def test_a_complete_run_is_reported_again_not_re_run(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    run_dir = workspace / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"run_id": "run-9"}), encoding="utf-8"
    )
    (workspace / "run.sqlite3").write_text("", encoding="utf-8")
    monkeypatch.setattr(qs, "scored_counts", lambda store, run_id: (1, 1))
    runner = FakeRunner()
    result = qs.run_quickstart(
        backend="docker",
        suite=_mini_suite(tmp_path),
        agents_dir=REPO / "agents",
        workspace=workspace,
        env={},
        execute=True,
        echo=lambda _line: None,
        runner=runner,
    )
    assert result.exit_code == 0
    assert "run" not in runner.subcommands(), "a complete run must not be run again"
    assert "report" in runner.subcommands()
    assert (workspace / "report.md").is_file()


def test_every_stage_the_runner_sees_is_the_documented_command(tmp_path, monkeypatch):
    runner = FakeRunner()
    _execute(tmp_path, monkeypatch, counts=(1, 1), runner=runner)
    subs = runner.subcommands()
    assert subs == ["docker", "run", "report", "dashboard", "recompute"]
    run_argv = next(a for a in runner.calls if "run" in a)
    assert "--suite" in run_argv and "--agents-dir" in run_argv and "--harbor-cli" in run_argv
    assert run_argv[-1] == "--force-build"        # the first run builds the template
    images_env = next(e for a, e in zip(runner.calls, runner.envs) if "run" in a)
    assert images_env["AEVAL_RUNTIME_IMAGES"] == str(REPO / "agents" / "_runtime" / "images.yaml")


def test_a_second_run_after_a_failure_gets_a_fresh_directory(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / "run").mkdir(parents=True)
    (workspace / "run" / "run_manifest.json").write_text(
        json.dumps({"run_id": "run-run"}), encoding="utf-8"
    )
    monkeypatch.setattr(qs, "scored_counts", lambda store, run_id: (0, 1))
    runner = FakeRunner()
    monkeypatch.setattr(qs, "control_package_root", lambda **_: tmp_path / "ctrl")
    monkeypatch.setattr(qs, "scored_counts", lambda store, run_id: (1, 1))
    result = qs.run_quickstart(
        backend="docker", suite=_mini_suite(tmp_path), agents_dir=REPO / "agents",
        workspace=workspace, env={}, execute=True, echo=lambda _line: None, runner=runner,
    )
    assert result.exit_code == 0
    run_argv = next(a for a in runner.calls if "run" in a)
    assert run_argv[run_argv.index("--run-dir") + 1].endswith("/ws/run-2")


# --------------------------------------------------------------------------- #
# e2b: refuse clearly, never fabricate a report
# --------------------------------------------------------------------------- #


def test_e2b_without_a_credential_names_it_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aeval.provenance.verify_e2b_backend", lambda *args, **kwargs: "1.0.0"
    )
    workspace = tmp_path / "ws-e2b"
    with pytest.raises(qs.QuickstartError) as caught:
        qs.run_quickstart(
            backend="e2b",
            suite=_mini_suite(tmp_path, kind="e2b"),
            agents_dir=REPO / "agents",
            workspace=workspace,
            env={},
            execute=True,
            echo=lambda _line: None,
            runner=FakeRunner(),
        )
    assert "E2B_API_KEY" in str(caught.value)
    assert not workspace.exists()
    assert not (workspace / "report.md").exists()


def test_e2b_without_the_sdk_names_the_install(tmp_path, monkeypatch):
    from aeval.provenance import BackendNotAvailableError

    def missing(*args, **kwargs):
        raise BackendNotAvailableError("the e2b backend is not installed")

    monkeypatch.setattr("aeval.provenance.verify_e2b_backend", missing)
    with pytest.raises(qs.QuickstartError) as caught:
        qs.run_quickstart(
            backend="e2b", suite=_mini_suite(tmp_path, kind="e2b"),
            agents_dir=REPO / "agents", workspace=tmp_path / "ws", env={},
            execute=True, echo=lambda _line: None, runner=FakeRunner(),
        )
    assert "e2b backend is not installed" in str(caught.value)


def test_e2b_does_not_probe_this_host_s_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aeval.provenance.verify_e2b_backend", lambda *args, **kwargs: "1.0.0"
    )
    runner = FakeRunner()
    with pytest.raises(qs.QuickstartError):
        qs.run_quickstart(
            backend="e2b", suite=_mini_suite(tmp_path, kind="e2b"),
            agents_dir=REPO / "agents", workspace=tmp_path / "ws",
            env={"E2B_API_KEY": "k"}, execute=True,
            echo=lambda _line: None, runner=runner,
        )
    assert "docker" not in runner.subcommands()


# --------------------------------------------------------------------------- #
# every hint must be actionable: the variables and commands it names are real
# --------------------------------------------------------------------------- #

SRC = REPO / "src" / "aeval"

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
_ENV_TOKEN = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")


def _quickstart_source() -> str:
    return Path(qs.__file__).read_text(encoding="utf-8")


def _string_value(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else "{...}"
            for value in node.values
        )
    return None


def _module_constants(tree) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                constants[node.targets[0].id] = value.value
    return constants


def _hint_literals() -> list[tuple[int, str]]:
    """Every ``QuickstartError`` hint that is (partly) a literal, with its line."""
    tree = ast.parse(_quickstart_source())
    hints: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "QuickstartError":
            continue
        argument = node.args[1] if len(node.args) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "hint":
                argument = keyword.value
        text = _string_value(argument)
        if text:
            hints.append((node.lineno, text))
    return hints


def _names_a_hint_can_print() -> set[str]:
    """Env-var names a hint prints, literal or through a module constant."""
    tree = ast.parse(_quickstart_source())
    constants = _module_constants(tree)
    names: set[str] = set()
    for _line, hint in _hint_literals():
        names.update(name for name in _ENV_TOKEN.findall(hint) if _ENV_NAME.match(name))
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for value in node.values:
            if not isinstance(value, ast.FormattedValue) or not isinstance(value.value, ast.Name):
                continue
            resolved = constants.get(value.value.id, "")
            if _ENV_NAME.match(resolved):
                names.add(resolved)
    return names


def _is_actually_read(name: str, sources: dict[Path, str]) -> bool:
    literal_reads = (
        f'env.get("{name}"', f'environ.get("{name}"', f'getenv("{name}"',
        f'environ["{name}"]', f"env.get('{name}'",
    )
    for path, text in sources.items():
        if any(pattern in text for pattern in literal_reads):
            return True
        # or read through a module constant that holds exactly this name
        # or read through a module constant that holds exactly this name and is
        # used where the environment is consulted (an adapter may resolve it in
        # a helper, not inline — `env=SOME_ROOT_ENV`, `environ.get(SOME_ENV)`)
        for constant, value in _module_constants(ast.parse(text)).items():
            if value != name:
                continue
            for line in text.splitlines():
                if constant in line and any(
                    token in line for token in ("environ", "env", "getenv", "setting")
                ):
                    return True
    return False


def test_every_variable_a_hint_names_is_really_read():
    """The ratchet: a hint may not name a variable no code reads.

    This is exactly the defect that shipped: a hint told the operator to export
    a variable that appeared nowhere else in the tree, so following the hint
    changed nothing and the same failure came back.
    """
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
    }
    unread = sorted(
        name for name in _names_a_hint_can_print()
        if not _is_actually_read(name, sources)
    )
    assert not unread, (
        "these variables are printed in a quickstart hint but read by no code: "
        + ", ".join(unread)
    )


def _click_commands() -> dict:
    import typer.main

    from aeval.cli import app

    return dict(typer.main.get_command(app).commands)


def test_every_command_and_flag_a_hint_names_exists():
    """A hint is copy-pasteable only if its subcommand and flags exist."""
    commands = _click_commands()
    known_commands = set(commands)
    offenders: list[str] = []
    for line, hint in _hint_literals():
        for command in re.findall(r"\baeval\s+([a-z][a-z0-9-]*)", hint):
            if command not in known_commands:
                offenders.append(f"quickstart.py:{line} names `aeval {command}`")
                continue
            available = {
                option
                for param in commands[command].params
                for option in (getattr(param, "opts", None) or [])
            }
            for flag in re.findall(r"\s(--[a-z][a-z0-9-]+)", hint):
                if flag not in available:
                    offenders.append(
                        f"quickstart.py:{line} names `{flag}` for `aeval {command}`"
                    )
    assert not offenders, "\n".join(offenders)


def test_the_missing_control_package_hint_names_a_variable_that_is_read(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(qs, "control_package_root", lambda **_: None)
    monkeypatch.setattr(qs, "scored_counts", lambda store, run_id: (1, 1))
    with pytest.raises(qs.QuickstartError) as caught:
        qs.run_quickstart(
            backend="docker", suite=_mini_suite(tmp_path), agents_dir=REPO / "agents",
            workspace=tmp_path / "ws", env={}, execute=True,
            echo=lambda _line: None, runner=FakeRunner(),
        )
    hint = caught.value.hint or ""
    assert qs.CONTROL_ROOT_ENV in hint
    assert "aeval doctor" in hint
    assert not re.search(r"\bDSH_CONTROL_ROOT\b", hint), (
        "that variable is read by no code: naming it is a dead end"
    )
    assert not re.search(r"\bDSH_CONTROL_ROOT\b", str(caught.value))


# --------------------------------------------------------------------------- #
# the pieces that make the wiring correct
# --------------------------------------------------------------------------- #


def test_the_host_address_is_read_the_way_this_platform_does(tmp_path):
    calls: list[list[str]] = []

    def fake(argv, *, cwd=None, env=None, capture=False):
        calls.append(list(argv))
        if argv[0] == "ipconfig":
            return qs.Completed(0 if argv[2] == "en0" else 1, "10.0.0.7\n", "")
        return qs.Completed(0, "10.0.0.9\n", "")

    assert qs.host_ip(platform="darwin", runner=fake) == "10.0.0.7"
    assert calls[0][0] == "ipconfig"
    calls.clear()
    assert qs.host_ip(platform="linux", runner=fake) == "10.0.0.9"
    assert calls[0][:2] == ["hostname", "-I"]


def test_the_broker_binds_an_address_the_suite_allows(tmp_path):
    suite = _mini_suite(tmp_path, allowed_hosts=["193.126.4.2", "registry.npmjs.org"])
    plan = _plan(tmp_path, suite=suite)
    assert plan.state is not None and plan.state.address == "193.126.4.2"
    assert any("lets the sandbox dial" in note for note in plan.notes)


def test_a_suite_that_allows_no_local_address_is_refused(tmp_path):
    suite = _mini_suite(tmp_path, allowed_hosts=["203.0.113.9"])

    def runner(argv, *, cwd=None, env=None, capture=False):
        if Path(argv[0]).name == "ipconfig":
            return qs.Completed(0, "10.1.2.3\n", "")
        if Path(argv[0]).name == "hostname":
            return qs.Completed(0, "10.1.2.3\n", "")
        return qs.Completed(0)

    with pytest.raises(qs.QuickstartError) as caught:
        qs.plan_quickstart(
            backend="docker", suite=suite, agents_dir=REPO / "agents",
            workspace=tmp_path / "ws", env={}, runner=runner,
        )
    message = str(caught.value)
    assert "203.0.113.9" in message and "10.1.2.3" in message
    assert caught.value.hint and "--listen-host" in caught.value.hint


def test_an_explicit_listen_host_overrides_the_allowlist(tmp_path):
    suite = _mini_suite(tmp_path, allowed_hosts=["203.0.113.9"])
    plan = _plan(tmp_path, suite=suite, listen_host="198.51.100.7")
    assert plan.state is not None and plan.state.address == "198.51.100.7"


def test_no_routable_address_is_a_named_refusal(tmp_path):
    def nothing(argv, *, cwd=None, env=None, capture=False):
        return qs.Completed(1, "", "no address")

    with pytest.raises(qs.QuickstartError) as caught:
        qs.host_ip(platform="linux", runner=nothing)
    assert "address" in str(caught.value)


def test_the_trusted_counter_answers_a_byte_length_bound():
    counter = qs.TokenCounter.start(0)
    try:
        body = json.dumps({"messages": [{"role": "user", "content": "x" * 500}]}).encode()
        import urllib.request

        request = urllib.request.Request(counter.endpoint, data=body, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["inputTokens"] == len(body)
        assert counter.port != 0
    finally:
        counter.stop()


def test_a_taken_counter_port_is_substituted_not_fatal():
    import socket

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        counter = qs.TokenCounter.start(port)
    finally:
        taken.close()
    try:
        assert counter.requested == port
        assert counter.port != port
        assert str(counter.port) in counter.endpoint
    finally:
        counter.stop()


def test_the_written_broker_spec_is_one_the_parser_accepts(tmp_path, monkeypatch):
    from aeval.hooks.broker_lifecycle import parse_broker_spec

    workspace = tmp_path / "ws"
    tls = qs._write_tls_material(workspace, "10.0.0.5")
    control = tmp_path / "ctrl"
    (control / "dist").mkdir(parents=True)
    for name in ("broker_main.js", "session_reader.js"):
        (control / "dist" / name).write_text("// built\n", encoding="utf-8")
    spec = qs.build_broker_spec(
        address="10.0.0.5",
        token_count_endpoint="http://127.0.0.1:8792/tokens/count",
        tls=tls,
        suite=REPO / "suites" / "tbench-pilot",
        control_root=control,
    )
    for key in ("controlRoot", "controlDist", "controlCa", "listenHost", "listenPort",
                "listenTls", "upstream", "identity", "limits", "maxOutputTokens",
                "tokenCount"):
        assert key in spec, key
    assert spec["upstream"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"
    assert "DEEPSEEK_API_KEY" not in json.dumps(spec).replace(
        spec["upstream"]["apiKeyEnv"], ""
    ), "no credential value may be written into the workspace"
    path = workspace / "broker.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("AEVAL_BROKER_JSON", str(path))
    parsed = parse_broker_spec()
    assert parsed is not None and parsed.control_dist == control / "dist"
    assert parsed.limits == {"maxSteps": 60, "maxTokens": 2000000}


def test_the_spec_takes_the_suite_s_budget_when_it_declares_one(tmp_path):
    spec = qs.build_broker_spec(
        address="10.0.0.5",
        token_count_endpoint="http://127.0.0.1:8792/tokens/count",
        tls=qs._write_tls_material(tmp_path / "ws2", "10.0.0.5"),
        suite=REPO / "suites" / "tbench-pilot",
        control_root=None,
    )
    assert spec["limits"] == {"maxSteps": 60, "maxTokens": 2000000}
    assert spec["auxiliaryPolicy"] == {"compaction": "allow"}


def test_an_unknown_backend_is_refused(tmp_path):
    with pytest.raises(qs.QuickstartError) as caught:
        _plan(tmp_path, backend="firecracker")
    assert "unknown backend" in str(caught.value)


def test_an_empty_agents_dir_is_initialized_as_part_of_the_one_command(tmp_path):
    agents = tmp_path / "empty-agents"
    plan = _plan(tmp_path, agents_dir=agents)
    assert "agents-init" in [step.id for step in plan.steps]
    assert not agents.exists(), "a dry run must not write the declarations either"


# --------------------------------------------------------------------------- #
# conversion: a committed, self-contained docker tree
# --------------------------------------------------------------------------- #


def test_the_conversion_commits_a_self_contained_tree(tmp_path):
    source = REPO / "suites"
    suites_root = tmp_path / "suites"
    suites_root.mkdir()
    subprocess.run(["cp", "-a", str(source / "_base"), str(suites_root / "_base")], check=True)
    subprocess.run(
        ["cp", "-a", str(source / "tbench-pilot"), str(suites_root / "tbench-pilot")],
        check=True,
    )
    workspace = tmp_path / "ws"
    converted = qs.convert_suite_for_docker(
        suites_root / "tbench-pilot", "/root/dshpkg", workspace=workspace
    )
    assert converted == workspace / "suites" / "tbench-pilot-docker"
    assert (workspace / "suites" / "_base" / "harbor.base.yaml").is_file()
    import yaml

    job = yaml.safe_load((converted / "jobs" / "tbench-m0.yaml").read_text(encoding="utf-8"))
    assert job["environment"]["type"] == "docker"
    assert job["n_attempts"] == 1
    assert job["agents"][0]["kwargs"]["install_prefix"] == "/root/dshpkg"
    status = subprocess.run(
        ["git", "-C", str(workspace / "suites"), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    assert status.stdout.strip() == "", "a run refuses a suite with uncommitted files"
    head = subprocess.run(
        ["git", "-C", str(workspace / "suites"), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    assert head.stdout.strip()


def test_a_conversion_already_present_is_reused(tmp_path):
    suites_root = tmp_path / "suites"
    source = suites_root / "mini"
    source.mkdir(parents=True)
    (source / "suite.yaml").write_text("schema_version: 2\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    target = qs.docker_suite_dir(source, workspace)
    target.mkdir(parents=True)
    (target / "hand-edited.txt").write_text("mine", encoding="utf-8")
    reused = qs.convert_suite_for_docker(source, "/root/dshpkg", workspace=workspace)
    assert reused == target
    assert (reused / "hand-edited.txt").is_file(), "a hand-edited copy is never overwritten"


# --------------------------------------------------------------------------- #
# the success criterion itself
# --------------------------------------------------------------------------- #


def test_scored_counts_uses_the_reliability_classifier(tmp_path, runtime_lock):
    from aeval.contracts import OverlayIdentity, RunManifest, VersionsBundle
    from aeval.store.sqlite import TrialStore

    store_path = tmp_path / "run.sqlite3"
    store = TrialStore(store_path)
    try:
        store.create_run(
            RunManifest(
                run_id="run-1",
                runtime_lock=runtime_lock,
                overlay=OverlayIdentity(
                    suite_id="mini", suite_version="1.0.0",
                    overlay_digest="d" * 64, source_commit="9" * 40,
                ),
                versions=VersionsBundle(aeval_version="0.3.0"),
            )
        )
        store.persist_trial(
            TrialRecord(
                trial_id="t-ok",
                coordinates={
                    "run_id": "run-1", "suite_id": "mini", "suite_version": "1.0.0",
                    "task_id": "hello", "trial_index": 0,
                },
                stop_reason="agent_exit_0",
                verdict="pass",
            )
        )
        store.persist_trial(
            TrialRecord(
                trial_id="t-infra",
                coordinates={
                    "run_id": "run-1", "suite_id": "mini", "suite_version": "1.0.0",
                    "task_id": "hello", "trial_index": 1,
                },
                stop_reason="infra_error",
                verdict=None,
            )
        )
    finally:
        store.close()
    assert qs.scored_counts(store_path, "run-1") == (1, 2)


def test_an_unreadable_previous_run_does_not_count_as_complete(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "run").mkdir(parents=True)
    (workspace / "run" / "run_manifest.json").write_text("{not json", encoding="utf-8")
    (workspace / "run.sqlite3").write_text("", encoding="utf-8")
    assert qs._completed_run(workspace) is None
