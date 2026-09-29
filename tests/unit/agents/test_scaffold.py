"""The first two onboarding steps, as one command.

A skeleton that satisfies the contract is worth more than prose about the
contract: it removes the failure mode where a new adapter starts a run and dies
midway through evidence collection.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aeval.agents.declaration import (
    declaration_class_mismatches,
    resolve_agent_declaration,
)
from aeval.agents.scaffold import _class_name, scaffold_agent
from aeval.suite_models import SuiteError

REPO = Path(__file__).resolve().parents[3]
AGENTS = REPO / "agents"


@pytest.fixture
def root(tmp_path):
    """A scaffoldable agents root: the base file is what makes it work."""
    shutil.copytree(AGENTS, tmp_path / "agents")
    return tmp_path / "agents"


def test_a_scaffolded_declaration_is_valid_immediately(root, tmp_path):
    result = scaffold_agent(
        root, "deepagent", package_dir=tmp_path / "pkg", provides=["shell", "file_tools"]
    )
    resolved = resolve_agent_declaration(result.declaration_path, agents_root=root)
    declaration = resolved.declaration
    assert declaration.id == "deepagent"
    assert declaration.provides == ["file_tools", "shell"]
    assert declaration.budget_enforcement == "none"
    # the module was written and is syntactically valid Python
    source = result.module_path.read_text(encoding="utf-8")
    compile(source, str(result.module_path), "exec")
    assert _class_name("deepagent") in source
    assert "ADAPTER_ID" in source and "read_trial_session" in source


def test_a_declaration_that_misnames_an_existing_adapter_is_detected(root, tmp_path):
    """Identity is not cosmetic: a declaration must carry the adapter's own id."""
    from aeval.agents.contract import load_adapter_class

    result = scaffold_agent(
        root,
        "fakeagent2",
        import_path="aeval.agents.testing.fake_agent:FakeAtifAgent",
        package_dir=tmp_path / "pkg",
        provides=["shell", "atif_native"],
        transcript_source="atif_native",
    )
    resolved = resolve_agent_declaration(result.declaration_path, agents_root=root)
    adapter = load_adapter_class(resolved.declaration.import_path)
    mismatches = declaration_class_mismatches(resolved.declaration, adapter)
    # the scaffolded id differs from the adapter it points at, and that is caught
    assert any(item.startswith("id:") for item in mismatches), mismatches


def test_an_unenforceable_budget_claim_is_refused(root, tmp_path):
    with pytest.raises(SuiteError, match="control stack"):
        scaffold_agent(root, "x", budget_enforcement="gateway_lease", package_dir=tmp_path)


def test_a_bad_vocabulary_is_refused_with_the_options(root, tmp_path):
    with pytest.raises(SuiteError, match="mode must be one of"):
        scaffold_agent(root, "x", mode="magic", package_dir=tmp_path)
    with pytest.raises(SuiteError, match="transcript source"):
        scaffold_agent(root, "x", transcript_source="proprietary", package_dir=tmp_path)


def test_an_existing_declaration_is_not_clobbered(root, tmp_path):
    scaffold_agent(root, "twice", package_dir=tmp_path)
    with pytest.raises(SuiteError, match="already exists"):
        scaffold_agent(root, "twice", package_dir=tmp_path)
    scaffold_agent(root, "twice", package_dir=tmp_path, force=True)  # explicit overwrite


def test_a_root_without_the_base_is_refused_with_the_fix(tmp_path):
    with pytest.raises(SuiteError, match="_base"):
        scaffold_agent(tmp_path / "empty", "x", package_dir=tmp_path)
