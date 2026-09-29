"""The agent-adapter contract: capability negotiation and required members.

Before this contract existed, ``driver.require`` was printed by ``explain`` and
never enforced: a suite could demand ``acp_stdio`` and the job could select an
agent that speaks nothing of the sort, and the run would proceed and burn money.
These tests pin the refusal.
"""

from __future__ import annotations

import pytest
import yaml

from aeval.agents.contract import (
    PROVIDES_ATTR,
    declared_capabilities,
    load_adapter_class,
    required_capability_gaps,
)
from aeval.suite_loader.composition import _check_agent_capabilities  # noqa: PLC2701
from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError

DSH_IMPORT_PATH = "aeval.agents.dsh.agent:DshAgent"


def test_dsh_declares_the_capabilities_its_suites_require():
    capabilities = declared_capabilities(DSH_IMPORT_PATH)
    assert capabilities == frozenset({"acp_stdio", "sdk_jsonrpc", "shell", "file_tools", "resume"})
    # the shipped base requires two of them; both must be covered
    assert {"acp_stdio", "sdk_jsonrpc"} <= capabilities


def test_missing_or_malformed_declaration_is_refused(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "adapter_shapes.py").write_text(
        "class NoDeclaration:\n    pass\n\n"
        "class StringDeclaration:\n    PROVIDES = 'shell'\n\n"
        "class BlankEntry:\n    PROVIDES = frozenset({'  '})\n\n"
        "not_a_class = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match=PROVIDES_ATTR):
        declared_capabilities("adapter_shapes:NoDeclaration")
    with pytest.raises(SuiteError, match="collection of capability names"):
        declared_capabilities("adapter_shapes:StringDeclaration")
    with pytest.raises(SuiteError, match="non-empty strings"):
        declared_capabilities("adapter_shapes:BlankEntry")
    with pytest.raises(SuiteError, match="must name a class"):
        declared_capabilities("adapter_shapes:not_a_class")
    with pytest.raises(SuiteError, match="has no attribute 'Missing'"):
        declared_capabilities("adapter_shapes:Missing")
    with pytest.raises(SuiteError, match="Cannot import agent adapter"):
        declared_capabilities("no_such_adapter_module:Agent")
    with pytest.raises(SuiteError, match="module:Class"):
        declared_capabilities("adapter_shapes")
    assert isinstance(load_adapter_class(DSH_IMPORT_PATH), type)


def test_gaps_are_computed_against_every_selected_adapter():
    provided, missing = required_capability_gaps([DSH_IMPORT_PATH], ["acp_stdio", ""])
    assert missing == []
    assert provided[DSH_IMPORT_PATH] >= {"acp_stdio"}
    # an open vocabulary: a name nobody provides is simply a gap, never a pass
    _, missing = required_capability_gaps([DSH_IMPORT_PATH], ["browser"])
    assert missing == ["browser"]


def test_named_harbor_agents_are_exempt_but_visible():
    # `name:` agents are Harbor-native placeholders (nop/oracle) used for suite
    # shape validation; they claim no capabilities, so requiring them would break
    # those jobs. The exemption is deliberate, not an implicit pass.
    _check_agent_capabilities([{"name": "nop"}], ["acp_stdio"], _job_path("jobs/nop.yaml"))


def test_agent_without_the_required_capability_is_refused():
    with pytest.raises(SuiteError, match="cannot serve this suite"):
        _check_agent_capabilities(
            [{"import_path": DSH_IMPORT_PATH}], ["browser"], _job_path("jobs/x.yaml")
        )


def test_compose_refuses_a_pairing_the_agent_cannot_serve(tmp_path, native_suite_dir, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "weak_adapter.py").write_text(
        "class WeakAgent:\n    PROVIDES = frozenset({'shell'})\n", encoding="utf-8"
    )
    data = yaml.safe_load((native_suite_dir / "suite.yaml").read_text(encoding="utf-8"))
    data["driver"] = {"require": ["acp_stdio", "shell"]}
    (native_suite_dir / "suite.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (native_suite_dir / "job.yaml").write_text(
        "job_name: synthetic\nn_attempts: 1\nn_concurrent_trials: 1\n"
        "agents: [{import_path: 'weak_adapter:WeakAgent'}]\n",
        encoding="utf-8",
    )
    suite = load_suite(native_suite_dir)
    with pytest.raises(SuiteError, match=r"missing \['acp_stdio'\]"):
        compose_harbor_job(suite)


def test_compose_accepts_an_agent_that_covers_the_requirements(tmp_path, native_suite_dir):
    data = yaml.safe_load((native_suite_dir / "suite.yaml").read_text(encoding="utf-8"))
    data["driver"] = {"require": ["acp_stdio", "sdk_jsonrpc"]}
    (native_suite_dir / "suite.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (native_suite_dir / "job.yaml").write_text(
        "job_name: synthetic\nn_attempts: 1\nn_concurrent_trials: 1\n"
        f"agents: [{{import_path: '{DSH_IMPORT_PATH}'}}]\n",
        encoding="utf-8",
    )
    job = compose_harbor_job(load_suite(native_suite_dir))
    assert job.job_name == "synthetic"


def _job_path(relative: str):
    from pathlib import Path

    return Path(relative)
