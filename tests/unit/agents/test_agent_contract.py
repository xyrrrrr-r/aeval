"""The agent-adapter contract: capability negotiation and required members.

Before this contract existed, ``driver.require`` was printed by ``explain`` and
never enforced: a suite could demand ``acp_stdio`` and the job could select an
agent that speaks nothing of the sort, and the run would proceed and burn money.
These tests pin the refusal.
"""

from __future__ import annotations

import pytest
import yaml

from types import SimpleNamespace

from aeval.agents.contract import (
    PROVIDES_ATTR,
    adapter_declaration_gap,
    adapter_member_gap,
    build_adapter_spec,
    declared_capabilities,
    load_adapter_class,
    required_capability_gaps,
)
from aeval.agents.dsh.agent import DshAgent
from aeval.hooks.plugin import HookRegistrationError
from aeval.hooks.plugin import _require_adapter_contract  # noqa: PLC2701
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


# --- recorded adapter identity -----------------------------------------

def test_session_record_output_of_reads_the_declared_slot():
    from aeval.agents.contract import session_record_output_of

    class Slotted:
        SESSION_RECORD_OUTPUT = "agent_session_record"

    assert session_record_output_of(Slotted) == "agent_session_record"
    assert session_record_output_of(DshAgent) == "dsh_session"


def test_session_record_output_of_fails_closed_on_bad_declarations():
    from aeval.agents.contract import session_record_output_of

    class Undeclared:
        pass

    class Malformed:
        SESSION_RECORD_OUTPUT = "Free-Form!"

    with pytest.raises(SuiteError, match="declares no valid SESSION_RECORD_OUTPUT"):
        session_record_output_of(Undeclared)
    with pytest.raises(SuiteError, match="declares no valid SESSION_RECORD_OUTPUT"):
        session_record_output_of(Malformed)


def test_a_declared_slot_needs_its_fixed_path():
    """The vocabulary is open (any well-formed slug is a slot), so the path
    is what makes a declared slot real — without it neither the collector
    nor the evidence gate could locate the record."""
    from aeval.agents.contract import session_record_output_of

    class OwnSlot:
        SESSION_RECORD_OUTPUT = "gpt_session"

    with pytest.raises(SuiteError, match="no built-in path"):
        session_record_output_of(OwnSlot)

    class SlottedWithPath(OwnSlot):
        SESSION_RECORD_OUTPUT_PATH = "gpt/session.bin"

    assert session_record_output_of(SlottedWithPath) == "gpt_session"


def test_dsh_adapter_spec_is_built_from_declarations():
    spec = build_adapter_spec(DshAgent, version="0.9.9-preview")
    assert spec.id == "dsh"
    assert spec.version == "0.9.9-preview"          # observed version wins
    assert spec.mode == "acp_stdio"
    assert spec.impl_version == "1"
    assert spec.budget_enforcement == "gateway_lease"
    assert spec.transcript.source == "native_session_via_bridge"
    # falls back to the declared version when nothing was observed
    assert build_adapter_spec(DshAgent).version == "1"


def test_adapter_without_declarations_cannot_be_recorded(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "bare_adapter.py").write_text("class BareAgent:\n    pass\n", encoding="utf-8")
    adapter = load_adapter_class("bare_adapter:BareAgent")
    gap = adapter_declaration_gap(adapter)
    assert "ADAPTER_ID" in gap and "BUDGET_ENFORCEMENT" in gap
    with pytest.raises(SuiteError, match="declares no"):
        build_adapter_spec(adapter)


def test_run_start_refuses_an_adapter_that_cannot_describe_itself(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "bare_adapter.py").write_text("class BareAgent:\n    pass\n", encoding="utf-8")
    job = SimpleNamespace(config=SimpleNamespace(
        agents=[SimpleNamespace(import_path="bare_adapter:BareAgent")]
    ))
    with pytest.raises(HookRegistrationError, match="missing required members"):
        _require_adapter_contract(job)
    # a Harbor-native placeholder makes no claim and is exempt
    _require_adapter_contract(SimpleNamespace(
        config=SimpleNamespace(agents=[SimpleNamespace(name="nop", import_path=None)])
    ))
    # the shipped adapter passes the gate
    _require_adapter_contract(SimpleNamespace(
        config=SimpleNamespace(agents=[SimpleNamespace(import_path=DSH_IMPORT_PATH)])
    ))


# --- required members (fail at start, not mid-collection) ---------------

def test_dsh_satisfies_the_adapter_contract_members():
    assert adapter_member_gap(DshAgent) == []


def test_adapter_that_cannot_be_read_is_refused_before_the_run(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "half_adapter.py").write_text(
        "class HalfAgent:\n"
        "    PROVIDES = frozenset({'shell'})\n"
        "    ADAPTER_ID = 'half'\n"
        "    ADAPTER_VERSION = '1'\n"
        "    ADAPTER_MODE = 'cli'\n"
        "    BUDGET_ENFORCEMENT = 'none'\n",
        encoding="utf-8",
    )
    gap = adapter_member_gap(load_adapter_class("half_adapter:HalfAgent"))
    assert "read_trial_session" in gap and "paths" in gap
    job = SimpleNamespace(config=SimpleNamespace(
        agents=[SimpleNamespace(import_path="half_adapter:HalfAgent")]
    ))
    with pytest.raises(HookRegistrationError, match="missing required members"):
        _require_adapter_contract(job)


# --- graded archives must name the agent that ran, never a hardcoded one -

def _record_with(adapter):
    from aeval.contracts import TrialCoordinates, TrialRecord

    return TrialRecord(
        trial_id="t1",
        coordinates=TrialCoordinates(
            run_id="r1", suite_id="s", suite_version="1", task_id="task", trial_index=0
        ),
        stop_reason="agent_exit_0",
        baseline_ok=True,
        verdict="cannot_judge",
        adapter=adapter,
    )


def test_stored_record_grading_uses_the_recorded_agent_identity():
    from aeval.verdict.executor import _ct_of  # noqa: PLC2701

    spec = build_adapter_spec(DshAgent, version="2.3.4")
    spec = spec.model_copy(update={"id": "otheragent"})
    assert _ct_of(_record_with(spec)).atif.agent.name == "otheragent"
    assert _ct_of(_record_with(spec)).atif.agent.version == "2.3.4"
    # no recorded adapter: neutral unknown, NOT a silent "dsh"
    legacy = _ct_of(_record_with(None)).atif.agent
    assert legacy.name == "unknown"
    assert legacy.version == "unknown"
