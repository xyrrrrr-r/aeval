"""P2-3: a job describes the task arm — which agent drives it is a declaration.

The matrix of job files per pairing is what makes a second agent expensive: every
new agent multiplies the job files. Composing the agent entry from its
declaration collapses that back to one job file per arm.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError

REPO = Path(__file__).resolve().parents[3]
SUITE = REPO / "suites" / "e2e-hello"
AGENTS = REPO / "agents"


@pytest.fixture
def suite():
    return load_suite(SUITE)


def test_a_declared_agent_reproduces_the_hand_written_job(suite):
    """The generated pairing must equal the job file a human wrote for the lab."""
    planned = compose_harbor_job(suite)
    driven = compose_harbor_job(suite, agent="dsh", agents_root=AGENTS)

    left, right = planned.model_dump(), driven.model_dump()
    left.pop("agents")
    right.pop("agents")
    assert left == right, "the agent-neutral parts of the job must not change"

    hand_written = yaml.safe_load((SUITE / "jobs" / "dsh.yaml").read_text())["agents"][0]
    generated = driven.model_dump()["agents"][0]
    assert {key: generated.get(key) for key in hand_written} == hand_written


def test_a_named_profile_changes_only_the_launch_facts(suite):
    default = compose_harbor_job(suite, agent="dsh", agents_root=AGENTS).agents[0]
    lab = compose_harbor_job(
        suite, agent="dsh", agent_profile="lab", agents_root=AGENTS
    ).agents[0]
    assert default.import_path == lab.import_path
    assert default.kwargs["npm_cache"] != lab.kwargs["npm_cache"]
    assert set(default.kwargs) == set(lab.kwargs)


def test_a_second_agent_is_evaluated_without_any_job_file_for_it(suite):
    """The whole point: pairing costs a declaration, not a file.

    There is no ``jobs/fakeagent.yaml`` and there never will be; the pairing is
    still evaluated, and refused for the real reason (a capability gap) rather
    than for a missing file.
    """
    assert not (SUITE / "jobs" / "fakeagent.yaml").exists()
    with pytest.raises(SuiteError) as excinfo:
        compose_harbor_job(suite, agent="fakeagent", agents_root=AGENTS)
    message = str(excinfo.value)
    assert "cannot serve this suite" in message
    assert "missing" in message and "acp_stdio" in message


def test_an_undeclared_agent_is_refused_with_the_fix(suite):
    with pytest.raises(SuiteError, match="declare it in"):
        compose_harbor_job(suite, agent="nope", agents_root=AGENTS)


def test_an_unknown_profile_lists_the_declared_ones(suite):
    with pytest.raises(SuiteError, match="declared profiles"):
        compose_harbor_job(suite, agent="dsh", agent_profile="nope", agents_root=AGENTS)


def test_a_job_file_no_longer_has_to_name_an_agent(tmp_path, suite):
    # the suite extends suites/_base, so the whole tree travels together
    shutil.copytree(REPO / "suites", tmp_path / "suites")
    copied = tmp_path / "suites" / "e2e-hello"
    job_path = copied / "jobs" / "e2e-hello.yaml"
    job = yaml.safe_load(job_path.read_text())
    job.pop("agents")
    job_path.write_text(yaml.safe_dump(job), encoding="utf-8")

    stripped = load_suite(copied)
    # without an agent the job is incomplete (legacy path, unchanged)
    with pytest.raises(SuiteError, match="at least one agent"):
        compose_harbor_job(stripped)
    # with a declared agent it composes
    driven = compose_harbor_job(stripped, agent="dsh", agents_root=AGENTS)
    assert driven.agents[0].import_path == "aeval.agents.dsh.agent:DshAgent"
