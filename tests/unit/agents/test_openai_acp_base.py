"""Acceptance: a fake OpenAI-protocol ACP agent, zero adapter code.

The point of the family base (:class:`aeval.agents.openai_acp.OpenAiAcpAgent`)
is that adding a sibling CLI stops being an adapter-writing task. This
module proves it the only way that counts: a
declaration under a temporary agents root names the base, carries its own
facts (protocol, env spellings, registry entry), and the resulting adapter
passes the whole conformance kit — with no class, no module and no code
anywhere for that agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeval.agents.conformance import run_conformance
from aeval.agents.contract import (
    adapter_declaration_gap,
    load_adapter_class,
    session_record_output_of,
)
from aeval.agents.declaration import (
    declaration_class_mismatches,
    materialize_declaration_adapter,
    resolve_agent_declaration,
)
from aeval.agents.deepagent.agent import (
    DcodeAgent,
    default_deepagent_registry_entry,
)
from aeval.agents.openai_acp import OpenAiAcpAgent
from aeval.contracts import FACADE_API_KEY_PLACEHOLDER, FACADE_BASE_URL
from aeval.suite_models import SuiteError

AGENTS_ROOT = Path(__file__).resolve().parents[3] / "agents"
DEEPAGENT_DECLARATION = AGENTS_ROOT / "deepagent.yaml"

#: The whole "adapter" for the fake CLI: a declaration. Its env spellings are
#: deliberately NOT dcode's (FAKE_*, no alt spelling) so the test can tell
#: declaration-driven routing from the family default.
FAKE_DECLARATION = """\
schema_version: 1
id: fake-openai
version: "1"
import_path: "aeval.agents.openai_acp:OpenAiAcpAgent"

provides:
  - acp_stdio
  - shell
  - file_tools

mode: acp_stdio
budget_enforcement: gateway_lease
control_stack: deepagent-facade

model_routing:
  agent_protocol: openai_chat
  env:
    base_url: FAKE_BASE_URL
    api_key: FAKE_API_KEY

transcript:
  source: native_session_via_bridge
  reader: harbor-acp-runner
  capabilities:
    - atif_via_bridge
    - token_usage
  fields_available:
    events: ok
    token_usage: partial

terminal_descriptor_owner: host
sandbox_session_record: /logs/agent/acp-summary.json

launch:
  default:
    kwargs:
      registry_entry:
        id: fake-cli
        name: Fake CLI
        version: "0.1.0"
        description: fakecli exposed as an ACP server
        distribution:
          local:
            cmd: fakecli
            args: ["--acp"]
            env: {}
"""

FAKE_REGISTRY_ENTRY = {
    "id": "fake-cli",
    "name": "Fake CLI",
    "version": "0.1.0",
    "description": "fakecli exposed as an ACP server",
    "distribution": {"local": {"cmd": "fakecli", "args": ["--acp"], "env": {}}},
}


@pytest.fixture()
def fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    (root / "fake-openai.yaml").write_text(FAKE_DECLARATION, encoding="utf-8")
    return root


def _resolved(fake_root: Path):
    return resolve_agent_declaration(
        fake_root / "fake-openai.yaml", agents_root=fake_root
    )


def test_a_fake_openai_acp_agent_passes_conformance_with_zero_code(
    fake_root: Path,
) -> None:
    """The 4.1 acceptance: declaration in, conformant adapter out — no code."""
    resolved = _resolved(fake_root)
    adapter = resolved.declaration.adapter_class()

    report = run_conformance(
        adapter,
        import_path=resolved.declaration.runtime_import_path(),
        declaration_path=fake_root / "fake-openai.yaml",
        agents_root=fake_root,
        required_capabilities=["acp_stdio", "shell"],
        session_record="agent_session_record",
    )

    assert report.ok, report.render()
    assert not report.failures, report.render()
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["declaration"] == "pass"
    assert statuses["contract"] == "pass"
    assert statuses["capabilities"] == "pass"
    # honest skips, not silent passes: no suite budget, no live trial to read
    assert statuses["accounting"] == "skipped"
    assert statuses["transcript"] == "skipped"


def test_the_materialized_class_is_built_from_the_declaration(
    fake_root: Path,
) -> None:
    resolved = _resolved(fake_root)
    adapter = resolved.declaration.adapter_class()

    # behavior from the family base, facts from the declaration
    assert adapter.__bases__ == (OpenAiAcpAgent,)
    assert adapter.__name__ == "FakeOpenai"
    assert adapter.__module__ == "aeval.agents.openai_acp"
    assert declaration_class_mismatches(resolved.declaration, adapter) == []
    assert adapter.name() == "fake-openai"
    assert adapter.ADAPTER_ID == "fake-openai"
    assert adapter.CONTROL_STACK == "deepagent-facade"
    assert session_record_output_of(adapter) == "agent_session_record"
    # stable identity: same declaration -> same class object
    assert resolved.declaration.adapter_class() is adapter


def test_the_launch_entry_names_the_materialized_class(
    fake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Harbor resolves the agent entry's import_path; for a declared agent
    that path must be the materialized class, not the bare base."""
    resolved = _resolved(fake_root)
    entry = resolved.declaration.launch_entry()

    assert entry["import_path"] == "aeval.agents.openai_acp:agent_fake_openai"
    assert (
        entry["kwargs"]["registry_entry"]["distribution"]["local"]["cmd"] == "fakecli"
    )

    # and that path round-trips through the module's lazy materialization
    monkeypatch.setattr(
        "aeval.agents.declaration.default_agents_root",
        lambda start=None: fake_root,
    )
    loaded = load_adapter_class(entry["import_path"])
    assert loaded is resolved.declaration.adapter_class()


