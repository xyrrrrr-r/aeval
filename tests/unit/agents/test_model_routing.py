"""The model_routing declaration.

One declaration now answers "which wire does this agent speak for model
traffic, and which env vars point it at the facade": the composition derives
the facade's served endpoints and the injected env from it, so adding an agent
with a different wire or different env spellings costs a declaration, not a
core change. These tests pin the vocabulary, the class↔declaration agreement,
the reconciliation rules, and the two shipped declarations (dsh =
gateway_native, no facade; deepagent = openai_responses, facade serves
/v1/responses).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aeval.agents.contract import (
    ModelRouting,
    adapter_declaration_gap,
    facade_protocols_for,
    model_routing_of,
    session_record_output_of,
    session_record_output_path_of,
)
from aeval.agents.declaration import (
    ModelRoutingDeclaration,
    declaration_class_mismatches,
    resolve_agent_declaration,
)
from aeval.agents.deepagent.agent import DcodeAgent, facade_routing_env
from aeval.agents.dsh.agent import DshAgent
from aeval.suite_models import SuiteError

_AGENTS_ROOT = Path(__file__).resolve().parents[3] / "agents"


# ── the declaration shape ──────────────────────────────────────────────────


def test_a_routing_declaration_parses_with_its_env_spellings():
    routing = ModelRoutingDeclaration.model_validate({
        "agent_protocol": "openai_responses",
        "env": {
            "base_url": "OPENAI_BASE_URL",
            "alt_base_url": "OPENAI_API_BASE",
            "api_key": "OPENAI_API_KEY",
        },
    })
    assert routing.agent_protocol == "openai_responses"
    assert routing.env["api_key"] == "OPENAI_API_KEY"


def test_gateway_native_needs_no_env_spellings():
    routing = ModelRoutingDeclaration.model_validate({"agent_protocol": "gateway_native"})
    assert routing.env == {}


@pytest.mark.parametrize(
    "payload",
    [
        # protocol outside the vocabulary
        {"agent_protocol": "grpc"},
        # an openai_* wire must say where the agent reads the facade URL/key
        {"agent_protocol": "openai_chat", "env": {"api_key": "K"}},
        {"agent_protocol": "openai_responses", "env": {}},
        # env spellings must be env var names in known slots
        {"agent_protocol": "openai_chat", "env": {"base_url": "not an env", "api_key": "K"}},
        {"agent_protocol": "openai_chat", "env": {"cookies": "K", "base_url": "U", "api_key": "K"}},
        # unknown keys are refused, not ignored
        {"agent_protocol": "gateway_native", "previous": "resp_1"},
    ],
)
def test_unrepresentable_routings_are_refused(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelRoutingDeclaration.model_validate(payload)


# ── the class-side declaration and its derivation ──────────────────────────


def test_model_routing_of_reads_and_validates_the_class_attribute():
    routing = model_routing_of(DcodeAgent)
    assert routing == ModelRouting(
        agent_protocol="openai_responses",
        env={"base_url": "OPENAI_BASE_URL", "alt_base_url": "OPENAI_API_BASE", "api_key": "OPENAI_API_KEY"},
    )
    assert model_routing_of(DshAgent) == ModelRouting(agent_protocol="gateway_native")
    assert model_routing_of(SimpleNamespace) is None


def test_a_malformed_class_routing_is_an_error_not_a_guess():
    class Bad:
        MODEL_ROUTING = {"agent_protocol": "carrier_pigeon"}

    with pytest.raises(SuiteError, match="agent_protocol"):
        model_routing_of(Bad)


def test_facade_protocols_derive_from_the_declared_wire():
    assert facade_protocols_for(ModelRouting(agent_protocol="gateway_native")) == []
    assert facade_protocols_for(ModelRouting(agent_protocol="openai_chat")) == ["chat_completions"]
    assert facade_protocols_for(ModelRouting(agent_protocol="openai_responses")) == ["responses"]


def test_facade_routing_env_uses_the_declared_spellings():
    from aeval.contracts import FACADE_API_KEY_PLACEHOLDER, FACADE_BASE_URL

    env = facade_routing_env()
    assert env == {
        "OPENAI_BASE_URL": FACADE_BASE_URL,
        "OPENAI_API_BASE": FACADE_BASE_URL,
        "OPENAI_API_KEY": FACADE_API_KEY_PLACEHOLDER,
    }
    # a different agent's spellings flow through the same derivation
    other = ModelRouting(
        agent_protocol="openai_chat",
        env={"base_url": "ANTHROPIC_BASE_URL", "api_key": "ANTHROPIC_API_KEY"},
    )
    assert facade_routing_env(other) == {
        "ANTHROPIC_BASE_URL": FACADE_BASE_URL,
        "ANTHROPIC_API_KEY": FACADE_API_KEY_PLACEHOLDER,
    }
    # a gateway-native agent gets nothing injected: no facade, no env
    assert facade_routing_env(ModelRouting(agent_protocol="gateway_native")) == {}


# ── the reconciliation rules ────────────────────────────────────────


def _complete_adapter(**overrides):
    """A contract-complete adapter skeleton with overridable declarations."""

    class Skeleton:
        ADAPTER_ID = "skeleton"
        ADAPTER_VERSION = "1"
        ADAPTER_MODE = "acp_stdio"
        TRANSCRIPT_CAPABILITY = None
        BUDGET_ENFORCEMENT = "none"

    for name, value in overrides.items():
        setattr(Skeleton, name, value)
    return Skeleton


def test_an_openai_wire_without_the_facade_stack_is_an_unkeepable_promise():
    adapter = _complete_adapter(
        MODEL_ROUTING={"agent_protocol": "openai_chat", "env": {"base_url": "U", "api_key": "K"}},
    )
    gap = adapter_declaration_gap(adapter)
    assert any("openai_chat" in item and "CONTROL_STACK" in item for item in gap)
    # a stack that does not translate the wire is refused the same way
    gap = adapter_declaration_gap(
        _complete_adapter(
            CONTROL_STACK="dsh",
            MODEL_ROUTING={"agent_protocol": "openai_chat", "env": {"base_url": "U", "api_key": "K"}},
        )
    )
    assert any("openai_chat" in item and "does not translate" in item for item in gap)


def test_a_translating_stack_without_an_openai_wire_serves_nothing():
    gap = adapter_declaration_gap(_complete_adapter(CONTROL_STACK="deepagent-facade"))
    assert any("MODEL_ROUTING" in item for item in gap)
    gap = adapter_declaration_gap(
        _complete_adapter(CONTROL_STACK="deepagent-facade", MODEL_ROUTING={"agent_protocol": "gateway_native"})
    )
    assert any("MODEL_ROUTING" in item for item in gap)


def test_the_two_shipped_agents_reconcile():
    assert adapter_declaration_gap(DshAgent) == []
    assert adapter_declaration_gap(DcodeAgent) == []


# ── the shipped declarations agree with their classes ──────────────────────


@pytest.mark.parametrize("agent_id", ["dsh", "deepagent"])
def test_the_shipped_declarations_carry_their_routing(agent_id):
    resolved = resolve_agent_declaration(_AGENTS_ROOT / f"{agent_id}.yaml", agents_root=_AGENTS_ROOT)
    assert declaration_class_mismatches(resolved.declaration, resolved.declaration.import_path and _class_of(resolved)) == []


def _class_of(resolved):
    from aeval.agents.contract import load_adapter_class

    return load_adapter_class(resolved.declaration.import_path)


def test_dsh_declares_gateway_native_and_deepagent_declares_responses():
    dsh = resolve_agent_declaration(_AGENTS_ROOT / "dsh.yaml", agents_root=_AGENTS_ROOT)
    assert dsh.declaration.model_routing is not None
    assert dsh.declaration.model_routing.agent_protocol == "gateway_native"
    deepagent = resolve_agent_declaration(_AGENTS_ROOT / "deepagent.yaml", agents_root=_AGENTS_ROOT)
    assert deepagent.declaration.model_routing is not None
    assert deepagent.declaration.model_routing.agent_protocol == "openai_responses"
    assert deepagent.declaration.model_routing.env["base_url"] == "OPENAI_BASE_URL"


def test_a_routing_disagreement_between_declaration_and_class_is_an_error():
    resolved = resolve_agent_declaration(_AGENTS_ROOT / "deepagent.yaml", agents_root=_AGENTS_ROOT)

    class Drifted(DcodeAgent):
        MODEL_ROUTING = {"agent_protocol": "openai_chat", "env": {"base_url": "U", "api_key": "K"}}

    mismatches = declaration_class_mismatches(resolved.declaration, Drifted)
    assert any("model_routing.agent_protocol" in item for item in mismatches)

    class Unrouted(DcodeAgent):
        pass

    Unrouted.MODEL_ROUTING = None
    mismatches = declaration_class_mismatches(resolved.declaration, Unrouted)
    # the gap check fires first (an unkeepable promise outranks a drift), so
    # the routing absence surfaces as the MODEL_ROUTING gap
    assert any("MODEL_ROUTING" in item or "model_routing" in item for item in mismatches)


def test_an_agent_without_any_routing_stays_compatible():
    """No model_routing block, no class attribute: nothing changes for it."""
    resolved = resolve_agent_declaration(_AGENTS_ROOT / "fakeagent.yaml", agents_root=_AGENTS_ROOT)
    assert resolved.declaration.model_routing is None


# --- stage 3: the session-record slot's path is declared, and mirrored -------

def test_the_shipped_dsh_declaration_carries_the_slot_path():
    """3.2: the DSH record's fixed path lives in agents/dsh.yaml (artifacts:),
    mirrored on the class — and the two cross-check, so onboarding docs and
    the collector can never disagree about where the record lands."""
    resolved = resolve_agent_declaration(_AGENTS_ROOT / "dsh.yaml", agents_root=_AGENTS_ROOT)
    slots = [entry for entry in resolved.declaration.artifacts if entry.slot is not None]
    assert len(slots) == 1
    assert slots[0].slot == "dsh_session"
    assert slots[0].path == "sessions/session.v4.jsonl.zstd"
    # the class mirrors it exactly
    assert session_record_output_path_of(DshAgent) == slots[0].path
    assert session_record_output_of(DshAgent) == "dsh_session"
    # and the shipped pair reconciles
    assert declaration_class_mismatches(resolved.declaration, DshAgent) == []


def test_deepagent_uses_the_builtin_slot_without_a_declaration():
    """The generic slot needs no declared path — its historical path is the
    framework's compatibility table, and absence on both sides is agreement."""
    resolved = resolve_agent_declaration(
        _AGENTS_ROOT / "deepagent.yaml", agents_root=_AGENTS_ROOT
    )
    assert all(entry.slot is None for entry in resolved.declaration.artifacts)
    assert session_record_output_path_of(DcodeAgent) is None
    assert session_record_output_of(DcodeAgent) == "agent_session_record"
    assert declaration_class_mismatches(resolved.declaration, DcodeAgent) == []


