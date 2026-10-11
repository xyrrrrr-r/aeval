"""The DSH official release pin (agents/dsh/release.py) and its cross-repo
invariants.

The pin data is the dsh adapter's own fact (moved out of aeval.provenance in
an earlier refactor): what lives here is that the slice is fully
pinned, that the neutral control package's build pins equal the slice the
trial lock records (they live in ONE repo — drift here means the build and
the attestation disagree), and that the recorded direct-import surface equals
what the sources actually import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aeval.agents.contract import official_release_lock_of
from aeval.agents.dsh.agent import DshAgent
from aeval.agents.dsh.release import (
    DSH_CONTROL_DIRECT_IMPORTS,
    DSH_NPM_SLICE,
    DSH_NODE_VERSIONS,
    OFFICIAL_DSH_TAG,
    build_official_dsh_lock,
)
from aeval.provenance import build_runtime_lock

_CONTROL_ROOT = Path(__file__).resolve().parents[4] / "control"
_REPO_ROOT = _CONTROL_ROOT.parent.parent


def test_the_declared_hook_reaches_the_runtime_lock():
    """The pin must actually land in the runtime lock — the whole point.

    Regression guard: the generic resolver read ``official_release_lock``
    while every adapter declares ``OFFICIAL_RELEASE_LOCK``, so it returned
    None for all of them, no run recorded an agent release lock, and the
    runtime lock's ``dsh`` section stayed null. Nothing failed — a version
    bump simply did not move the lock digest. This test fails loudly on any
    such drift, so the supply-chain identity cannot go missing in silence.
    """
    pinned = official_release_lock_of(DshAgent)
    assert pinned is not None, (
        "DshAgent declares no release lock the generic resolver can find — "
        "the declaration name and OFFICIAL_RELEASE_LOCK_ATTR have drifted "
        "apart again, and no run would record the DSH release"
    )
    assert pinned.official_tag == OFFICIAL_DSH_TAG
    lock = build_runtime_lock(release_locks={"dsh": pinned})
    assert lock.dsh is not None, "the built runtime lock dropped the DSH section"
    assert lock.dsh.official_tag == OFFICIAL_DSH_TAG
    assert lock.dsh.commit
    assert any(p.name == "@deepseek-ai/dsh" for p in lock.dsh.packages)


def test_official_dsh_slice_is_fully_pinned():
    dsh = build_official_dsh_lock()
    by_name = {p.name: p for p in dsh.packages}
    assert by_name["@deepseek-ai/dsh"].integrity is not None
    assert by_name["@deepseek-ai/cordis"].version == "4.0.4"
    assert by_name["@agentclientprotocol/sdk"].version == "1.4.0"
    # schemastery is a standalone library (like cordis), not part of the
    # DSH release train; it carries its own version line.
    assert by_name["@deepseek-ai/schemastery"].version == "3.18.4"
    assert all(
        p.version == "0.2.0-rc.2"
        for n, p in by_name.items()
        if n.startswith("@deepseek-ai/")
        and n not in ("@deepseek-ai/cordis", "@deepseek-ai/schemastery")
    )
    assert dsh.experimental is True
    assert dsh.node_versions == list(DSH_NODE_VERSIONS)


def test_slice_pins_the_control_plugins_direct_imports():
    slice_names = {name for name, _, _ in DSH_NPM_SLICE}
    missing = set(DSH_CONTROL_DIRECT_IMPORTS) - slice_names
    assert not missing, (
        f"control plugin direct imports missing from DSH_NPM_SLICE: {sorted(missing)}"
    )


def test_control_package_pins_match_the_dsh_slice():
    """Defense 3: aeval/control's build pins and the trial lock live in ONE
    repo — every @deepseek-ai/* version in the control package's manifest
    must equal the DSH slice the lock records, or the build drifts from what
    trials attest."""
    manifest = json.loads(
        (_CONTROL_ROOT / "package.json").read_text(encoding="utf-8")
    )
    slice_versions = {name: version for name, version, _ in DSH_NPM_SLICE}
    deps = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    deepseek_deps = {n: v for n, v in deps.items() if n.startswith("@deepseek-ai/")}
    assert deepseek_deps, "aeval/control lost its @deepseek-ai/* pins"
    for name, pinned in deepseek_deps.items():
        assert slice_versions.get(name) == pinned, (
            f"aeval/control pins {name}@{pinned} but DSH_NPM_SLICE records "
            f"{slice_versions.get(name)!r} — build pin and trial lock drifted"
        )


def test_control_plugin_import_surface_matches_recorded_list():
    """Re-measure the real sources against the recorded import surface.

    After the slim-down the deployed control stack is a composition: the
    DSH package's own sources plus the neutral broker cluster in
    aeval/control (whose dist the DSH package composes into its own). The
    invariant covers the union; generated .d.ts shims are excluded because
    they mirror the neutral sources. Skipped (never passed) when the
    sibling checkout is absent.
    """
    dsh_src = _REPO_ROOT / "dsh-eval-control" / "src"
    neutral_src = _CONTROL_ROOT / "src"
    if not dsh_src.is_dir():
        pytest.skip("dsh-eval-control sibling checkout not present (dev layout)")
    measured: set[str] = set()
    for source in sorted(dsh_src.glob("*.ts")):
        if source.name.endswith(".d.ts"):
            continue  # generated shim pulled from aeval/control/dist
        measured.update(
            re.findall(r"from '(@[^']+)'", source.read_text(encoding="utf-8"))
        )
    for source in sorted(neutral_src.glob("*.ts")):
        measured.update(
            re.findall(r"from '(@[^']+)'", source.read_text(encoding="utf-8"))
        )
    assert measured == set(DSH_CONTROL_DIRECT_IMPORTS), (
        "control-stack import surface drifted from DSH_CONTROL_DIRECT_IMPORTS "
        "— update the constant AND the slice"
    )