def test_the_constructor_injects_the_declared_routing(
    fake_root: Path, tmp_path: Path
) -> None:
    """env spellings are the DECLARATION's, not the family's: the fake CLI
    reads FAKE_BASE_URL/FAKE_API_KEY, so those are what the facade env sets."""
    adapter = _resolved(fake_root).declaration.adapter_class()
    agent = adapter(tmp_path / "logs", registry_entry=FAKE_REGISTRY_ENTRY)

    entry = getattr(agent, "_registry_entry", None)
    assert entry is not None, "the adapter exposed no registry entry"
    env = dict(entry.distribution.local.env)
    assert env == {
        "FAKE_BASE_URL": FACADE_BASE_URL,
        "FAKE_API_KEY": FACADE_API_KEY_PLACEHOLDER,
    }


def test_a_declared_agent_launches_no_cli_without_a_registry_entry(
    fake_root: Path, tmp_path: Path
) -> None:
    """Which CLI to run is a declaration fact: without it the base refuses
    rather than inventing one."""
    adapter = _resolved(fake_root).declaration.adapter_class()

    with pytest.raises(SuiteError, match="launches no CLI"):
        adapter(tmp_path / "logs")


def test_the_behavior_base_alone_is_not_an_agent() -> None:
    """Using the base directly is refused: it carries the family's behavior,
    not any agent's facts (a declaration or a pin must say which CLI)."""
    gap = adapter_declaration_gap(OpenAiAcpAgent)
    assert any("MODEL_ROUTING" in item for item in gap), gap


def test_the_pinned_dcode_adapter_is_not_materialized() -> None:
    """The dcode adapter keeps its facts in code; its declaration mirrors them
    and the two are cross-checked. It must NOT be re-materialized from the
    declaration, or the pinned registry entry would be lost."""
    assert DcodeAgent.DECLARATION_DRIVEN is False
    assert DcodeAgent.__bases__ == (OpenAiAcpAgent,)

    resolved = resolve_agent_declaration(DEEPAGENT_DECLARATION, agents_root=AGENTS_ROOT)
    assert resolved.declaration.adapter_class() is DcodeAgent
    assert (
        resolved.declaration.launch_entry()["import_path"]
        == "aeval.agents.deepagent.agent:DcodeAgent"
    )
    assert declaration_class_mismatches(resolved.declaration, DcodeAgent) == []
    assert (
        default_deepagent_registry_entry()["distribution"]["local"]["cmd"] == "dcode"
    )

    with pytest.raises(SuiteError, match="pinned adapter class"):
        materialize_declaration_adapter(resolved.declaration)


def test_the_materialized_class_still_reads_a_real_trial(
    fake_root: Path, tmp_path: Path
) -> None:
    """The family read-back works for a declared agent too: the ACP runner's
    summary and trajectory are the same facts, wherever the CLI came from."""
    adapter = _resolved(fake_root).declaration.adapter_class()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "acp-summary.json").write_text(
        json.dumps(
            {
                "session": {"sessionId": "fake-session-1"},
                "prompt_response": {
                    "stopReason": "end_turn",
                    "usage": {"inputTokens": 3, "outputTokens": 4},
                },
            }
        ),
        encoding="utf-8",
    )
    agent = adapter(logs_dir, registry_entry=FAKE_REGISTRY_ENTRY)

    assert agent.agent_session_id == "fake-session-1"
    assert agent.read_session_record() == (logs_dir / "acp-summary.json").read_bytes()
