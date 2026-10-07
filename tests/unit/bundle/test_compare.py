"""Manifest comparability tests (plan §7 row 12): incomparable ≠ averageable."""

from __future__ import annotations

from aeval.bundle.attestation import compare_manifests
from aeval.contracts import OverlayIdentity, RunManifest, VersionsBundle


def _manifest(run_id="r1", **overrides) -> RunManifest:
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.provenance import build_runtime_lock

    manifest = RunManifest(
        run_id=run_id,
        # a dsh run's lock: the adapter hook contributed its pin (the
        # agent-neutral builder cannot name an agent)
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )
    for field, value in overrides.items():
        setattr(manifest, field, value)
    return manifest


def test_identical_manifests_are_comparable():
    report = compare_manifests(_manifest(), _manifest())
    assert report.comparable is True
    assert report.first_difference() is None


def test_overlay_difference_blocks_comparison():
    right = _manifest(overlay=OverlayIdentity(
        suite_id="s", suite_version="2", overlay_digest="e" * 64,
        source_commit="9" * 40,
    ))
    report = compare_manifests(_manifest(), right)
    assert report.comparable is False
    assert report.first_difference().startswith("overlay")


def test_budget_point_difference_blocks_comparison():
    report = compare_manifests(_manifest(), _manifest(budget_enforcement_point="gateway_lease"))
    assert "budget" in report
    assert report.comparable is False


def test_harbor_commit_difference_blocks_comparison():
    right = _manifest()
    right.runtime_lock.harbor.commit = "0" * 40
    report = compare_manifests(_manifest(), right)
    assert "harbor" in report
    assert report.comparable is False
    assert report.first_difference() is not None


def test_python_version_difference_blocks_comparison():
    right = _manifest()
    right.runtime_lock.python_env.python_version = "3.13.0"
    report = compare_manifests(_manifest(), right)
    assert "python_env" in report


def test_dsh_slice_difference_blocks_comparison():
    from aeval.agents.dsh.release import OFFICIAL_DSH_TAG

    right = _manifest()
    right.runtime_lock.dsh.official_tag = "dsh-v0.2.0"
    report = compare_manifests(_manifest(), right)
    assert "dsh" in report
    assert OFFICIAL_DSH_TAG  # sanity: the left side stays on the locked tag


def test_different_agent_adapters_block_comparison():
    """Scores from two different agents are never averageable (P0-2)."""
    from aeval.agents.contract import build_adapter_spec
    from aeval.agents.dsh.agent import DshAgent

    dsh = build_adapter_spec(DshAgent)
    other = dsh.model_copy(update={"id": "otheragent", "impl": "somewhere:OtherAgent"})
    assert compare_manifests(_manifest(adapters=[dsh]), _manifest(adapters=[dsh])).comparable is True
    report = compare_manifests(_manifest(adapters=[dsh]), _manifest(adapters=[other]))
    assert report.comparable is False
    assert "adapters" in report
    # a manifest sealed before the adapter contract carries no adapter: it stays
    # comparable with another adapter-less manifest (legacy tolerance)
    assert compare_manifests(_manifest(), _manifest()).comparable is True


def test_a_second_agents_release_blocks_comparison():
    """P1-1: release identity is per agent, not only DSH's."""
    from aeval.contracts import AgentReleaseLock

    right = _manifest()
    left = _manifest()
    assert compare_manifests(left, right).comparable is True
    right.runtime_lock.agents["otheragent"] = AgentReleaseLock(
        id="otheragent", version="1.0.0"
    )
    report = compare_manifests(left, right)
    assert report.comparable is False
    assert "agents" in report
