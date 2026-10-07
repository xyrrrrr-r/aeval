"""Offline validation of the terminal-bench-core pilot suite.

Everything checkable without a sandbox: suite/job composition, the
vendored-task integrity contract (what was adapted, what is verbatim),
budget parity between the broker spec and the trajectory grader, the
content-addressed reward grader, and the file-only observable contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from aeval.hooks.evidence import build_required_collect_plan, CONDITIONAL_OUTPUTS, FIXED_OUTPUT_PATHS, SESSION_RECORD_OUTPUTS
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.verdict.executor import grade_trial
from aeval.verdict.pipeline import build_trial_record, load_suite_graders
from aeval.verdict.progress import RequirementProgress
from aeval.suite_models import DriverSpec

SUITE = Path(__file__).parents[2] / "suites" / "tbench-pilot"
PROVENANCE_NOTE = (SUITE / "PROVENANCE.md").read_text("utf-8")

ROSTER = ("hello-world", "sqlite-db-truncate", "openssl-selfsigned-cert")
# The ONLY files the vendoring pipeline is allowed to rewrite.
ADAPTED_FILES = {
    "task.toml",
    "environment/Dockerfile",
    "tests/setup-uv-pytest.sh",
    "tests/test.sh",
}
SOURCE_COMMIT = "91e10457b5410f16c44364da1a34cb6de8c488a5"
ALLOWED_HOSTS = {"registry.npmjs.org", "nodejs.org", "193.126.4.2"}


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE)


@pytest.fixture(scope="module")
def budgets():
    return yaml.safe_load((SUITE / "budgets.yaml").read_text("utf-8"))


@pytest.fixture(scope="module")
def bases():
    return json.loads((SUITE / "images" / "bases.json").read_text("utf-8"))


def _task(name):
    return SUITE / "tasks" / name


# --- composition ------------------------------------------------------


def test_suite_loads_and_composes(suite):
    job = compose_harbor_job(suite)
    assert job.job_name == "tbench-m0"
    assert job.n_attempts == 3
    assert job.n_concurrent_trials == 1


def test_job_targets_the_e2b_backend(suite):
    """Regression (found on the aarch64 e2b host): without an explicit
    environment type the composed job falls back to the local docker
    backend, which cannot enforce the task's network policy."""
    from harbor.models.environment_type import EnvironmentType

    job = compose_harbor_job(suite)
    assert job.environment.type is EnvironmentType.E2B


def test_dsh_job_carries_the_verified_agent_settings():
    job = (SUITE / "jobs" / "tbench-m0.yaml").read_text("utf-8")
    assert "aeval.agents.dsh.agent:DshAgent" in job
    assert "install_prefix:" in job and "npm_cache:" in job
    assert "session_reader:" in job
    assert "\n    override_setup_timeout_sec: 900" in job
    assert "      override_setup_timeout_sec" not in job


def test_oracle_job_uses_harbors_builtin_agent():
    job = (SUITE / "jobs" / "tbench-oracle.yaml").read_text("utf-8")
    assert "- name: oracle" in job
    assert "n_attempts: 1" in job


def test_observables_are_file_sources_only(suite):
    for observable in suite.overlay.observables:
        assert observable.source.startswith("file:"), observable


def test_baselines_probe_only_declared_observables(suite):
    names = {o.name for o in suite.overlay.observables}
    assert {o.name for o in suite.overlay.observables} == {"ready", "reward"}
    for baseline in suite.overlay.baselines:
        kind, _, name = baseline.probe.partition(":")
        assert kind == "observable"
        assert name in names


# --- vendored tasks ---------------------------------------------------


def test_every_vendored_task_is_present_and_parses():
    for name in ROSTER:
        task_dir = _task(name)
        assert (task_dir / "task.toml").is_file(), name
        assert (task_dir / "instruction.md").is_file(), name
        import tomllib

        data = tomllib.loads((task_dir / "task.toml").read_text("utf-8"))
        assert data["environment"]["network_mode"] == "allowlist", name
        assert set(data["environment"]["allowed_hosts"]) == ALLOWED_HOSTS, name


