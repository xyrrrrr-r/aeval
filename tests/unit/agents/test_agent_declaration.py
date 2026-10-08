"""``agents/<id>.yaml`` — one declaration, inherited like a suite.

Onboarding an agent should cost a declaration, not a core change. The declaration
uses the same inheritance engine as suites, and is proven against the adapter
class it names: the class stays what the runtime reads, which is only safe
because disagreement is an error rather than a silent winner.
"""

from __future__ import annotations

import yaml
import pytest

from aeval.agents.contract import load_adapter_class
from aeval.agents.declaration import (
    AGENT_BASE_FILENAME,
    check_declaration_matches_adapter,
    declaration_class_mismatches,
    discover_agent_declarations,
    resolve_agent_declaration,
)
from aeval.agents.dsh.agent import DshAgent
from aeval.suite_models import SuiteError

BASE = {
    "schema_version": 1,
    "budget_enforcement": "none",
    "write_surface": "ephemeral_overlay",
    "server_side_session": "forbidden",
    "observations": [],
    "provides": [],
    "transcript": {"source": "atif_native", "reader": "r", "capabilities": [], "fields_available": {}},
}


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _declaration(**overrides) -> dict:
    return {
        **BASE,
        "id": "agentx",
        "version": "1",
        "import_path": "aeval.agents.testing.fake_agent:FakeAtifAgent",
        **overrides,
    }


@pytest.fixture
def agents_root(tmp_path):
    _write(tmp_path / "_base" / AGENT_BASE_FILENAME, BASE)
    return tmp_path


def test_the_shipped_declarations_agree_with_their_adapters():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "agents"
    assert discover_agent_declarations(root) == ["deepagent", "dsh", "fakeagent"]
    for agent_id in ("deepagent", "dsh", "fakeagent"):
        resolved = resolve_agent_declaration(root / f"{agent_id}.yaml", agents_root=root)
        adapter = load_adapter_class(resolved.declaration.import_path)
        check_declaration_matches_adapter(resolved.declaration, adapter)  # no raise
        # base first, then the agent's own file
        assert [source.path for source in resolved.sources] == [
            "_base/agent.base.yaml",
            f"{agent_id}.yaml",
        ]


def test_a_child_inherits_and_overrides_the_base(agents_root):
    _write(
        agents_root / "agentx.yaml",
        _declaration(provides=["shell"], budget_enforcement="gateway_lease"),
    )
    resolved = resolve_agent_declaration(agents_root / "agentx.yaml", agents_root=agents_root)
    assert resolved.declaration.id == "agentx"
    # overridden by the child
    assert resolved.declaration.budget_enforcement == "gateway_lease"
    # inherited from the base
    assert resolved.declaration.write_surface == "ephemeral_overlay"
    assert resolved.declaration.provides == ["shell"]


def test_union_lists_append_across_the_chain(agents_root):
    _write(agents_root / "_base" / "capped.base.yaml", {**BASE, "provides": ["shell"]})
    _write(
        agents_root / "agentx.yaml",
        _declaration(extends="_base/capped.base.yaml", provides=["resume"]),
    )
    resolved = resolve_agent_declaration(agents_root / "agentx.yaml", agents_root=agents_root)
    assert resolved.declaration.provides == ["shell", "resume"]


def test_remove_drops_an_inherited_entry_and_refuses_a_missing_one(agents_root):
    _write(agents_root / "_base" / "offers.base.yaml", {**BASE, "provides": ["shell", "resume"]})
    _write(
        agents_root / "agentx.yaml",
        _declaration(extends="_base/offers.base.yaml", remove={"provides": ["resume"]}),
    )
    resolved = resolve_agent_declaration(agents_root / "agentx.yaml", agents_root=agents_root)
    assert resolved.declaration.provides == ["shell"]

    _write(
        agents_root / "bady.yaml",
        _declaration(extends="_base/offers.base.yaml", remove={"provides": ["browser"]}),
    )
    with pytest.raises(SuiteError, match="absent"):
        resolve_agent_declaration(agents_root / "bady.yaml", agents_root=agents_root)


def test_a_cycle_and_a_too_deep_chain_are_refused(agents_root):
    _write(agents_root / "_base" / "a.base.yaml", {**BASE, "extends": "_base/b.base.yaml"})
    _write(agents_root / "_base" / "b.base.yaml", {**BASE, "extends": "_base/a.base.yaml"})
    _write(agents_root / "cyc.yaml", _declaration(extends="_base/a.base.yaml"))
    with pytest.raises(SuiteError, match="cycle"):
        resolve_agent_declaration(agents_root / "cyc.yaml", agents_root=agents_root)

    previous = None
    for index in range(6):
        name = f"_base/d{index}.base.yaml"
        _write(agents_root / name, {**BASE, **({"extends": previous} if previous else {})})
        previous = name
    _write(agents_root / "deep.yaml", _declaration(extends=previous))
    with pytest.raises(SuiteError, match="deeper than"):
        resolve_agent_declaration(agents_root / "deep.yaml", agents_root=agents_root)


def test_a_declaration_that_disagrees_with_its_adapter_is_refused(agents_root):
    # fakeagent covers {shell, atif_native} — claiming resume is a lie
    _write(
        agents_root / "agentx.yaml",
        _declaration(id="fakeagent", provides=["shell", "resume"]),
    )
    resolved = resolve_agent_declaration(agents_root / "agentx.yaml", agents_root=agents_root)
    mismatches = declaration_class_mismatches(resolved.declaration, load_adapter_class(resolved.declaration.import_path))
    assert any(item.startswith("provides:") for item in mismatches)
    with pytest.raises(SuiteError, match="disagrees with"):
        check_declaration_matches_adapter(resolved.declaration, load_adapter_class(resolved.declaration.import_path))


def test_a_declaration_may_only_extend_a_base_file(agents_root):
    _write(agents_root / "other.yaml", _declaration(id="other"))
    _write(agents_root / "agentx.yaml", _declaration(extends="other.yaml"))
    with pytest.raises(SuiteError, match="may only extend a base file"):
        resolve_agent_declaration(agents_root / "agentx.yaml", agents_root=agents_root)


def test_the_shipped_dsh_declaration_matches_the_class_it_names():
    """A declaration drifting from its adapter must fail here, not in the lab."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "agents"
    resolved = resolve_agent_declaration(root / "dsh.yaml", agents_root=root)
    declaration = resolved.declaration
    assert declaration.id == DshAgent.ADAPTER_ID
    assert declaration.provides == sorted(DshAgent.PROVIDES) or set(declaration.provides) == set(DshAgent.PROVIDES)
    assert declaration.observations == list(DshAgent.REQUIRED_OBSERVATIONS)
    assert declaration.sandbox_home == DshAgent.SANDBOX_HOME
    assert declaration.session_artifact_dir == DshAgent.SESSION_ARTIFACT_DIR
    assert declaration_class_mismatches(declaration, DshAgent) == []
