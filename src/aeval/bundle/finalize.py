"""Run finalization (P0-8): the gate between "Harbor exited" and "done".

``harbor run`` returning 0 only means the process exited; it is not a
verdict on the evaluation. Finalization proves, in order:

1. the plugin wrote a run summary and observed every expected trial;
2. every observed trial reached a terminal phase;
3. every trial has a store record with a final classification
   (a ``None`` verdict is an unclassified trial, not a pass);
4. the on-disk intent manifest still matches the intent recorded in the
   store at run creation (rewrite detection), then the manifest is
   sealed with the store's exclusion summary;
5. the full bundle is attested and survives strict recompute.

Any failure raises :class:`FinalizeError` — the run stays unsealed and
the CLI must not report completion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from aeval.bundle.attestation import create_test_result_attestation, recompute_bundle
from aeval.bundle.manifest import ManifestTamperError, seal_run_manifest
from aeval.contracts import ExclusionSummary
from aeval.metrics.reliability import exclusion_summary
from aeval.store.sqlite import TrialStore

__all__ = ["FinalizeError", "FinalizeReport", "finalize_run"]


class FinalizeError(RuntimeError):
    """The run cannot be finalized — it is not complete or not trustworthy."""


_TERMINAL_PHASES = ("ended", "failed", "cancelled")


@dataclass
class FinalizeReport:
    run_id: str
    expected_trials: int = 0
    recorded_trials: int = 0
    sealed: bool = False
    seal_digest: str | None = None
    attestation_digest: str | None = None
    attested_files: int = 0
    exclusions: ExclusionSummary | None = None
    problems: list[str] = field(default_factory=list)


def _load_summary(run_dir: Path) -> dict:
    summary_path = run_dir / "aeval_run_summary.json"
    if not summary_path.is_file():
        raise FinalizeError(
            "plugin run summary missing: the AevalPlugin did not report a "
            "job summary — the run cannot be verified as complete"
        )
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"plugin run summary is unreadable: {exc}") from exc


def finalize_run(run_dir: Path, store_path: Path) -> FinalizeReport:
    """Verify and seal one finished run; raise on any incompleteness."""
    run_dir = Path(run_dir)
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise FinalizeError(f"no intent manifest in {run_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizeError(f"intent manifest is unreadable: {exc}") from exc
    if manifest.get("sealed"):
        raise FinalizeError("run manifest is already sealed — runs are never re-finalized")
    run_id = manifest.get("run_id")
    if not run_id:
        raise FinalizeError("intent manifest carries no run_id")

    summary = _load_summary(run_dir)
    if summary.get("run_id") != run_id:
        raise FinalizeError(
            f"summary run id {summary.get('run_id')!r} differs from the "
            f"intent manifest run id {run_id!r}"
        )

    problems: list[str] = []
    unobserved = summary.get("unobserved_trials")
    if unobserved is None:
        problems.append("summary does not report unobserved_trials")
    elif unobserved != 0:
        problems.append(
            f"{unobserved} trial(s) expected by the job were never observed by the plugin"
        )

    trials = summary.get("trials")
    if not isinstance(trials, dict):
        raise FinalizeError("summary carries no trial table")
    for trial_id, info in trials.items():
        phase = (info or {}).get("phase")
        if phase not in _TERMINAL_PHASES:
            problems.append(f"trial {trial_id!r} is not terminal (phase={phase!r})")

    store = TrialStore(Path(store_path))
    try:
        try:
            recorded_manifest = store.load_run_manifest(run_id)
        except KeyError as exc:
            raise FinalizeError(
                f"run {run_id!r} was never recorded in the store — the "
                "trusted intent copy is missing"
            ) from exc
        if recorded_manifest.run_id != run_id:
            raise FinalizeError("store run identity mismatch")

        recorded = store.list_trial_ids(run_id)
        missing = sorted(set(trials) - set(recorded))
        if missing:
            problems.append(
                f"trial(s) without a store record: {missing}"
            )
        for trial_id in sorted(set(trials) & set(recorded)):
            try:
                record = store.load_trial(trial_id)
            except KeyError:
                problems.append(f"trial {trial_id!r} vanished from the store")
                continue
            if record.verdict is None:
                problems.append(
                    f"trial {trial_id!r} has no final classification (verdict=None)"
                )
        extra = sorted(set(recorded) - set(trials))
        if extra:
            problems.append(f"store holds unexpected trial(s) for this run: {extra}")

        if problems:
            raise FinalizeError(
                "run incomplete — refusing to seal: " + "; ".join(problems)
            )

        exclusions = exclusion_summary(store.list_trials([run_id]))
        intent_digest = store.run_intent_digest(run_id)
        try:
            _, seal_digest = seal_run_manifest(
                manifest_path, exclusions, expected_intent_digest=intent_digest
            )
        except ManifestTamperError as exc:
            raise FinalizeError(f"seal refused: {exc}") from exc
    finally:
        store.close()

    attestation_digest = create_test_result_attestation(run_dir)
    recompute = recompute_bundle(run_dir)
    return FinalizeReport(
        run_id=run_id,
        expected_trials=len(trials) + (unobserved or 0),
        recorded_trials=len(recorded),
        sealed=True,
        seal_digest=seal_digest,
        attestation_digest=attestation_digest,
        attested_files=int(recompute.get("attested_files", 0)),
        exclusions=exclusions,
        problems=problems,
    )
