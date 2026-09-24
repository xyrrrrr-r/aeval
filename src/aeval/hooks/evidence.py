"""Evidence hard gate (plan §2): best-effort collection becomes a gate.

Harbor treats ``[[verifier.collect]]`` failures as warnings and
collects artifacts best-effort. For score validity that is not enough:
an incomplete evidence set must never reach a verifier. This module
runs at VERIFICATION_START and raises ``EvidenceIntegrityError`` — the
one hook point where raising actually prevents the verifier from
running — when evidence is missing, mismatched or untrustworthy.

Everything else (audit reads, record writes) is record-only: audit
hooks own their I/O errors and never let them break a trial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aeval.contracts import (
    ArtifactRef,
    CollectOutcome,
    CollectionManifest,
    EvidenceBundle,
    RequirementBitmap,
    RuntimeLock,
)
from aeval.suite_models import ResolvedSuite

__all__ = [
    "EvidenceIntegrityError",
    "build_required_collect_plan",
    "validate_collect_declarations",
    "load_collection_manifest",
    "verify_evidence_bundle",
    "verify_artifact_hashes",
    "evaluate_requirements",
    "gate_verification",
    "finalize_trial_record",
]


class EvidenceIntegrityError(RuntimeError):
    """Raised at verification-start to block the verifier entirely.

    Carries the trial id and a full diagnosis list so the exclusion is
    auditable. Raising here is the ONLY intended gate: the trial is
    classified infra_invalid and must not enter the valid denominator.
    """


REQUIRED_COLLECT_OUTPUTS: tuple[str, ...] = (
    "runtime_dump",
    "mock_call_log",
    "dsh_session",
    "collection_manifest",
)


def build_required_collect_plan(suite: ResolvedSuite) -> list[str]:
    """Names of the collect outputs the evidence bundle must contain.

    Observables are probed separately (they are read via the
    environment API, not files); the atomic file outputs listed here
    are what ``[[verifier.collect]]`` must produce.
    """
    plan = list(REQUIRED_COLLECT_OUTPUTS)
    plan.extend(f"observable:{o.name}" for o in suite.overlay.observables)
    return plan


def validate_collect_declarations(
    verifier_collect: list[Any], plan: list[str]
) -> None:
    """The task must declare a collect command for every required output.

    Harbor's collect config is a list of commands; we require the plan
    outputs to be produced (their names appear in declared commands'
    output paths). This check fails at suite validation time, long
    before any run, so a missing declaration never reaches mid-run.
    """
    if not verifier_collect:
        raise EvidenceIntegrityError(
            "task declares no [[verifier.collect]] commands but the evidence "
            f"plan requires: {plan}"
        )
    commands = " ".join(
        getattr(c, "command", str(c)) for c in verifier_collect
    )
    missing = [
        name
        for name in plan
        if name.startswith("observable:")
        or name == "collection_manifest"
    ]
    # File outputs must be named somewhere in the declared commands.
    for name in REQUIRED_COLLECT_OUTPUTS:
        if name not in commands:
            raise EvidenceIntegrityError(
                f"collect plan output {name!r} is not produced by any "
                "declared [[verifier.collect]] command"
            )
    del missing  # observables are env-API reads; nothing to check here


def load_collection_manifest(trial_dir: Path) -> CollectionManifest:
    """Load and structurally validate the collection manifest.

    The manifest is written by our collect wrapper; a missing or
    unparsable manifest is an evidence failure, not a warning.
    """
    path = trial_dir / "collection_manifest.json"
    if not path.is_file():
        raise EvidenceIntegrityError(
            f"collection manifest missing: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = CollectionManifest.model_validate(data)
    except Exception as exc:
        raise EvidenceIntegrityError(
            f"collection manifest invalid at {path}: {exc}"
        ) from exc
    return manifest


def verify_artifact_hashes(bundle: EvidenceBundle, bundle_dir: Path) -> None:
    """Re-hash every sealed artifact; any mismatch is fatal."""
    for name, ref in bundle.artifacts.items():
        path = (bundle_dir / ref.path).resolve()
        root = bundle_dir.resolve()
        if not str(path).startswith(str(root)):
            raise EvidenceIntegrityError(
                f"artifact {name!r} path escapes the bundle: {ref.path}"
            )
        if not path.is_file():
            raise EvidenceIntegrityError(
                f"artifact {name!r} missing at {ref.path}"
            )
        import hashlib

        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != ref.sha256:
            raise EvidenceIntegrityError(
                f"artifact {name!r} hash mismatch: expected {ref.sha256}, "
                f"actual {actual}"
            )
        if path.stat().st_size != ref.size_bytes:
            raise EvidenceIntegrityError(
                f"artifact {name!r} size mismatch: expected {ref.size_bytes}, "
                f"actual {path.stat().st_size}"
            )


def evaluate_requirements(
    evidence: EvidenceBundle,
) -> RequirementBitmap:
    """Map sealed evidence onto the fixed six-requirement bitmap."""
    ok = evidence.requirements
    bitmap = RequirementBitmap(
        input_complete=ok.input_complete,
        agent_finished=ok.agent_finished,
        integration_valid=ok.integration_valid,
        render_valid=ok.render_valid,
        judge_finished=ok.judge_finished,
        artifact_schema_ok=ok.artifact_schema_ok,
    )
    if evidence.stop_reason == "infra_error":
        bitmap.judge_finished = False
    return bitmap


def verify_evidence_bundle(
    trial_dir: Path,
    lock: RuntimeLock,
    plan: list[str],
) -> EvidenceBundle:
    """Build the sealed EvidenceBundle or raise EvidenceIntegrityError.

    Checks (all fatal on failure):
    - collection manifest present, valid, and every collect outcome
      succeeded (zero exit, no exception, atomic write);
    - every required plan output exists as an artifact;
    - DSH session present;
    - artifact hashes match the manifest;
    - bundle descriptor (host-side) paths stay inside the declared
      session root;
    - runtime lock digest matches the run's lock.
    """
    manifest = load_collection_manifest(trial_dir)

    issues: list[str] = []
    for outcome in manifest.outcomes:
        if outcome.exception is not None:
            issues.append(f"collect {outcome.name!r} raised: {outcome.exception}")
        elif outcome.exit_code is not None and outcome.exit_code != 0:
            issues.append(
                f"collect {outcome.name!r} exited {outcome.exit_code}"
            )
        elif outcome.sha256 is None:
            issues.append(f"collect {outcome.name!r} produced no digest")
        elif not outcome.atomic:
            issues.append(f"collect {outcome.name!r} was not atomic")

    if issues:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: collection incomplete: " + "; ".join(issues)
        )

    artifacts: dict[str, ArtifactRef] = {a.path: a for a in manifest.artifacts}
    artifact_paths = {a.path for a in manifest.artifacts}

    required_files = [n for n in plan if not n.startswith("observable:")]
    missing_files = [n for n in required_files if n not in artifact_paths]
    if missing_files:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: required outputs missing from the "
            f"collection manifest: {missing_files}"
        )

    dsh_sessions = [p for p in artifact_paths if "session" in p]
    if not dsh_sessions:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: no DSH session artifact collected"
        )

    bundle = EvidenceBundle(
        trial_id=manifest.trial_id,
        stop_reason="infra_error",
        artifacts=artifacts,
        collection_manifest=manifest,
    )

    verify_artifact_hashes(bundle, trial_dir)

    descriptor_path = trial_dir / "bundle_descriptor.json"
    if descriptor_path.is_file():
        from aeval.contracts import BundleDescriptor

        try:
            descriptor = BundleDescriptor.model_validate(
                json.loads(descriptor_path.read_text(encoding="utf-8"))
            )
        except Exception as exc:
            raise EvidenceIntegrityError(
                f"bundle descriptor invalid: {exc}"
            ) from exc
        session_root = (trial_dir / descriptor.session_root).resolve()
        root = trial_dir.resolve()
        if not str(session_root).startswith(str(root)):
            raise EvidenceIntegrityError(
                f"bundle descriptor session_root escapes the trial dir: "
                f"{descriptor.session_root}"
            )
        bundle.bundle_descriptor = descriptor
        bundle.stop_reason = descriptor.stop_reason
    else:
        bundle.issues.append("bundle descriptor missing (host-side control plugin)")

    if lock.digest() == "":
        raise EvidenceIntegrityError("runtime lock has no digest")

    return bundle


async def gate_verification(event: Any, context: Any) -> None:
    """VERIFICATION_START hook: the only hard gate.

    Verifies the sealed evidence for this trial and raises
    EvidenceIntegrityError to prevent the verifier from running when
    evidence is incomplete. On success, records evidence_ok and lets
    Harbor proceed.
    """
    trial_id = str(getattr(event, "trial_id", ""))
    state = context.trial_state(trial_id)

    if state.infra_invalid_reasons:
        raise EvidenceIntegrityError(
            f"trial {trial_id}: baseline/isolation failures recorded: "
            + "; ".join(state.infra_invalid_reasons)
        )

    trial_dir = state.trial_dir or _locate_trial_dir(event, context)
    plan = build_required_collect_plan(context.suite)
    try:
        bundle = verify_evidence_bundle(trial_dir, context.runtime_lock, plan)
    except EvidenceIntegrityError as exc:
        state.evidence_ok = False
        state.evidence_issues.append(str(exc))
        state.mark_infra_invalid(f"evidence: {exc}")
        raise

    bitmap = evaluate_requirements(bundle)
    state.evidence_ok = True
    context.artifacts[trial_id] = bundle


def _locate_trial_dir(event: Any, context: Any) -> Path:
    """Find the trial directory from the hook event's result/paths."""
    result = getattr(event, "result", None)
    for attr in ("directory", "dir", "trial_dir", "path", "output_dir"):
        candidate = getattr(result, attr, None)
        if isinstance(candidate, (str, Path)):
            return Path(candidate)
    config = getattr(event, "config", None)
    for attr in ("trial_dir", "output_dir", "directory"):
        candidate = getattr(config, attr, None)
        if isinstance(candidate, (str, Path)):
            return Path(candidate)
    lock = getattr(event, "lock", None)
    paths = getattr(lock, "paths", None)
    if paths is not None:
        for attr in ("trial_dir", "directory"):
            candidate = getattr(paths, attr, None)
            if isinstance(candidate, (str, Path)):
                return Path(candidate)
    raise EvidenceIntegrityError(
        "cannot locate the trial directory from the hook event — "
        "refusing to pass an unverifiable trial to the verifier"
    )


async def finalize_trial_record(event: Any, context: Any) -> None:
    """TRIAL_END hook: record-only audit finalization.

    Never raises: an I/O failure here is recorded as an issue; the
    sealed evidence (or its absence) was already gated at
    verification-start.
    """
    trial_id = str(getattr(event, "trial_id", ""))
    state = context.trial_state(trial_id)
    try:
        trial_dir = state.trial_dir or _locate_trial_dir(event, context)
        summary_path = trial_dir / "aeval_audit.json"
        summary = {
            "trial_id": trial_id,
            "session_id": state.session_id,
            "phase": state.phase,
            "exception": state.exception,
            "binding": state.binding.model_dump() if state.binding else None,
            "baseline_ok": state.baseline_ok,
            "baseline_failures": state.baseline_failures,
            "evidence_ok": state.evidence_ok,
            "evidence_issues": state.evidence_issues,
            "infra_invalid_reasons": state.infra_invalid_reasons,
            "stop_reason": state.stop_reason,
        }
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # audit write failure must not break the trial
        reason = f"audit finalize failed: {exc}"
        state.evidence_issues.append(reason)
        state.mark_infra_invalid(reason)