def test_a_slot_path_drift_between_declaration_and_class_is_a_mismatch():
    from aeval.agents.declaration import AgentArtifactDeclaration

    resolved = resolve_agent_declaration(_AGENTS_ROOT / "dsh.yaml", agents_root=_AGENTS_ROOT)

    class Drifting(DshAgent):
        SESSION_RECORD_OUTPUT_PATH = "somewhere/else.jsonl"

    mismatches = declaration_class_mismatches(resolved.declaration, Drifting)
    assert any(
        "artifacts slot path" in message
        and "somewhere/else.jsonl" in message
        for message in mismatches
    ), mismatches


def test_a_class_path_without_a_declaration_entry_is_a_mismatch():
    """The declaration is what onboarding reads; a class-only path would let
    the docs and the collector disagree silently."""
    from aeval.agents.declaration import AgentDeclaration

    resolved = resolve_agent_declaration(_AGENTS_ROOT / "deepagent.yaml", agents_root=_AGENTS_ROOT)

    class Undeclared(DcodeAgent):
        SESSION_RECORD_OUTPUT_PATH = "deepagent/record.json"

    mismatches = declaration_class_mismatches(resolved.declaration, Undeclared)
    assert any(
        "artifacts slot path" in message and "declaration=None" in message
        for message in mismatches
    ), mismatches


def test_a_declared_slot_entry_needs_a_well_formed_shape():
    """Malformed slot entries are refused at load, not discovered mid-run."""
    from pydantic import ValidationError

    from aeval.agents.declaration import AgentArtifactDeclaration

    with pytest.raises(ValidationError, match="slot"):
        AgentArtifactDeclaration(name="x", slot="Bad Slot!", path="p")
    with pytest.raises(ValidationError, match="trial dir"):
        AgentArtifactDeclaration(name="x", slot="own_slot", path="/abs")
    with pytest.raises(ValidationError, match="trial dir"):
        AgentArtifactDeclaration(name="x", slot="own_slot", path="../escape")
    ok = AgentArtifactDeclaration(
        name="x", slot="own_slot", path="own/record.bin"
    )
    assert ok.slot == "own_slot"
