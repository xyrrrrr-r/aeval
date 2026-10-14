"""Offline validation of the terminal-beach suite.

terminal-beach is the deepAgent arm of the terminal-bench tasks
(`suites/tbench-pilot` is the DSH arm). Everything checkable without a
sandbox: suite/job composition, the deepAgent pairing (capabilities,
session slot, scope roots), the vendored-task integrity contract (what was
adapted, what is verbatim), budget parity between the broker spec and the
trajectory grader, the content-addressed reward grader, and the file-only
observable contract.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re

import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from aeval.agents.deepagent.agent import _DEEPAGENTS_CODE_VERSION, DcodeAgent
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite

SUITE = Path(__file__).parents[2] / "suites" / "terminal-beach"
REPO = Path(__file__).parents[2]

ROSTER = ("hello-world", "openssl-selfsigned-cert", "sqlite-db-truncate")
# The ONLY files this suite is allowed to rewrite relative to the upstream
# copy: the environment block, the image reference, the collector's session
# slot, and the offline verifier setup script.
ADAPTED_FILES = {
    "task.toml",
    "environment/Dockerfile",
    "tests/setup-uv-pytest.sh",
    "tests/test.sh",
}
# Files upstream ships as build-context entries that this suite inlines into
# the Dockerfile instead (the e2b template build API rejects build-context
# uploads): they are absent from the task, and the Dockerfile asserts their
# sha256 at build time. Documented in tbench-pilot/PROVENANCE.md item 3.
INLINED_FILES = {
    "environment/trunc.db",
}
# Byte-identical to upstream, asserted below.
VERBATIM_FILES = (
    "instruction.md",
    "tests/test_outputs.py",
    "tests/run-uv-pytest.sh",
    "solution/solve.sh",
)
SOURCE_COMMIT = "91e10457b5410f16c44364da1a34cb6de8c488a5"
BROKER_HOST = "193.126.4.2"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE)


@pytest.fixture(scope="module")
def budgets():
    return yaml.safe_load((SUITE / "budgets.yaml").read_text("utf-8"))


def _task(name: str) -> Path:
    return SUITE / "tasks" / name


def _task_toml(name: str) -> str:
    return (_task(name) / "task.toml").read_text("utf-8")


# --- composition and the deepAgent pairing -----------------------------


def test_suite_loads_and_composes(suite):
    job = compose_harbor_job(suite)
    assert job.job_name == "terminal-beach"
    assert len(job.tasks) == len(ROSTER)
    assert job.n_attempts == 3
    assert job.n_concurrent_trials == 1


def test_job_targets_the_e2b_backend(suite):
    """The suite was validated on e2b; an implicit docker fallback would
    change how the declared allowlist is enforced."""
    job = compose_harbor_job(suite)
    assert job.environment.type.value == "e2b"


def test_deepagent_pairing_injects_the_declared_adapter(suite):
    """`--agent deepagent` must resolve the declaration into the pinned
    adapter entry (not the job file's `nop` placeholder)."""
    job = compose_harbor_job(
        suite, agent="deepagent", lease_model="deepseek/deepseek-chat",
        agents_root=REPO / "agents",
    )
    entry = job.agents[0]
    assert entry.import_path == "aeval.agents.deepagent.agent:DcodeAgent"
    assert entry.model_name == "deepseek/deepseek-chat"
    assert entry.override_setup_timeout_sec == 900.0


def test_the_suite_only_requires_capabilities_deepagent_provides(suite):
    require = set(suite.overlay.driver.require)
    assert require == {"acp_stdio", "shell", "file_tools"}
    missing = require - set(DcodeAgent.PROVIDES)
    assert not missing, f"capability gate would refuse deepAgent: {missing}"


def test_session_slot_is_the_generic_agent_session_record(suite):
    """deepAgent publishes Harbor's ACP runner summary, not a DSH session
    file; the suite must declare that fact instead of inheriting a default."""
    assert suite.overlay.driver.session_record == "agent_session_record"


def test_workspace_dir_is_the_directory_the_tests_hardcode(suite):
    assert suite.overlay.driver.workspace_dir == "/app"


def test_scope_roots_include_the_declared_workspace():
    """The preset scratch roots do not contain /app; without the override
    every legitimate write would be an integrity violation (veto)."""
    module = _load_module(
        "tb_trajectory", SUITE / "graders" / "tb_trajectory.py"
    )
    assert "/app" in module.SCOPE_ROOTS
    built = {mid: factory({}) for mid, factory in module.METRICS.items()}
    assert "/app" in built["scope_discipline"].allowed_prefixes


def test_integrity_metrics_are_declared_as_integrity(suite):
    catalog = {m.id: m for m in suite.overlay.verdict.metric_catalog}
    assert catalog["forbidden_access"].severity == "integrity"
    assert catalog["scope_discipline"].severity == "integrity"
    assert catalog["step_efficiency"].severity == "score"


# --- observables and baselines ----------------------------------------


def test_observables_are_file_sources_only(suite):
    for observable in suite.overlay.observables:
        assert observable.source.startswith("file:")
    assert {o.name for o in suite.overlay.observables} == {"ready", "reward"}


def test_baselines_probe_only_declared_observables(suite):
    names = {o.name for o in suite.overlay.observables}
    for baseline in suite.overlay.baselines:
        probe = baseline.probe
        assert probe.startswith("observable:")
        assert probe.split(":", 1)[1] in names


def test_suite_stages_the_task_tests_before_collection(suite):
    """Harbor uploads each task's tests only at verification time — after
    the agent stopped — so the suite must ask aeval to stage them."""
    assert suite.overlay.driver.stage_tests_before_collect is True


# --- the vendored tasks ------------------------------------------------


def test_every_vendored_task_is_present_and_parses():
    assert sorted(p.name for p in (SUITE / "tasks").iterdir() if p.is_dir()) == \
        sorted(ROSTER)
    for name in ROSTER:
        assert (_task(name) / "task.toml").is_file()
        assert (_task(name) / "instruction.md").is_file()
        assert (_task(name) / "tests" / "test.sh").is_file()
        assert (_task(name) / "solution" / "solve.sh").is_file()


def test_only_the_declared_files_differ_from_the_upstream_copy():
    """The vendoring promise: only the four adapted files move. Everything
    else (instructions, the task's own tests, the oracle solution) is
    byte-identical to the upstream terminal-bench copy."""
    for name in ROSTER:
        upstream = SUITE / "vendor" / "upstream" / name
        task = _task(name)
        assert upstream.is_dir(), f"missing upstream reference for {name}"
        for path in sorted(upstream.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(upstream).as_posix()
            if relative in ADAPTED_FILES or relative in INLINED_FILES:
                continue
            counterpart = task / relative
            assert counterpart.is_file(), f"{name}/{relative} disappeared"
            assert sha256(counterpart.read_bytes()).digest() == \
                sha256(path.read_bytes()).digest(), f"{name}/{relative} drifted"


def test_the_carried_over_files_are_verbatim():
    for name in ROSTER:
        for relative in VERBATIM_FILES:
            upstream = SUITE / "vendor" / "upstream" / name / relative
            assert upstream.is_file(), f"missing upstream {name}/{relative}"
            assert sha256((_task(name) / relative).read_bytes()).digest() == \
                sha256(upstream.read_bytes()).digest(), f"{name}/{relative}"


def test_tasks_keep_the_adapted_egress():
    for name in ROSTER:
        text = _task_toml(name)
        assert 'network_mode = "allowlist"' in text
        assert f'allowed_hosts = ["{BROKER_HOST}"]' in text, (
            f"{name}: deepAgent's CLI is baked into the image, so the npm "
            "egress grants tbench-pilot needed must not be carried over"
        )
        assert "registry.npmjs.org" not in text


def test_each_task_pins_its_own_digest_built_image():
    """`docker_image` and the Dockerfile are TWO levels: the backend uses the
    image verbatim, so the task image — not the Dockerfile — is what runs."""
    images = {}
    for name in ROSTER:
        text = _task_toml(name)
        reference = [
            ln for ln in text.splitlines() if ln.startswith("docker_image")
        ][0].split('"')[1]
        assert reference.endswith("sha256:" + reference.rsplit("@sha256:", 1)[-1])
        assert len(reference.rsplit("@sha256:", 1)[-1]) == 64, name
        assert f"terminal-beach-{name}@" in reference, (
            f"{name}: the pin must name this task's image, not the shared base "
            "— a shared image cannot carry per-task inputs"
        )
        images[name] = reference
    # hello-world and openssl need no task inputs, so their images are the base
    # plus ENV/WORKDIR and legitimately share one content digest; what must NOT
    # be shared is the task that carries an input.
    assert images["sqlite-db-truncate"] != images["hello-world"], (
        "the sqlite image carries trunc.db, so its digest cannot equal the "
        "input-free task image"
    )


def test_task_dockerfiles_share_one_base_and_keep_the_runtime_contract():
    froms = {}
    for name in ROSTER:
        text = (_task(name) / "environment" / "Dockerfile").read_text("utf-8")
        from_line = [ln for ln in text.splitlines() if ln.startswith("FROM ")][0]
        froms[name] = from_line.removeprefix("FROM ").strip()
        assert "@sha256:" in froms[name], name
        assert "WORKDIR /app" in text, name
        assert "ENV TEST_DIR=/tests" in text, name
    assert len(set(froms.values())) == 1, "one shared runtime base"
    assert "terminal-beach-agent@" in froms["hello-world"]


def test_the_sqlite_task_input_is_baked_not_merely_copied():
    """Upstream COPYs trunc.db from the build context. The template build API
    cannot receive context files, so it is inlined — and because the Dockerfile
    is never executed on a trial, the input only exists if the task IMAGE was
    built from it. Assert both halves."""
    text = (_task("sqlite-db-truncate") / "environment" / "Dockerfile").read_text(
        "utf-8"
    )
    assert "base64 -d > /app/trunc.db" in text
    assert "sha256sum -c -" in text, "the inlined bytes are checked at build time"
    digest = re.search(r"echo '([0-9a-f]{64})  /app/trunc.db'", text)
    assert digest, "the self-check must name the expected digest"
    upstream = (
        SUITE / "vendor" / "upstream" / "sqlite-db-truncate" / "environment" / "trunc.db"
    )
    assert sha256(upstream.read_bytes()).hexdigest() == digest.group(1), (
        "the inlined copy drifted from the vendored upstream database"
    )
    assert "COPY trunc.db" not in text, "the build context is not available"


def test_verifier_runs_the_upstream_tests_then_collects_evidence():
    """Order matters: the upstream verifier publishes the reward, and the
    aeval collection follows even on failure so a failing trial still seals
    its evidence — with the deepAgent session slot, not the DSH one."""
    for name in ROSTER:
        text = _task_toml(name)
        command = [
            ln for ln in text.splitlines()
            if ln.strip().startswith("command = ")
        ][0]
        assert "bash /tests/test.sh" in command
        assert "agent_session_record" in command
        assert "dsh_session" not in command
        assert command.index("bash /tests/test.sh") < \
            command.index("aeval-collect")


def test_every_task_declares_a_wall_clock_the_suite_budget_covers(budgets):
    for name in ROSTER:
        text = _task_toml(name)
        assert "[agent]\ntimeout_sec = 900.0" in text, name
    assert budgets["agent"]["run_timeout_sec"] == 900


def test_provenance_pins_source_commit_and_license():
    for name in ROSTER:
        text = _task_toml(name)
        assert SOURCE_COMMIT in text
        assert 'license = "Apache-2.0"' in text


# --- budgets ----------------------------------------------------------


def test_budget_parity_between_broker_spec_and_trajectory_grader(budgets):
    """The broker enforces limits.maxSteps/maxTokens; the trajectory grader
    measures efficiency against its own thresholds. Drift makes every
    efficiency score measure the wrong budget."""
    module = _load_module(
        "tb_trajectory_budgets", SUITE / "graders" / "tb_trajectory.py"
    )
    built = {mid: factory({}) for mid, factory in module.METRICS.items()}
    assert built["step_efficiency"].max_steps == budgets["grader"]["max_steps"]
    assert built["token_efficiency"].max_tokens == budgets["grader"]["max_tokens"]
    assert budgets["broker"]["maxSteps"] == budgets["grader"]["max_steps"]
    assert budgets["broker"]["maxTokens"] == budgets["grader"]["max_tokens"]
    assert module.MAX_STEPS == budgets["grader"]["max_steps"]
    assert module.MAX_TOKENS == budgets["grader"]["max_tokens"]


def test_declared_budget_matches_the_suite_contract(suite, budgets):
    budget = suite.overlay.budget
    assert budget is not None
    assert budget.max_steps == budgets["broker"]["maxSteps"]
    assert budget.max_tokens == budgets["broker"]["maxTokens"]
    # The suite's spend window is the per-trial agent wall clock plus setup.
    assert budget.max_seconds == 1800.0
    assert budget.enforcement_point == "gateway_lease"
    assert DcodeAgent.BUDGET_ENFORCEMENT == "gateway_lease"


def test_broker_spec_generator_injects_budget_and_the_responses_wire(
    tmp_path, budgets
):
    """The upstream wire protocol is a suite fact: dcode posts the OpenAI
    Responses API, so a spec left at the chat-completions default would send
    every model call to the wrong provider path."""
    base = {
        "brokerJs": "/opt/dist/broker_main.js",
        "listenPort": 8447,
        "listenHost": BROKER_HOST,
        "limits": {"maxSteps": 8},
        "maxOutputTokens": 512,
        "upstream": {"provider": "deepseek", "model": "deepseek-chat",
                     "baseUrl": "https://api.deepseek.com/v1",
                     "apiKeyEnv": "DEEPSEEK_API_KEY"},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), "utf-8")
    out_path = tmp_path / "terminal-beach.json"
    result = subprocess.run(
        [sys.executable, str(SUITE / "tools" / "gen_broker_spec.py"),
         "--budgets", str(SUITE / "budgets.yaml"),
         "--base", str(base_path), "--out", str(out_path),
         "--token-count-endpoint", "http://127.0.0.1:8792/tokens/count"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    spec = json.loads(out_path.read_text("utf-8"))
    assert spec["limits"]["maxSteps"] == budgets["broker"]["maxSteps"]
    assert spec["limits"]["maxTokens"] == budgets["broker"]["maxTokens"]
    assert spec["maxOutputTokens"] == budgets["broker"]["maxOutputTokens"]
    assert spec["upstream"]["protocol"] == "responses"
    assert spec["upstream"]["baseUrl"] == "https://api.deepseek.com"
    assert spec["auxiliaryPolicy"] == {"compaction": "allow"}
    # Everything not declared is carried over from the verified base verbatim.
    assert spec["listenPort"] == base["listenPort"]
    assert spec["upstream"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"


def test_the_declared_protocol_is_the_one_the_adapter_speaks(suite, budgets):
    from aeval.agents.contract import model_routing_of

    routing = model_routing_of(DcodeAgent)
    assert routing.agent_protocol == "openai_responses"
    assert budgets["broker"]["upstreamProtocol"] == "responses"


def test_the_pinned_cli_version_matches_the_adapter():
    """The dcode version is pinned in three places on purpose — the adapter
    constant, this suite's provenance record, and the image recipe's source
    image. A bump that touches one alone must fail here, not in a trial."""
    provenance = (SUITE / "PROVENANCE.md").read_text("utf-8")
    assert f"deepagents-code=={_DEEPAGENTS_CODE_VERSION}" in provenance
    image = (SUITE / "images" / "agent-image.Dockerfile").read_text("utf-8")
    assert "agentlab-deepagent@sha256:" in image
    recipe = (SUITE / "images" / "build-agent-image.sh").read_text("utf-8")
    assert "agentlab-deepagent@sha256:" in recipe
    assert "base-pilot@sha256:" in recipe


def test_runtime_image_table_has_a_row_for_this_pairing():
    table = yaml.safe_load(
        (REPO / "agents" / "_runtime" / "images.yaml").read_text("utf-8")
    )
    rows = [
        row for row in table["images"]
        if row["suite"] == "terminal-beach"
    ]
    assert len(rows) == 1
    assert rows[0]["runtime"] == "deepagents-code"


# --- the graders ------------------------------------------------------


def test_trajectory_guard_judges_events_without_the_token_ledger(suite):
    """A required-field gate is all-or-nothing. Demanding token_usage here
    would turn the whole trajectory layer into cannot_judge on an adapter
    that declares it partial — throwing away eight metrics that read only
    the event stream. token_efficiency reports its own honest skip."""
    module = _load_module("tb_trajectory_fields", SUITE / "graders" / "tb_trajectory.py")
    assert module.REQUIRED_FIELDS == ["events"]
    built = {mid: factory({}) for mid, factory in module.METRICS.items()}
    assert "token_efficiency" in built


def test_trajectory_grader_declares_exactly_the_catalog_gate_metrics(suite):
    module = _load_module(
        "tb_trajectory_ids", SUITE / "graders" / "tb_trajectory.py"
    )
    declared = [
        metric.id
        for guard in suite.overlay.verdict.guards
        for metric in (guard.metrics or [])
    ]
    assert declared == list(module.METRICS.keys())


def test_outcome_grader_matches_the_suite_declaration(suite):
    module = _load_module("tb_outcome", SUITE / "graders" / "tb_outcome.py")
    grader = suite.overlay.verdict.graders["default"]
    assert grader.impl == f"graders/tb_outcome.py@{module.GRADER_VERSION}"
    assert module.GRADER_ID == "terminal-beach-outcome"
    assert module.LAYER == grader.layer


def test_outcome_grader_requires_events_but_not_the_token_ledger(suite):
    """The ACP bridge yields ATIF steps without per-step usage (measured on
    the target host), and this adapter declares token_usage partial. Gating
    the OUTCOME on it would report cannot_judge for tasks that actually
    passed — the reward is a task fact, not a transcript accounting fact."""
    module = _load_module("tb_outcome_fields", SUITE / "graders" / "tb_outcome.py")
    assert module.REQUIRED_FIELDS == ["events"]
    assert DcodeAgent.TRANSCRIPT_CAPABILITY.fields_available["token_usage"] == \
        "partial"


def test_outcome_grader_scores_by_content_address():
    module = _load_module("tb_outcome_addr", SUITE / "graders" / "tb_outcome.py")

    class Ref:
        def __init__(self, sha256: str) -> None:
            self.sha256 = sha256

    class Record:
        def __init__(self, artifacts) -> None:
            self.artifacts = artifacts

    def grade(artifacts):
        return asyncio.run(module.grade(Record(artifacts)))

    missing = grade({})
    assert missing.status == "cannot_judge"
    assert not missing.score.valid

    passed = grade({"observable:reward": Ref(
        module.EXPECTED_ARTIFACT_SHA256["1"])})
    assert passed.status == "pass" and passed.score.value == 1.0

    failed = grade({"observable:reward": Ref(
        module.EXPECTED_ARTIFACT_SHA256["0"])})
    assert failed.status == "fail" and failed.score.value == 0.0

    malformed = grade({"observable:reward": Ref("0" * 64)})
    assert malformed.status == "cannot_judge"
    assert not malformed.score.valid


def test_reward_artifact_serialization_is_the_collectors():
    """The grader's expected digests are the collector's exact
    serialization; recompute them here so a serializer change is caught."""
    module = _load_module("tb_outcome_ser", SUITE / "graders" / "tb_outcome.py")
    for value in ("0", "1"):
        expected = sha256(json.dumps(
            {"name": "reward", "value": value},
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        assert module.EXPECTED_ARTIFACT_SHA256[value] == expected


def test_offline_setup_script_makes_no_network_calls():
    for name in ROSTER:
        text = (_task(name) / "tests" / "setup-uv-pytest.sh").read_text("utf-8")
        # Only executable lines count: the docstring explains what upstream
        # did (curl + astral.sh) before saying what this script does instead.
        body = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "curl" not in body
        assert "astral.sh" not in body
        assert "uv sync --offline" in body