def test_only_the_declared_files_differ_from_the_migration_output():
    """The adaptation must be exactly: environment block, verifier
    collect command, base image reference, offline verifier setup. Every
    other byte — instruction, tests, solution, task steps — is carried
    over verbatim, so a silent edit to a benchmark task is impossible."""
    for name in ROSTER:
        adapted_root = _task(name)
        upstream_root = SUITE / "vendor" / "upstream" / name
        adapted = {
            p.relative_to(adapted_root).as_posix(): p
            for p in adapted_root.rglob("*") if p.is_file()
        }
        upstream = {
            p.relative_to(upstream_root).as_posix(): p
            for p in upstream_root.rglob("*") if p.is_file()
        }
        inlined = _inlined_files(name)
        assert not set(adapted) - set(upstream), f"{name}: unexpected new file"
        # Task files that upstream COPYs are inlined into the Dockerfile
        # and removed from the build context (the e2b template build API
        # cannot accept build-context uploads), so they are the only
        # allowed absence — and their bytes are checked separately.
        missing = set(upstream) - set(adapted)
        assert missing == {f"environment/{item['file']}" for item in inlined}, (
            f"{name}: unexpected removed files {sorted(missing)}"
        )
        differing = {
            rel for rel in adapted
            if adapted[rel].read_bytes() != upstream[rel].read_bytes()
        }
        assert differing == ADAPTED_FILES, (
            f"{name}: unexpected adaptations {sorted(differing ^ ADAPTED_FILES)}"
        )


def _inlined_files(name: str) -> list[dict]:
    """Rows of the PROVENANCE "Inlined task files" table for one task."""
    import re

    rows = []
    for line in (SUITE / "PROVENANCE.md").read_text("utf-8").splitlines():
        match = re.match(
            r"\| `([^`]+)` \| `([^`]+)` \| (\d+) \| `([0-9a-f]{64})` \| `([^`]+)` \|",
            line.strip(),
        )
        if match and match.group(1) == name:
            rows.append({
                "task": match.group(1),
                "file": match.group(2),
                "bytes": int(match.group(3)),
                "sha256": match.group(4),
                "target": match.group(5),
            })
    return rows


def test_inlined_task_files_are_byte_identical_to_the_upstream_files():
    """A COPY replaced by an inline write must not change the bytes: the
    digest proven at build time is the upstream file's digest, and the
    image path is the one upstream's COPY produced."""
    import base64
    import hashlib

    roster_with_inlines = 0
    for name in ROSTER:
        inlined = _inlined_files(name)
        if inlined:
            roster_with_inlines += 1
        dockerfile = (_task(name) / "environment" / "Dockerfile").read_text("utf-8")
        for item in inlined:
            upstream = SUITE / "vendor" / "upstream" / name / "environment" / item["file"]
            data = upstream.read_bytes()
            assert len(data) == item["bytes"], item
            assert hashlib.sha256(data).hexdigest() == item["sha256"], item
            assert f"'{item['sha256']}  {item['target']}' | sha256sum -c -" in dockerfile
            encoded = base64.b64encode(data).decode("ascii")
            assert f"'{encoded}' | base64 -d > {item['target']}" in dockerfile
            # the file must be gone from the build context the e2b build
            # API scans
            assert not (_task(name) / "environment" / item["file"]).exists()
    assert roster_with_inlines >= 1, "the pilot roster must exercise inlining"


def test_adapted_dockerfiles_carry_no_build_time_network_install():
    """Build-time egress to archive.ubuntu.com is unusable here, and the
    adapted base already provides the tool: the install must be a
    build-time presence assertion instead."""
    for name in ROSTER:
        dockerfile = (_task(name) / "environment" / "Dockerfile").read_text("utf-8")
        executable = "\n".join(
            line for line in dockerfile.splitlines()
            if not line.strip().startswith("#")
        )
        assert "apt-get" not in executable, name
    openssl = (_task("openssl-selfsigned-cert") / "environment" / "Dockerfile").read_text("utf-8")
    assert "RUN openssl version" in openssl


