"""Suite inheritance: `extends`, per-field merge semantics, and chain identity.

The point of these tests is not that merging works, but that it stays
*decidable*: which facts may be inherited, what a removal means, and that a
base edit moves the identity of every suite that inherits it (the property
the fail-closed manifest gate depends on).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aeval.suite_loader.loader import discover_suites, load_suite
from aeval.suite_loader.transfer import _transfer  # noqa: PLC2701 - internal by design
from aeval.suite_models import SuiteError

BASE_CONVENTIONS = {
    "schema_version": 2,
    "clock": {"mode": "real"},
    "baselines": [{"id": "ready", "probe": "observable:ready", "equals": "true"}],
    "observables": [{"name": "ready", "type": "string", "source": "file:/workspace/ready"}],
    "verdict": {
        "requirements": ["input_complete", "agent_finished"],
        "graders": {},
    },
    "driver": {"require": ["acp_stdio"]},
    "metrics": [{"id": "reliability", "kind": "pass_pow_k"}],
    "harbor": {"dataset": "datasets/local.yaml"},
}

CHILD_SPECIFIC = {
    "schema_version": 2,
    "id": "child-suite",
    "version": "0.1.0",
    "harbor": {"job": "jobs/local.yaml"},
    "observables": [{"name": "reward", "type": "string", "source": "file:/logs/reward.txt"}],
    "verdict": {"graders": {"default": {"impl": "graders/outcome.py@v1", "layer": "outcome"}}},
    "metrics": [{"id": "reliability", "kind": "pass_pow_k", "k": 3}],
    "provenance": {"source": "authored-internally", "license": "MIT"},
}


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture()
def suites_root(tmp_path) -> Path:
    """A real suites root: bases live in `_base/`, suites beside it."""
    root = tmp_path / "suites"
    _write(root / "_base" / "harbor.base.yaml", BASE_CONVENTIONS)
    return root


def _child(root: Path, name: str = "child-suite", **overrides) -> Path:
    data = {**CHILD_SPECIFIC, "extends": "_base/harbor.base.yaml", **overrides}
    data.setdefault("id", name)
    return _write(root / name / "suite.yaml", data)


def test_child_without_extends_is_the_only_source(suites_root):
    # A base-less suite carries the conventions itself (nothing to inherit).
    solo = {
        **BASE_CONVENTIONS,
        **CHILD_SPECIFIC,
        "id": "solo",
        "harbor": {"dataset": "datasets/local.yaml", "job": "jobs/local.yaml"},
        "verdict": {**BASE_CONVENTIONS["verdict"], **CHILD_SPECIFIC["verdict"]},
    }
    path = _write(suites_root / "solo" / "suite.yaml", solo)
    suite = load_suite(path.parent)
    assert suite.extends == []
    assert [(s.role, s.path) for s in suite.sources] == [("child", "solo/suite.yaml")]
    # Chain digest is always present, even without inheritance.
    assert suite.identity_digest == suite.overlay_chain_digest
    assert suite.identity() == ("solo", "0.1.0", suite.overlay_chain_digest)


def test_inherits_base_conventions_and_records_sources(suites_root):
    suite = load_suite(_child(suites_root).parent)
    assert suite.extends == ["_base/harbor.base.yaml"]
    assert [(s.role, s.path) for s in suite.sources] == [
        ("base", "_base/harbor.base.yaml"),
        ("child", "child-suite/suite.yaml"),
    ]
    # Inherited facts really landed with no restatement in the child.
    assert suite.overlay.clock.mode == "real"
    assert [o.name for o in suite.overlay.observables] == ["ready", "reward"]
    assert [b.id for b in suite.overlay.baselines] == ["ready"]
    assert suite.overlay.harbor.dataset == "datasets/local.yaml"


def test_keyed_list_replaces_same_key_and_appends_new(suites_root):
    child = {
        **CHILD_SPECIFIC,
        "id": "child-suite",
        "metrics": [{"id": "reliability", "kind": "pass_pow_k", "k": 5},
                    {"id": "cost", "kind": "cost_normalized"}],
    }
    _write(suites_root / "child-suite" / "suite.yaml", {**child, "extends": "_base/harbor.base.yaml"})
    suite = load_suite(suites_root / "child-suite")
    assert [(m.id, m.k) for m in suite.overlay.metrics] == [("reliability", 5), ("cost", None)]


def test_union_lists_append_without_duplicates(suites_root):
    child = {
        **CHILD_SPECIFIC,
        "driver": {"require": ["acp_stdio", "sdk_jsonrpc"], "workspace_dir": "/app"},
        "verdict": {
            "requirements": ["agent_finished", "render_valid"],
            "graders": CHILD_SPECIFIC["verdict"]["graders"],
        },
    }
    _write(suites_root / "child-suite" / "suite.yaml", {**child, "extends": "_base/harbor.base.yaml"})
    suite = load_suite(suites_root / "child-suite")
    assert suite.overlay.verdict.requirements == [
        "input_complete", "agent_finished", "render_valid",
    ]
    assert suite.overlay.driver.require == ["acp_stdio", "sdk_jsonrpc"]
    assert suite.overlay.driver.workspace_dir == "/app"


def test_remove_drops_an_inherited_entry(suites_root):
    child = {**CHILD_SPECIFIC, "remove": {"observables": ["ready"], "requirements": ["input_complete"]}}
    _write(suites_root / "child-suite" / "suite.yaml", {**child, "extends": "_base/harbor.base.yaml"})
    suite = load_suite(suites_root / "child-suite")
    assert [o.name for o in suite.overlay.observables] == ["reward"]
    assert suite.overlay.verdict.requirements == ["agent_finished"]


def test_removing_something_that_is_absent_is_refused(suites_root):
    child = {**CHILD_SPECIFIC, "remove": {"observables": ["nope"]}}
    _write(suites_root / "child-suite" / "suite.yaml", {**child, "extends": "_base/harbor.base.yaml"})
    with pytest.raises(SuiteError, match="absent"):
        load_suite(suites_root / "child-suite")


def test_base_may_not_restate_a_harbor_owned_fact(suites_root):
    _write(suites_root / "_base" / "bad.base.yaml", {**BASE_CONVENTIONS, "trials": {"k": 3}})
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/bad.base.yaml"},
    )
    with pytest.raises(SuiteError, match="Harbor-owned"):
        load_suite(suites_root / "child-suite")


@pytest.mark.parametrize("key", ["id", "version"])
def test_base_may_not_declare_identity(suites_root, key):
    _write(suites_root / "_base" / "bad.base.yaml", {**BASE_CONVENTIONS, key: "inherited"})
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/bad.base.yaml"},
    )
    with pytest.raises(SuiteError, match="bases hold conventions only"):
        load_suite(suites_root / "child-suite")


def test_base_may_not_select_the_job(suites_root):
    _write(
        suites_root / "_base" / "bad.base.yaml",
        {**BASE_CONVENTIONS, "harbor": {"dataset": "datasets/local.yaml", "job": "jobs/other.yaml"}},
    )
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/bad.base.yaml"},
    )
    with pytest.raises(SuiteError, match="harbor.job"):
        load_suite(suites_root / "child-suite")


def test_provenance_is_not_inherited_silently(suites_root):
    _write(
        suites_root / "_base" / "prov.base.yaml",
        {**BASE_CONVENTIONS, "provenance": {"source": "upstream", "license": "Apache-2.0"}},
    )
    child = {k: v for k, v in CHILD_SPECIFIC.items() if k != "provenance"}
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**child, "extends": "_base/prov.base.yaml"},
    )
    with pytest.raises(SuiteError, match="provenance"):
        load_suite(suites_root / "child-suite")

    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**child, "extends": "_base/prov.base.yaml", "provenance": "inherit"},
    )
    suite = load_suite(suites_root / "child-suite")
    assert suite.overlay.provenance.source == "upstream"


def test_inherited_veto_grader_is_refused(suites_root):
    _write(
        suites_root / "_base" / "veto.base.yaml",
        {
            **BASE_CONVENTIONS,
            "verdict": {
                "requirements": ["input_complete"],
                "graders": {"trajectory": {"impl": "graders/traj.py@v1", "layer": "trajectory", "veto": True}},
            },
        },
    )
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/veto.base.yaml"},
    )
    with pytest.raises(SuiteError, match="veto"):
        load_suite(suites_root / "child-suite")


def test_missing_base_and_traversal_are_refused(suites_root):
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/nope.base.yaml"},
    )
    with pytest.raises(SuiteError, match="not found"):
        load_suite(suites_root / "child-suite")

    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "../outside.base.yaml"},
    )
    with pytest.raises(SuiteError, match="portable relative path"):
        load_suite(suites_root / "child-suite")


def test_a_base_must_not_be_named_suite_yaml(suites_root):
    _write(suites_root / "other" / "suite.yaml", BASE_CONVENTIONS)
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "other/suite.yaml"},
    )
    with pytest.raises(SuiteError, match="must not be named suite.yaml"):
        load_suite(suites_root / "child-suite")


def test_inheritance_cycle_is_refused(suites_root):
    _write(suites_root / "_base" / "one.base.yaml", {**BASE_CONVENTIONS, "extends": "_base/two.base.yaml"})
    _write(suites_root / "_base" / "two.base.yaml", {**BASE_CONVENTIONS, "extends": "_base/one.base.yaml"})
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/one.base.yaml"},
    )
    with pytest.raises(SuiteError, match="cycle|deeper than"):
        load_suite(suites_root / "child-suite")


def test_over_deep_chain_is_refused(suites_root):
    for level in range(6):
        _write(
            suites_root / "_base" / f"chain{level}.base.yaml",
            {**BASE_CONVENTIONS, "extends": f"_base/chain{level + 1}.base.yaml"},
        )
    _write(suites_root / "_base" / "chain6.base.yaml", BASE_CONVENTIONS)
    _write(
        suites_root / "child-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/chain0.base.yaml"},
    )
    with pytest.raises(SuiteError, match="deeper than"):
        load_suite(suites_root / "child-suite")


def test_editing_a_base_changes_every_dependent_identity(suites_root):
    suite_yaml = _child(suites_root)
    before = load_suite(suite_yaml.parent)
    # Reword an inherited convention: the child file is untouched.
    changed = {**BASE_CONVENTIONS, "driver": {"require": ["acp_stdio", "sdk_jsonrpc"]}}
    _write(suites_root / "_base" / "harbor.base.yaml", changed)
    after = load_suite(suite_yaml.parent)
    assert before.suite_yaml_digest == after.suite_yaml_digest  # raw child bytes unchanged
    assert before.identity_digest != after.identity_digest
    assert before.identity() != after.identity()


def test_discover_suites_skips_underscore_bases(suites_root):
    _child(suites_root)
    _write(suites_root / "solo" / "suite.yaml", {**CHILD_SPECIFIC, "id": "solo"})
    discovered = discover_suites([suites_root])
    assert sorted(path.name for path in discovered) == ["child-suite", "solo"]


def test_transfer_refuses_a_suite_with_inherited_bases(suites_root, tmp_path):
    suite_dir = _child(suites_root).parent
    with pytest.raises(SuiteError, match="inherits"):
        _transfer(suite_dir, tmp_path / "exported", format="harbor-task", version="0.2.0")


# --- the fail-closed gate the manifest/plugin path relies on -----------------

def _manifest_overlay(suite, **overrides):
    from aeval.contracts import OverlayIdentity

    values = {
        "suite_id": suite.id,
        "suite_version": suite.version,
        "overlay_digest": suite.suite_yaml_digest,
        "overlay_chain_digest": suite.identity_digest,
        "source_commit": "a" * 40,
    }
    values.update(overrides)
    return OverlayIdentity(**values)


def test_gate_accepts_the_same_chain_and_rejects_a_base_edit(suites_root):
    from aeval.hooks.plugin import suite_identity_matches

    suite_yaml = _child(suites_root)
    suite = load_suite(suite_yaml.parent)
    assert suite_identity_matches(_manifest_overlay(suite), suite) is True

    # A base edited after the intent manifest was written: the child file is
    # byte-identical, so only the chain digest can catch it.
    _write(suites_root / "_base" / "harbor.base.yaml", {**BASE_CONVENTIONS, "driver": {"require": ["sdk_jsonrpc"]}})
    edited = load_suite(suite_yaml.parent)
    assert edited.suite_yaml_digest == suite.suite_yaml_digest
    assert suite_identity_matches(_manifest_overlay(suite), edited) is False


def test_gate_still_accepts_legacy_manifests_without_a_chain_digest(suites_root):
    from aeval.hooks.plugin import suite_identity_matches

    suite = load_suite(_child(suites_root).parent)
    legacy = _manifest_overlay(suite, overlay_chain_digest=None)
    assert suite_identity_matches(legacy, suite) is True
    # ...but the raw-bytes check still bites
    assert suite_identity_matches(_manifest_overlay(suite, overlay_digest="0" * 64), suite) is False
    assert suite_identity_matches(_manifest_overlay(suite, overlay_chain_digest="0" * 64), suite) is False
    assert suite_identity_matches(_manifest_overlay(suite, suite_version="9.9.9"), suite) is False


def test_suite_may_declare_a_spend_cap(suites_root):
    """budget is an aeval fact (Harbor has no budget field), so a suite may cap it."""
    _write(
        suites_root / "_base" / "capped.base.yaml",
        {**BASE_CONVENTIONS, "budget": {"max_tokens": 5000}},
    )
    _write(
        suites_root / "capped-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/capped.base.yaml"},
    )
    inherited = load_suite(suites_root / "capped-suite")
    assert inherited.overlay.budget is not None
    assert inherited.overlay.budget.max_tokens == 5000

    # a child adds a constraint by declaring its own: nested mappings are
    # deep-merged, which for a spend cap is the fail-safe direction — inheriting
    # cannot silently drop the base's cap (drop it with `remove: [budget]`).
    _write(
        suites_root / "narrowed-suite" / "suite.yaml",
        {**CHILD_SPECIFIC, "extends": "_base/capped.base.yaml", "budget": {"max_seconds": 30}},
    )
    narrowed = load_suite(suites_root / "narrowed-suite")
    assert narrowed.overlay.budget.max_seconds == 30
    assert narrowed.overlay.budget.max_tokens == 5000
