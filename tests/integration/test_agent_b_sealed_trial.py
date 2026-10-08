"""Acceptance: a non-Node, ATIF-only second agent reaches a sealed run.

The framework's claim is that a second agent costs one adapter, not a core
change. This pins the part of that claim reachable without Docker: an adapter
with no Node, no DSH control stack and no DSH session file passes the contract
gates, its identity lands in the store, its run seals — and that run stays
incomparable with a DSH run.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aeval.agents.contract import (
    adapter_declaration_gap,
    adapter_member_gap,
    build_adapter_spec,
    load_adapter_class,
    required_capability_gaps,
)
from aeval.agents.dsh.agent import DshAgent
from aeval.agents.testing.fake_agent import REFERENCE_AGENT_IMPORT_PATH
from aeval.bundle.attestation import compare_manifests
from aeval.bundle.finalize import finalize_run
from aeval.bundle.manifest import write_intent_manifest
from aeval.contracts import (
    OverlayIdentity,
    RunManifest,
    TrialCoordinates,
    TrialRecord,
    VersionsBundle,
)
from aeval.hooks.plugin import _observed_adapter, _require_adapter_contract  # noqa: PLC2701
from aeval.store.sqlite import TrialStore


def _manifest(runtime_lock, adapters=(), run_id="run-agentb") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        runtime_lock=runtime_lock,
        adapters=list(adapters),
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64, source_commit="9" * 40
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def _fake_run(tmp_path: Path, manifest: RunManifest):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    write_intent_manifest(manifest, run_dir)
    (run_dir / "harbor-job.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "runtime_lock.json").write_text(
        manifest.runtime_lock.model_dump_json(), encoding="utf-8"
    )
    store_path = tmp_path / "store.sqlite3"
    store = TrialStore(store_path)
    store.create_run(manifest)
    store.close()
    return run_dir, store_path


def _summary(run_dir: Path, manifest: RunManifest, trial_id: str) -> None:
    payload = {
        "run_id": manifest.run_id,
        "unobserved_trials": 0,
        "trials": {
            trial_id: {"verdict": "pass", "stop_reason": "agent_exit_0", "phase": "ended"}
        },
    }
    (run_dir / "aeval_run_summary.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_second_agent_satisfies_the_contract_without_any_dsh():
    adapter = load_adapter_class(REFERENCE_AGENT_IMPORT_PATH)
    assert adapter_member_gap(adapter) == []
    assert adapter_declaration_gap(adapter) == []
    _, missing = required_capability_gaps([REFERENCE_AGENT_IMPORT_PATH], ["shell"])
    assert missing == []
    # the run-start gate accepts it exactly as it accepts the shipped adapter
    _require_adapter_contract(
        SimpleNamespace(config=SimpleNamespace(
            agents=[SimpleNamespace(import_path=REFERENCE_AGENT_IMPORT_PATH)]
        ))
    )
    assert build_adapter_spec(adapter).budget_enforcement == "none"


def test_live_agent_identity_is_observed_not_assumed():
    from aeval.agents.testing.fake_agent import FakeAtifAgent

    state = SimpleNamespace(trial_id="t1", evidence_issues=[])
    context = SimpleNamespace(
        environments=SimpleNamespace(agent=lambda trial_id: FakeAtifAgent(observed_version="7.7.7"))
    )
    spec = _observed_adapter(context, state)
    assert (spec.id, spec.version, spec.mode) == ("fakeagent", "7.7.7", "installed_cli")
    assert state.evidence_issues == []
    # no agent available: no identity claimed, no issue invented
    assert _observed_adapter(SimpleNamespace(environments=None), state) is None


def test_second_agent_trial_is_recorded_by_identity_and_the_run_seals(tmp_path, runtime_lock):
    from aeval.agents.testing.fake_agent import FakeAtifAgent

    # as the live path builds it: the observed version, not the declared default
    spec = build_adapter_spec(
        FakeAtifAgent, import_path=REFERENCE_AGENT_IMPORT_PATH, version="0.0.1-fake"
    )
    manifest = _manifest(runtime_lock, adapters=[spec])
    run_dir, store_path = _fake_run(tmp_path, manifest)

    store = TrialStore(store_path)
    try:
        store.persist_trial_with_grades(
            TrialRecord(
                trial_id="trial-b",
                coordinates=TrialCoordinates(
                    run_id=manifest.run_id, suite_id="s", suite_version="1",
                    task_id="hello-world", trial_index=0,
                ),
                stop_reason="agent_exit_0",
                verdict="pass",
                adapter=spec,
            ),
            [],
        )
        # the store round-trips the adapter identity, not just the verdict
        stored = store.load_trial("trial-b")
        assert stored.adapter is not None
        assert stored.adapter.id == "fakeagent"
        assert stored.adapter.version == "0.0.1-fake"
    finally:
        store.close()

    _summary(run_dir, manifest, "trial-b")
    report = finalize_run(run_dir, store_path)
    assert report is not None


def test_second_agent_run_is_incomparable_with_a_dsh_run(runtime_lock):
    fake = build_adapter_spec(
        load_adapter_class(REFERENCE_AGENT_IMPORT_PATH), import_path=REFERENCE_AGENT_IMPORT_PATH
    )
    dsh = build_adapter_spec(DshAgent)
    report = compare_manifests(
        _manifest(runtime_lock, adapters=[dsh], run_id="r-dsh"),
        _manifest(runtime_lock, adapters=[fake], run_id="r-fake"),
    )
    assert report.comparable is False
    assert "adapters" in report