def test_upstream_test_invocation_is_untouched():
    """run-uv-pytest.sh is the invocation that runs the task's tests; it
    must be byte-identical to the migration output. Only the
    infrastructure setup script is adapted for the offline sandbox."""
    for name in ROSTER:
        assert (_task(name) / "tests" / "run-uv-pytest.sh").read_bytes() == (
            SUITE / "vendor" / "upstream" / name / "tests" / "run-uv-pytest.sh"
        ).read_bytes(), name


def test_offline_setup_script_makes_no_network_calls():
    adaptation = (SUITE / "adaptation" / "setup-uv-pytest.sh").read_text("utf-8")
    executable = "\n".join(
        line for line in adaptation.splitlines()
        if not line.strip().startswith("#")
    )
    for forbidden in ("apt-get", "curl ", "astral.sh", "pip install"):
        assert forbidden not in executable, forbidden
    # The verifier resolves pytest from the wheelhouse baked at /opt/wheels
    # by the pinned verifier project; a cache/index resolution was NOT
    # enough on the image's Python 3.10 (uv still wanted index metadata
    # for tomli/exceptiongroup).
    assert "uv sync --offline" in adaptation
    assert "cp /opt/verifier/pyproject.toml" in adaptation
    for name in ROSTER:
        vendored = (_task(name) / "tests" / "setup-uv-pytest.sh").read_text("utf-8")
        assert vendored == adaptation, name


def test_verifier_runs_the_upstream_tests_then_collects_evidence(suite):
    import tomllib

    for name in ROSTER:
        data = tomllib.loads((_task(name) / "task.toml").read_text("utf-8"))
        collect = data["verifier"]["collect"]
        assert len(collect) == 1, name
        command = collect[0]["command"]
        # The reward directory exists first (the upstream test.sh writes
        # the reward file but never creates the directory), then the
        # task's own verifier runs, then evidence collection — and
        # collection must NOT be skipped when the tests fail, which is why
        # the steps are joined with `;` and not `&&`.
        assert command.startswith("mkdir -p /logs/verifier; bash /tests/test.sh; "), name
        assert "aeval-collect" in command, name
        assert "&&" not in command, name
        for output in build_required_collect_plan(suite):
            if output.startswith("observable:"):
                continue
            assert output in command, f"{name}: missing collect output {output}"


def test_verifier_timeout_matches_the_migration_output():
    import tomllib

    for name in ROSTER:
        upstream = tomllib.loads(
            (SUITE / "vendor" / "upstream" / name / "task.toml").read_text("utf-8")
        )
        adapted = tomllib.loads((_task(name) / "task.toml").read_text("utf-8"))
        assert adapted["verifier"]["timeout_sec"] == upstream["verifier"]["timeout_sec"]
        assert adapted["agent"]["timeout_sec"] == upstream["agent"]["timeout_sec"]


def test_provenance_pins_source_commit_and_license():
    import tomllib

    for name in ROSTER:
        data = tomllib.loads((_task(name) / "task.toml").read_text("utf-8"))
        provenance = data["metadata"]["provenance"]
        assert provenance["license"] == "Apache-2.0", name
        assert SOURCE_COMMIT in provenance["source"], name
        assert provenance["original_id"] == name
        assert provenance["data_imported"] is True
        assert provenance["rewritten_by_us"] is True
    provenance_md = (SUITE / "PROVENANCE.md").read_text("utf-8")
    assert SOURCE_COMMIT in provenance_md
    assert "Apache-2.0" in provenance_md


def test_task_images_come_from_the_digest_pinned_internal_mirror(bases):
    for name in ROSTER:
        dockerfile = (_task(name) / "environment" / "Dockerfile").read_text("utf-8")
        from_lines = [
            line for line in dockerfile.splitlines()
            if line.upper().startswith("FROM ")
        ]
        assert len(from_lines) == 1, name
        reference = from_lines[0].split(None, 1)[1].strip()
        # The HTTPS ingress, not the plain-HTTP registry port: the e2b
        # template builder pulls over HTTPS and the registry port answers
        # HTTP ("server gave HTTP response to HTTPS client"). The same
        # manifest is addressed as 193.126.4.2:2900/t-bench/... on push.
        assert reference.startswith("harbor:443/t-bench/"), reference
        assert "@sha256:" in reference, f"{name}: base image is not digest-pinned"
        digest = reference.split("@", 1)[1]
        assert len(digest) == len("sha256:") + 64, reference
        # no upstream registry reference survives in an executable line
        executable = "\n".join(
            line for line in dockerfile.splitlines()
            if not line.strip().startswith("#")
        )
        assert "ghcr.io" not in executable, name
        import tomllib

        data = tomllib.loads((_task(name) / "task.toml").read_text("utf-8"))
        assert "docker_image" not in data.get("environment", {}), (
            f"{name}: declaring environment.docker_image makes Harbor "
            "ignore the Dockerfile"
        )


