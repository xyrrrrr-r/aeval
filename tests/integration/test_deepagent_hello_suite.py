"""P2-5a smoke suite: the deepagent-hello pairing is real, not declared.

The suite is the "runs at all" carrier for the ACP adapter: same task shape as
e2e-hello (write ``hello`` to /workspace/result), a driver contract deepagent
can actually serve, and the generic session-record slot. These tests pin the
load-bearing facts so the suite cannot silently drift into an unserviceable
shape before the lab run seals it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeval.hooks.evidence import (
    CONDITIONAL_OUTPUTS,
    FIXED_OUTPUT_PATHS,
    SESSION_RECORD_OUTPUTS,
    build_required_collect_plan,
)
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError

REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "suites" / "deepagent-hello"
AGENTS = REPO / "agents"


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE)


def test_the_suite_loads_with_the_acp_driver_contract(suite):
    """No sdk_jsonrpc: the DSH-only requirement is explicitly removed."""
    assert sorted(suite.overlay.driver.require) == ["acp_stdio", "file_tools", "shell"]
    assert suite.overlay.driver.session_record == "agent_session_record"


def test_the_collect_plan_takes_the_generic_session_slot(suite):
    plan = build_required_collect_plan(suite)
    # gated outputs (the sealed anchors channel) join a plan only when
    # the suite declares them — none of the shipped suites does
    fixed = {
        n for n in FIXED_OUTPUT_PATHS
        if n not in SESSION_RECORD_OUTPUTS and n not in CONDITIONAL_OUTPUTS
    }
    assert set(plan) == fixed | {"agent_session_record"} | {
        f"observable:{o.name}" for o in suite.overlay.observables
    }
    # exactly one flavor of the slot, never both
    assert "dsh_session" not in plan


def test_the_task_declares_the_flavored_collect_outputs(suite):
    task_toml = (SUITE / "tasks" / "hello" / "task.toml").read_text("utf-8")
    plan = build_required_collect_plan(suite)
    for name in plan:
        if name.startswith("observable:"):
            continue
        assert name in task_toml, f"task.toml must declare collect output {name}"


def test_the_placeholder_job_composes_for_shape_validation(suite):
    """agents: [nop] keeps the suite loadable without any adapter installed."""
    job = compose_harbor_job(suite)
    assert job.n_attempts == 5
    assert [a.name for a in job.agents] == ["nop"]


def test_the_deepagent_pairing_composes_end_to_end(suite):
    """The whole point: one declaration, no per-agent job file."""
    assert not (SUITE / "jobs" / "deepagent.yaml").exists()
    driven = compose_harbor_job(suite, agent="deepagent", agents_root=AGENTS)
    assert driven.agents[0].import_path == "aeval.agents.deepagent.agent:DcodeAgent"


def test_the_dsh_pairing_is_refused_for_the_flavor_reason(suite):
    """dsh produces dsh_session; this suite's plan names agent_session_record."""
    with pytest.raises(SuiteError, match="session-record flavor mismatch"):
        compose_harbor_job(suite, agent="dsh", agents_root=AGENTS)


def test_deepagent_against_e2e_hello_is_refused_for_the_slot_not_capabilities():
    """e2e-hello asks only for what its task needs (acp_stdio + shell), which
    deepagent provides — so the refusal must come from the fact that actually
    differs: the session-record slot it collects (dsh_session vs
    agent_session_record). A capability refusal here would mean the suite is
    back to requiring a DSH-specific channel."""
    with pytest.raises(SuiteError, match="session-record flavor mismatch"):
        compose_harbor_job(
            load_suite(REPO / "suites" / "e2e-hello"),
            agent="deepagent",
            agents_root=AGENTS,
        )
