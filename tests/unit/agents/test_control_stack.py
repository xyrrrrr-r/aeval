"""The in-sandbox control stack is declared, not assumed.

It is what makes ``gateway_lease`` enforceable, and it is DSH-specific, so a
framework that deploys it for every agent both breaks the second agent and claims
metering it cannot deliver.
"""

from __future__ import annotations

import pytest

from aeval.agents.contract import (
    adapter_declaration_gap,
    build_adapter_spec,
    control_stack_of,
)
from aeval.agents.dsh.agent import DshAgent
from aeval.agents.testing.fake_agent import FakeAtifAgent
from aeval.agents.declaration import (
    AgentDeclaration,
    declaration_class_mismatches,
    resolve_agent_declaration,
)
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_only_the_dsh_adapter_declares_a_control_stack():
    assert control_stack_of(DshAgent) == "dsh"
    assert control_stack_of(FakeAtifAgent) is None


def test_a_gateway_lease_claim_without_a_stack_is_refused():
    """An unkeepable promise: metered in the record, unmetered in reality."""

    class Liar:
        ADAPTER_ID = "liar"
        ADAPTER_VERSION = "1"
        ADAPTER_MODE = "installed_cli"
        BUDGET_ENFORCEMENT = "gateway_lease"
        WRITE_SURFACE = "ephemeral_overlay"
        PROVIDES = frozenset({"shell"})
        TRANSCRIPT_CAPABILITY = DshAgent.TRANSCRIPT_CAPABILITY

    gap = adapter_declaration_gap(Liar)
    assert any("CONTROL_STACK" in item for item in gap), gap
    with pytest.raises(Exception, match="CONTROL_STACK"):
        build_adapter_spec(Liar)
    # declaring the stack satisfies it
    Liar.CONTROL_STACK = "dsh"
    assert adapter_declaration_gap(Liar) == []


def test_the_shipped_declaration_names_the_stack():
    resolved = resolve_agent_declaration(REPO / "agents" / "dsh.yaml", agents_root=REPO / "agents")
    assert resolved.declaration.control_stack == "dsh"
    assert declaration_class_mismatches(resolved.declaration, DshAgent) == []


def test_a_declaration_that_hides_the_stack_is_refused():
    resolved = resolve_agent_declaration(REPO / "agents" / "dsh.yaml", agents_root=REPO / "agents")
    data = resolved.declaration.model_dump()
    data.pop("control_stack")
    wrong = AgentDeclaration.model_validate(data)
    assert any(
        item.startswith("control_stack:")
        for item in declaration_class_mismatches(wrong, DshAgent)
    )
