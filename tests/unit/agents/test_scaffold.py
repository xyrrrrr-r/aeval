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
    discover_agent_declarations,
    resolve_agent_declaration,
)
from aeval.agents.scaffold import (
    STARTER_DECLARATIONS,
    _class_name,
    scaffold_agent,
    write_starter_declarations,
)
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
        root, "agentb", package_dir=tmp_path / "pkg", provides=["shell", "file_tools"]
    )
    resolved = resolve_agent_declaration(result.declaration_path, agents_root=root)
    declaration = resolved.declaration
    assert declaration.id == "agentb"
    assert declaration.provides == ["file_tools", "shell"]
    assert declaration.budget_enforcement == "none"
    # the module was written and is syntactically valid Python
    source = result.module_path.read_text(encoding="utf-8")
    compile(source, str(result.module_path), "exec")
    assert _class_name("agentb") in source
    assert "ADAPTER_ID" in source and "read_trial_session" in source


def test_the_scaffolded_class_agrees_with_its_own_declaration(root, tmp_path, monkeypatch):
    """The drift this generator exists to prevent, in the generator itself.

    The generated declaration and the generated class must agree on the spot:
    ``aeval agents`` refuses a disagreement at load, so a skeleton that ships
    with one hands the user a failure as step one. Measured on the previous
    template: ``adapter declares no ['TRANSCRIPT_CAPABILITY']`` plus missing
    members ``paths`` and ``agent_session_id``.
    """
    from aeval.agents.contract import (
        adapter_declaration_gap,
        adapter_member_gap,
        load_adapter_class,
    )

    result = scaffold_agent(
        root,
        "agreeing",
        package_dir=tmp_path / "pkg",
        provides=["shell"],
        transcript_source="atif_native",
    )
    monkeypatch.syspath_prepend(str(tmp_path / "pkg"))
    resolved = resolve_agent_declaration(result.declaration_path, agents_root=root)
    adapter = load_adapter_class(resolved.declaration.import_path)
    assert adapter_member_gap(adapter) == []
    assert adapter_declaration_gap(adapter) == []
    assert declaration_class_mismatches(resolved.declaration, adapter) == []
    # and the deliberate failure mode is still deliberate: the reader is a
    # skeleton, so it is honest about being unimplemented rather than passing.
    with pytest.raises(NotImplementedError):
        adapter().read_trial_session()


def test_the_import_path_follows_where_the_module_landed(root, tmp_path):
    """``--package-dir`` moves the import root; the declaration must follow it.

    Same module, two layouts, two different true import paths. The template
    used to hardcode ``aeval.agents.<id>.agent`` — correct only for the in-repo
    default, and silently wrong (``No module named ...``) for every other
    ``--package-dir``.
    """
    plain = scaffold_agent(root, "plainroot", package_dir=tmp_path / "pkg")
    assert plain.import_path == "plainroot.agent:PlainrootAgent"

    nested_root = tmp_path / "src"
    (nested_root / "aeval").mkdir(parents=True)
    (nested_root / "aeval" / "__init__.py").write_text("", encoding="utf-8")
    agents_dir = nested_root / "aeval" / "agents"
    agents_dir.mkdir()
    (agents_dir / "__init__.py").write_text("", encoding="utf-8")

    nested = scaffold_agent(root, "nestedone", package_dir=agents_dir)
    assert nested.import_path == "aeval.agents.nestedone.agent:NestedoneAgent"

    # an explicit --import-path still wins over the derived answer
    override = scaffold_agent(
        root, "overridden", package_dir=tmp_path / "pkg2",
        import_path="somewhere.else:TheClass",
    )
    assert override.import_path == "somewhere.else:TheClass"


def test_a_relative_package_dir_walks_up_without_spinning(tmp_path, monkeypatch):
    """``--package-dir src`` is relative on the command line, so the walk must
    be absolute and must terminate.

    Regression: walking up from a relative path never terminates once it reaches
    ``"."`` — ``Path(".").parent`` is ``Path(".")`` and ``Path(".").name``
    is ``""``, so the loop appended empty components forever.
    """
    from aeval.agents.scaffold import _derive_import_path

    monkeypatch.chdir(tmp_path)
    module_dir = Path("src") / "relagent"
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    # make every level up to and including the cwd a package: this is the shape
    # that used to spin, because it walks the relative path past "."
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")

    assert _derive_import_path(module_dir, "RelagentAgent") == (
        f"{tmp_path.name}.src.relagent.agent:RelagentAgent"
    )


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


# --- starter declarations for an installed aeval ------------------------


def test_the_starters_are_the_repo_declarations_byte_for_byte(tmp_path):
    """The shipped copy is made from the repo's agents/ at build time; if the
    two ever disagree, an installed aeval hands users a stale declaration."""
    written = write_starter_declarations(tmp_path / "agents")
    assert [p.relative_to(tmp_path / "agents").as_posix() for p in written] == list(
        STARTER_DECLARATIONS
    )
    for path in written:
        assert path.read_bytes() == (AGENTS / path.relative_to(tmp_path / "agents")).read_bytes()


def test_the_starters_land_where_the_run_looks_for_them(tmp_path):
    """Not just written — discoverable: an empty directory is the failure this
    replaces, so the proof is that discovery finds an agent afterwards."""
    agents_dir = tmp_path / "agents"
    write_starter_declarations(agents_dir)
    assert discover_agent_declarations(agents_dir) == ["dsh"]


def test_an_existing_declaration_is_not_overwritten_without_force(tmp_path):
    agents_dir = tmp_path / "agents"
    write_starter_declarations(agents_dir)
    (agents_dir / "dsh.yaml").write_text("# mine\n", encoding="utf-8")
    with pytest.raises(SuiteError, match="pass --force"):
        write_starter_declarations(agents_dir)
    assert (agents_dir / "dsh.yaml").read_text(encoding="utf-8") == "# mine\n"
    write_starter_declarations(agents_dir, force=True)
    assert (agents_dir / "dsh.yaml").read_bytes() == (AGENTS / "dsh.yaml").read_bytes()


def test_a_template_root_without_the_starters_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("aeval.agents.scaffold.starter_templates_dir", lambda: None)
    with pytest.raises(SuiteError, match="no shipped agent declarations"):
        write_starter_declarations(tmp_path / "agents")


def test_the_build_ships_the_starters_inside_the_package():
    """`--init` reads from the package; if the build stops copying agents/ there,
    the command breaks only after release. Assert the packaging contract here."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'force-include = { "agents" = "aeval/_templates/agents" }' in pyproject