def test_bases_json_matches_the_vendored_dockerfiles(bases):
    for name in ROSTER:
        dockerfile = (_task(name) / "environment" / "Dockerfile").read_text("utf-8")
        reference = next(
            line.split(None, 1)[1].strip()
            for line in dockerfile.splitlines()
            if line.upper().startswith("FROM ")
        )
        assert reference in bases.values(), (
            f"{name}: {reference} is not recorded in images/bases.json"
        )
    # ONE adapted base replaces both upstream bases: ghcr.io is not
    # pullable at image-build scale on the pilot cluster, so the mirrored
    # base is a superset of what the three tasks need. Both upstream keys
    # must therefore resolve to the same digest-pinned reference.
    assert len(bases) == 2, "each upstream base needs an explicit mapping"
    assert len(set(bases.values())) == 1, (
        "the pilot deliberately runs one adapted base for all three tasks"
    )
    assert all("@sha256:" in ref for ref in bases.values())


def test_base_image_recipe_self_tests_the_offline_verifier_path():
    """The adapted base must prove, at BUILD time, the exact offline path
    the task verifier takes; otherwise a wheelhouse regression surfaces
    inside a sealed trial instead of in the build."""
    recipe = (SUITE / "images" / "base-image.Dockerfile").read_text("utf-8")
    assert "COPY wheels/ /opt/wheels/" in recipe
    assert "COPY verifier/ /opt/verifier/" in recipe
    assert "ENV UV_OFFLINE=1" in recipe
    assert "uv sync --offline" in recipe
    assert "uv run pytest --version" in recipe
    # the readiness seed every baseline assertion depends on
    assert "> /workspace/ready" in recipe
    # the verifier project the image bakes is the one the setup script uses
    verifier = (SUITE / "images" / "verifier" / "pyproject.toml").read_text("utf-8")
    assert "pytest==9.1.1" in verifier, "pytest must be pinned exactly"
    assert 'find-links = ["/opt/wheels"]' in verifier
    assert 'environments = ["sys_platform == \'linux\'"]' in verifier
    setup = (SUITE / "adaptation" / "setup-uv-pytest.sh").read_text("utf-8")
    assert "cp /opt/verifier/pyproject.toml" in setup
    assert "uv sync --offline" in setup


def test_base_image_bakes_the_npm_cache_the_trial_install_needs():
    """The agent installs the DSH CLI with npm at trial time. This
    cluster's registry path is throttled to a few KB/s, and the first
    pilot trial died with `Agent setup timed out after 900.0 seconds`
    mid-install, so the image must carry a warm cache AND configure npm to
    use it without a network round trip; the build must prove that path
    offline, and the job must point at the very path the image bakes."""
    recipe = (SUITE / "images" / "base-image.Dockerfile").read_text("utf-8")
    # the cache and the npm config are baked at a fixed, documented path
    assert "COPY npm-cache/ /root/.npm/" in recipe
    assert "COPY npmrc /root/.npmrc" in recipe
    npmrc = (SUITE / "images" / "npmrc").read_text("utf-8")
    assert "prefer-offline=true" in npmrc
    assert "registry=https://registry.npmjs.org/" in npmrc, (
        "the baked cache is keyed by the registry URL; a different registry "
        "would miss every cached entry"
    )
    # the version is injected from the official lock, never hardcoded
    assert "ARG DSH_VERSION" in recipe
    assert '@deepseek-ai/dsh@${DSH_VERSION}' in recipe
    assert not re.search(r"@deepseek-ai/dsh@\d", recipe), (
        "the pinned version must come from the official lock, not the recipe"
    )
    # the build proves the exact offline path the trial will take
    assert "--cache /root/.npm --offline" in recipe
    assert "test -x /tmp/dshwarm/bin/dsh" in recipe
    # staging and the build argument come from the cluster host + aeval
    builder = (SUITE / "images" / "build-bases.py").read_text("utf-8")
    assert 'NPM_CACHE = Path("/root/.npm")' in builder
    assert 'DshAgent._locked_version()' in builder, (
        "the warmed cache must be built for the version the adapter installs"
    )
    assert '"--build-arg", f"DSH_VERSION=' in builder
    assert "stage_npm_cache()" in builder
    # the job's npm_cache is the path the image bakes
    job = (SUITE / "jobs" / "tbench-m0.yaml").read_text("utf-8")
    assert 'npm_cache: "/root/.npm"' in job
    assert "npm_cache: \"/dev/shm/npmcache\"" not in job, (
        "a tmpfs cache starts empty: it would re-download the whole tree "
        "over the throttled registry"
    )


def test_every_task_declares_the_working_directory_its_tests_assume():
    """The migration drops the upstream compose working_dir. These tasks
    hardcode /app (tests read /app/..., hello-world says "the current
    directory"), and Harbor's e2b environment uses the Dockerfile WORKDIR
    as the cwd for every exec — the agent's and the verifier's. Without it
    both run in the builder's /home/user and every task fails.

    The Dockerfile instruction is used rather than [environment].workdir
    because that field deadlocks this e2b template builder (the build
    stops answering /health at [finalize], the API then reports "template
    builder not found", and every trial dies with ServiceBusyException).
    """
    import tomllib

    for name in ROSTER:
        data = tomllib.loads((_task(name) / "task.toml").read_text("utf-8"))
        assert "workdir" not in data.get("environment", {}), name
        instructions = [
            line.strip()
            for line in (_task(name) / "environment" / "Dockerfile")
            .read_text("utf-8")
            .splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert instructions[-1] == "WORKDIR /app", (name, instructions[-1])
    # the tests really do depend on it
    hello = (_task("hello-world") / "tests" / "test_outputs.py").read_text("utf-8")
    assert 'Path("/app/hello.txt")' in hello
    # the migration is what loses it: no WORKDIR upstream, none in its output
    upstream = SUITE / "vendor" / "upstream" / "hello-world"
    assert "workdir" not in (upstream / "task.toml").read_text("utf-8")
    assert "WORKDIR" not in (upstream / "environment" / "Dockerfile").read_text("utf-8")
    assert "WORKDIR /app" in PROVENANCE_NOTE
    assert "deadlocks" in PROVENANCE_NOTE


def test_suite_stages_the_task_tests_before_collection(suite):
    """The collect command runs the task's own verifier, and the reward it
    publishes is read as an observable during the same collection. Harbor
    uploads tests/ only at verification time (after collection), so the
    suite must ask aeval to stage them post-agent."""
    assert suite.overlay.driver.stage_tests_before_collect is True
    collect = (SUITE / "suite.yaml").read_text("utf-8")
    assert "stage_tests_before_collect: true" in collect


def test_collect_plan_and_task_declarations_agree(suite):
    plan = build_required_collect_plan(suite)
    # The session-record slot takes the suite's flavor (dsh_session here):
    # exactly one of the two slot names appears, never both.
    # gated outputs (the sealed anchors channel) join a plan only when
    # the suite declares them — none of the shipped suites does
    fixed = {
        n for n in FIXED_OUTPUT_PATHS
        if n not in SESSION_RECORD_OUTPUTS and n not in CONDITIONAL_OUTPUTS
    }
    flavor = suite.overlay.driver.session_record
    assert set(plan) == fixed | {flavor} | {
        f"observable:{o.name}" for o in suite.overlay.observables
    }


# --- budgets ----------------------------------------------------------


def test_budget_parity_between_broker_spec_and_trajectory_grader(budgets):
    """The broker enforces limits.maxSteps/maxTokens; the trajectory
    grader measures efficiency against its own thresholds. If they drift
    apart, every efficiency score silently measures the wrong budget."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tbench_trajectory_grader", SUITE / "graders" / "tbench_trajectory.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    declared = {m.name: m for m in module._IMPL._metrics}
    assert declared["step_efficiency"].max_steps == budgets["grader"]["max_steps"]
    assert declared["token_efficiency"].max_tokens == budgets["grader"]["max_tokens"]
    assert budgets["broker"]["maxSteps"] == budgets["grader"]["max_steps"]
    assert budgets["broker"]["maxTokens"] == budgets["grader"]["max_tokens"]


def test_job_agent_timeout_matches_the_declared_budget(budgets):
    job = yaml.safe_load((SUITE / "jobs" / "tbench-m0.yaml").read_text("utf-8"))
    kwargs = job["agents"][0]["kwargs"]
    assert kwargs["run_timeout_sec"] == budgets["agent"]["run_timeout_sec"]


def test_broker_spec_generator_injects_the_declared_budget(tmp_path, budgets):
    base = {
        "brokerJs": "/opt/dist/broker_main.js",
        "listenPort": 8447,
        "listenHost": "193.126.4.2",
        "limits": {"maxSteps": 8},
        "maxOutputTokens": 512,
        "upstream": {"provider": "deepseek", "model": "deepseek-chat",
                     "baseUrl": "https://api.deepseek.com/v1",
                     "apiKeyEnv": "DEEPSEEK_API_KEY"},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), "utf-8")
    out_path = tmp_path / "pilot.json"
    result = subprocess.run(
        [sys.executable, str(SUITE / "tools" / "gen_broker_spec.py"),
         "--budgets", str(SUITE / "budgets.yaml"),
         "--base", str(base_path), "--out", str(out_path),
         "--token-count-endpoint", "http://127.0.0.1:8791/tokens/count"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    spec = json.loads(out_path.read_text("utf-8"))
    assert spec["limits"]["maxSteps"] == budgets["broker"]["maxSteps"]
    assert spec["limits"]["maxTokens"] == budgets["broker"]["maxTokens"]
    assert spec["maxOutputTokens"] == budgets["broker"]["maxOutputTokens"]
    assert spec["auxiliaryPolicy"] == {"compaction": "allow"}
    assert spec["tokenCount"]["endpoint"] == "http://127.0.0.1:8791/tokens/count"
    # everything else is carried over verbatim
    assert spec["listenPort"] == 8447
    assert spec["upstream"] == base["upstream"]
    # the spec names credential paths: it must not be world readable
    assert (out_path.stat().st_mode & 0o777) == 0o600


def test_broker_spec_generator_refuses_an_untrusted_counter(tmp_path):
    """A budget with maxTokens is only enforceable against a counter that
    cannot undercount. The base spec's counter is the offline chain's stub
    (a fixed 64 tokens for every request), and the first pilot run failed
    EVERY trial with AEVAL_TOKEN_BOUND_VIOLATED because the generator
    silently carried it over. Generating such a spec must now fail loudly
    instead."""
    base = {
        "brokerJs": "/opt/dist/broker_main.js",
        "limits": {"maxSteps": 8},
        "maxOutputTokens": 512,
        "tokenCount": {"endpoint": "http://127.0.0.1:8791/tokens/count"},
        "upstream": {"provider": "deepseek", "model": "deepseek-chat",
                     "baseUrl": "https://api.deepseek.com/v1",
                     "apiKeyEnv": "DEEPSEEK_API_KEY"},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), "utf-8")
    out_path = tmp_path / "pilot.json"
    result = subprocess.run(
        [sys.executable, str(SUITE / "tools" / "gen_broker_spec.py"),
         "--budgets", str(SUITE / "budgets.yaml"),
         "--base", str(base_path), "--out", str(out_path)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "--token-count-endpoint" in result.stderr
    assert not out_path.exists()


def test_pilot_ships_a_counter_that_cannot_undercount():
    """The counter the pilot is told to run answers with the UTF-8 byte
    length of the exact request body: one token cannot encode less than
    one byte, so the bound is conservative for any byte-level tokenizer —
    the only property the broker's post-dispatch check relies on."""
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location(
        "tbench_token_count", SUITE / "tools" / "token_count.py"
    )
    assert spec is not None and spec.loader is not None
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    for text in ["", "hello", "\u5199\u4e00\u4e2a\u6587\u4ef6\u5230 /app/hello.txt", "x" * 10_000]:
        body = json.dumps(
            {"model": "deepseek-chat", "messages": [{"role": "user", "content": text}]}
        ).encode("utf-8")
        bound = module.upper_bound(body)
        assert bound >= len(body) >= len(text.encode("utf-8"))
        assert bound >= 1
    assert module.upper_bound(b"x" * 5000) > module.upper_bound(b"x" * 50)


def test_task_working_directory_is_the_declared_agent_cwd(suite):
    """The agent must run where the task's tests look. The suite declares
    one cwd (driver.workspace_dir) and every vendored Dockerfile ends with
    the matching WORKDIR, so the DSH session and the verifier agree."""
    cwd = suite.overlay.driver.workspace_dir
    assert cwd == "/app"
    for task in sorted((SUITE / "tasks").iterdir()):
        if not task.is_dir():
            continue
        dockerfile = (task / "environment" / "Dockerfile").read_text("utf-8")
        last = [line for line in dockerfile.splitlines() if line.strip()][-1]
        assert last.strip() == f"WORKDIR {cwd}", (task.name, last)


def test_every_task_publishes_a_reward_on_every_code_path():
    """Harbor's adapter checklist: test.sh must rewrite the reward file on
    EVERY code path. Upstream's does not — setup-uv-pytest.sh is sourced
    and enables errexit, so a failing test run exits before the reward
    branch, and Harbor then reports RewardFileNotFoundError instead of
    reward 0 (measured on the first M0 pilot: all three failed tasks were
    excluded as unjudgeable)."""
    for task in sorted((SUITE / "tasks").iterdir()):
        if not task.is_dir():
            continue
        script = (task / "tests" / "test.sh").read_text("utf-8")
        assert "mkdir -p /logs/verifier" in script, task.name
        # errexit is cleared after the sourced setup script and before the
        # test invocation, so the reward branch is always reached
        assert script.index("source /tests/setup-uv-pytest.sh") < script.index("set +e")
        assert script.index("set +e") < script.index("bash /tests/run-uv-pytest.sh")
        assert script.index("bash /tests/run-uv-pytest.sh") < script.index(
            "echo 1 > /logs/verifier/reward.txt"
        )
        assert "echo 0 > /logs/verifier/reward.txt" in script
        # the test invocation itself is upstream's
        assert (_task(task.name) / "tests" / "run-uv-pytest.sh").read_bytes() == (
            SUITE / "vendor" / "upstream" / task.name / "tests" / "run-uv-pytest.sh"
        ).read_bytes(), task.name


def test_suite_declares_the_sandbox_posture_the_tasks_need(suite):
    """DSH refuses shell commands when no bwrap/Landlock runner exists
    (measured: "no sandbox backend is usable on this host"), which left
    two of three tasks unanswerable. The knob is DSH's own, so the suite
    declares it inside the dsh namespace of ``control_options`` — the
    framework carries it without interpreting it, and the dsh flavor turns it
    into DSH_PERMISSION_MODE."""
    assert suite.overlay.driver.control_options == {
        "dsh": {"permission_mode": "danger-full-access"}
    }
    # the framework has no DSH-shaped field of its own any more
    assert not hasattr(DriverSpec(), "sandbox_mode")
    assert DriverSpec().control_options == {}
    # a new family's namespace is accepted by the framework and validated by
    # the flavor that owns it (see the dsh flavor's own tests)
    declared = DriverSpec(control_options={"other": {"knob": 1}})
    assert declared.control_options == {"other": {"knob": 1}}


# --- grading ----------------------------------------------------------


@pytest.fixture(scope="module")
def graders(suite):
    return load_suite_graders(suite)


def test_grader_identities_are_versioned_and_layered(graders):
    by_id = {g.grader.id: g for g in graders}
    assert set(by_id) == {"tbench-outcome", "tbench-trajectory"}
    outcome, trajectory = by_id["tbench-outcome"], by_id["tbench-trajectory"]
    assert outcome.grader.version == "v1" and outcome.grader.layer == "outcome"
    assert trajectory.grader.version == "v1" and trajectory.grader.layer == "trajectory"
    assert outcome.requires_fields == ["events", "token_usage"]
    union = SUITE.joinpath("suite.yaml").read_text("utf-8")
    assert "graders/tbench_outcome.py@v1" in union
    assert "graders/tbench_trajectory.py@v1" in union


def _record(tmp_path, *, reward=None, with_reward=True, completeness=("ok", "ok")):
    from aeval.contracts import ArtifactRef, TrialCoordinates

    artifacts = {}
    if with_reward:
        content = json.dumps(
            {"name": "reward", "value": reward},
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")
        (tmp_path / "artifacts").mkdir(exist_ok=True)
        (tmp_path / "artifacts" / "reward.json").write_bytes(content)
        artifacts["observable:reward"] = ArtifactRef(
            media_type="application/json",
            sha256=sha256(content).hexdigest(),
            size_bytes=len(content),
            path="artifacts/reward.json",
        )
    events_status, usage_status = completeness
    extra = {"aeval": {"completeness": {"fields": [
        {"field": "events", "status": events_status},
        {"field": "token_usage", "status": usage_status},
    ]}}}
    progress = RequirementProgress()
    for bit in ("input_complete", "agent_finished", "integration_valid",
                "render_valid", "artifact_schema_ok"):
        progress.mark(bit)
    return build_trial_record(
        trial_id="trial-tbench-1",
        coordinates=TrialCoordinates(
            run_id="run-tbench", suite_id="tbench-pilot",
            suite_version="0.1.0", task_id="hello-world", trial_index=0,
        ),
        stop_reason="agent_exit_0",
        baseline_ok=True,
        progress=progress,
        artifacts=artifacts,
        transcript_extra=extra,
        grader_versions={"tbench-outcome": "v1", "tbench-trajectory": "v1"},
    )


def _by_id(results, grader_id):
    return next(r for r in results if r.grader_id == grader_id)


async def test_outcome_grader_passes_on_reward_one(tmp_path, graders):
    results = await grade_trial(_record(tmp_path, reward="1"), graders)
    outcome = _by_id(results, "tbench-outcome")
    assert outcome.status == "pass"
    assert outcome.score.value == 1.0
    assert outcome.grader_version == "v1"


async def test_outcome_grader_fails_on_reward_zero(tmp_path, graders):
    results = await grade_trial(_record(tmp_path, reward="0"), graders)
    outcome = _by_id(results, "tbench-outcome")
    assert outcome.status == "fail"
    assert outcome.score.value == 0.0
    assert "reward is 0" in " ".join(outcome.reasons)


async def test_outcome_grader_cannot_judge_without_a_reward(tmp_path, graders):
    """A missing reward means the upstream verifier never ran: an
    infrastructure fact, never a fabricated zero."""
    results = await grade_trial(_record(tmp_path, with_reward=False), graders)
    outcome = _by_id(results, "tbench-outcome")
    assert outcome.status == "cannot_judge"
    # unjudgeable is NEVER a zero: no value, and the record says why
    assert outcome.score.valid is False
    assert outcome.score.value is None
    assert "never published a reward" in " ".join(outcome.reasons)


async def test_outcome_grader_cannot_judge_on_malformed_reward(tmp_path, graders):
    results = await grade_trial(_record(tmp_path, reward="true"), graders)
    outcome = _by_id(results, "tbench-outcome")
    assert outcome.status == "cannot_judge"
    assert "malformed" in " ".join(outcome.reasons)


async def test_outcome_grader_cannot_judge_on_degraded_transcript(tmp_path, graders):
    record = _record(tmp_path, reward="1", completeness=("ok", "partial"))
    results = await grade_trial(record, graders)
    outcome = _by_id(results, "tbench-outcome")
    assert outcome.status == "cannot_judge"
    assert outcome.score.valid is False
    assert "token_usage" in " ".join(outcome.reasons)


async def test_outcome_grader_cannot_judge_on_missing_transcript_fields(tmp_path, graders):
    record = _record(tmp_path, reward="1")
    record = record.model_copy(update={"transcript_extra": None})
    results = await grade_trial(record, graders)
    outcome = _by_id(results, "tbench-outcome")
    assert outcome.status == "cannot_judge"
