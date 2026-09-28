"""Evidence hard gate (plan §2): best-effort collection becomes a gate.

Harbor treats ``[[verifier.collect]]`` failures as warnings and
collects artifacts best-effort. For score validity that is not enough:
an incomplete evidence set must never reach a verifier. This module
runs at VERIFICATION_START and raises ``EvidenceIntegrityError`` — the
one hook point where raising actually prevents the verifier from
running — when evidence is missing, mismatched or untrustworthy.

Everything else (audit reads, record writes) is record-only: audit
hooks own their I/O errors and never let them break a trial.

P0-6 hardening, all fail-closed:

- fixed logical-name → file-path mapping; a manifest artifact at any
  other path for a required output is a failure, and the loose
  ``"session" in path`` heuristic is gone;
- the collection manifest's own identity is bound by the outer bundle
  attestation (P0-8) — it is NOT a collect output and never hashes
  itself;
- empty outcomes, outcomes that never executed (no exit code and no
  exception), missing observable artifacts and a missing bundle
  descriptor are all fatal;
- containment is checked on resolved paths (``is_relative_to``), never
  by string prefix;
- the manifest must be bound to the run's runtime lock digest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aeval.agents.dsh.agent import find_session_record
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
    "find_bundle_descriptor",
    "EvidenceIntegrityError",
    "FIXED_OUTPUT_PATHS",
    "build_required_collect_plan",
    "output_path_for",
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


# Logical collect output names → the fixed file path inside the trial
# directory that carries them. The mapping is fixed so a manifest
# cannot rename evidence post hoc (P0-6).
FIXED_OUTPUT_PATHS: dict[str, str] = {
    "runtime_dump": "runtime_dump.json",
    "mock_call_log": "mock_call_log.jsonl",
    "dsh_session": "sessions/session.v4.jsonl.zstd",
    "canonical_transcript": "canonical_transcript.json",
}

SESSION_ROOT = "sessions"


def output_path_for(name: str) -> str:
    """Fixed relative path of a logical output inside the trial dir."""
    if name in FIXED_OUTPUT_PATHS:
        return FIXED_OUTPUT_PATHS[name]
    if name.startswith("observable:"):
        return f"observables/{name.split(':', 1)[1]}.json"
    raise EvidenceIntegrityError(f"unknown collect output name: {name!r}")


def build_required_collect_plan(suite: ResolvedSuite) -> list[str]:
    """Names of the collect outputs the evidence bundle must contain.

    The collection manifest itself is NOT in this list: its identity is
    bound by the outer bundle attestation, and it must never be turned
    into a same-named placeholder artifact or a self-referential hash.
    """
    plan = list(FIXED_OUTPUT_PATHS)
    plan.extend(f"observable:{o.name}" for o in suite.overlay.observables)
    return plan


def validate_collect_declarations(
    verifier_collect: list[Any], plan: list[str]
) -> None:
    """The task must declare a collect command for every required output.

    Harbor's collect config is a list of commands (``VerifierCollectConfig``
    with a ``command`` string); the fixed file outputs must be produced
    by the declared commands. This check runs at suite-validation time
    (inside ``compose_harbor_job``), long before any run, so a missing
    declaration never reaches mid-run. Observables are probed via the
    environment API and are not file outputs of collect commands.
    """
    if not verifier_collect:
        raise EvidenceIntegrityError(
            "task declares no [[verifier.collect]] commands but the evidence "
            f"plan requires: {plan}"
        )
    commands = " ".join(
        getattr(c, "command", str(c)) for c in verifier_collect
    )
    for name in FIXED_OUTPUT_PATHS:
        if name not in commands:
            raise EvidenceIntegrityError(
                f"collect plan output {name!r} is not produced by any "
                "declared [[verifier.collect]] command"
            )


def find_bundle_descriptor(trial_dir: Path) -> Path | None:
    """Locate the descriptor Harbor downloaded for this trial.

    The control plugin writes it inside the sandbox's agent logs dir
    (``/logs/agent/bundle_descriptor.json``), and Harbor downloads that
    tree to ``<trial_dir>/agent/`` — so the host-side path carries the
    ``agent/`` prefix. A deployment that copies it to the trial root is
    accepted too, but the downloaded location is checked first: looking
    only at the trial root made every real run fail the evidence gate
    with "bundle descriptor missing" while the file was present
    (found during environment verification).
    """
    trial_dir = Path(trial_dir)
    candidates = [trial_dir / "agent" / "bundle_descriptor.json",
                  trial_dir / "bundle_descriptor.json"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_collection_manifest(trial_dir: Path) -> CollectionManifest:
    """Load and structurally validate the collection manifest.

    The manifest is written by the aeval collect wrapper; a missing or
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
    """Re-hash every sealed artifact; any mismatch is fatal.

    Containment is verified on RESOLVED paths: a symlink or ``..``
    component cannot smuggle an artifact outside the trial directory
    past a string-prefix comparison (P0-6).
    """
    root = bundle_dir.resolve()
    for name, ref in bundle.artifacts.items():
        path = (bundle_dir / ref.path).resolve()
        if not path.is_relative_to(root):
            raise EvidenceIntegrityError(
                f"artifact {name!r} path escapes the bundle: {ref.path}"
            )
        if not path.is_file():
            raise EvidenceIntegrityError(
                f"artifact {name!r} missing at {ref.path}"
            )
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
    - collection manifest present, valid, bound to the run's runtime
      lock digest, and non-empty;
    - every planned output has a SUCCESSFUL collect outcome (executed:
      exit code recorded or exception raised; zero exit; atomic; with
      digest) — an outcome that never ran is not evidence;
    - every planned output exists as an artifact AT ITS FIXED PATH;
    - the DSH session artifact lives under the descriptor's
      session_root (session ownership);
    - artifact hashes and sizes match the manifest;
    - the bundle descriptor (host-side control plugin) is present and
      its session_root stays inside the trial dir.
    """
    manifest = load_collection_manifest(trial_dir)

    if not manifest.runtime_lock_digest:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: collection manifest is not bound "
            "to a runtime lock (empty digest) — evidence is untrustworthy"
        )
    if manifest.runtime_lock_digest != lock.digest():
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: collection manifest runtime lock "
            f"digest {manifest.runtime_lock_digest} differs from the run's "
            f"lock {lock.digest()}"
        )

    issues: list[str] = []
    if not manifest.outcomes:
        issues.append("collection manifest records no outcomes at all")
    for outcome in manifest.outcomes:
        if outcome.exception is not None:
            issues.append(f"collect {outcome.name!r} raised: {outcome.exception}")
        elif outcome.exit_code is None:
            # Neither an exit code nor an exception: the command was
            # never executed — "unknown" is not success.
            issues.append(f"collect {outcome.name!r} never executed (no exit code)")
        elif outcome.exit_code != 0:
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

    outcome_names = {o.name for o in manifest.outcomes}
    missing_outcomes = [n for n in plan if n not in outcome_names]
    if missing_outcomes:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: collect outcomes missing for "
            f"required outputs: {missing_outcomes}"
        )

    artifacts: dict[str, ArtifactRef] = {a.path: a for a in manifest.artifacts}
    artifact_paths = {a.path for a in manifest.artifacts}

    # Fixed-path discipline: every planned output must exist at its
    # fixed path, and the manifest must not carry artifacts at paths
    # that are not part of the plan's fixed mapping.
    misplaced = []
    for name in plan:
        expected = output_path_for(name)
        if expected not in artifact_paths:
            misplaced.append(f"{name} missing at fixed path {expected}")
    allowed_paths = {output_path_for(n) for n in plan}
    extra = sorted(artifact_paths - allowed_paths)
    if extra:
        misplaced.append(f"artifacts outside the fixed mapping: {extra}")
    if misplaced:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: " + "; ".join(misplaced)
        )

    bundle = EvidenceBundle(
        trial_id=manifest.trial_id,
        stop_reason="infra_error",
        artifacts=artifacts,
        collection_manifest=manifest,
    )

    verify_artifact_hashes(bundle, trial_dir)

    # The bundle descriptor is the host-side control plugin's statement
    # of what was observed. Without it there is no session ownership
    # and no stop reason: the evidence is incomplete (P0-6 — this used
    # to be a recorded issue, not a gate).
    descriptor_path = find_bundle_descriptor(trial_dir)
    if descriptor_path is None:
        raise EvidenceIntegrityError(
            f"trial {manifest.trial_id}: bundle descriptor missing "
            "(host-side control plugin) — refusing to pass unverifiable "
            "evidence to the verifier"
        )
    from aeval.contracts import BundleDescriptor

    try:
        descriptor = BundleDescriptor.model_validate(
            json.loads(descriptor_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        raise EvidenceIntegrityError(
            f"bundle descriptor invalid: {exc}"
        ) from exc
    # ``session_root`` is relative to the DESCRIPTOR's own directory
    # (the control plugin wrote it beside the sandbox agent logs, which
    # Harbor downloads to <trial_dir>/agent/), not to the trial root.
    # Resolving it against the trial root made every real run fail with
    # "session_root does not exist" while the tree was right there
    # (found during environment verification).
    descriptor_dir = descriptor_path.parent.resolve()
    session_root = (descriptor_dir / descriptor.session_root).resolve()
    root = descriptor_dir
    if not session_root.is_relative_to(root):
        raise EvidenceIntegrityError(
            f"bundle descriptor session_root escapes the trial dir: "
            f"{descriptor.session_root}"
        )
    # Session ownership: the session artifact must live under the
    # descriptor's session_root, and that root must exist.
    if not session_root.is_dir():
        raise EvidenceIntegrityError(
            f"bundle descriptor session_root does not exist: "
            f"{descriptor.session_root}"
        )
    # Session ownership by CONTENT, not by location: collection writes the
    # session bytes to a fixed logical path (FIXED_OUTPUT_PATHS), so the
    # artifact is a copy — the invariant that matters is that it IS the
    # official record of this descriptor's session, which is a strictly
    # stronger statement than "the file sits under session_root".
    record = find_session_record(session_root, descriptor.session_id)
    if record is None:
        raise EvidenceIntegrityError(
            "the descriptor's session_root "
            f"({descriptor.session_root}) holds no official record for "
            f"session {descriptor.session_id}"
        )
    artifact = bundle.artifacts.get(FIXED_OUTPUT_PATHS["dsh_session"])
    if artifact is None:
        raise EvidenceIntegrityError(
            "dsh_session artifact missing from the evidence bundle"
        )
    expected = hashlib.sha256(record.read_bytes()).hexdigest()
    if artifact.sha256 != expected:
        raise EvidenceIntegrityError(
            "dsh_session artifact is not this trial's official session "
            f"record (artifact {artifact.sha256[:12]}…, official {expected[:12]}…)"
        )
    bundle.bundle_descriptor = descriptor
    bundle.stop_reason = descriptor.stop_reason

    return bundle


async def gate_verification(event: Any, context: Any) -> None:
    """VERIFICATION_START hook: the only hard gate.

    Verifies the sealed evidence for this trial and raises
    EvidenceIntegrityError to prevent the verifier from running when
    evidence is incomplete. On success, records evidence_ok and lets
    Harbor proceed.

    The trial directory comes from the owner state recorded at trial
    start (P0-1, from ``config.trials_dir / config.trial_name``). There
    is deliberately NO fallback directory guessing from hook-event
    fields: a trial without a trusted recorded directory is
    unverifiable and fails closed (P0-6).
    """
    trial_id = str(getattr(event, "trial_id", ""))
    state = context.trial_state(trial_id)

    if state.infra_invalid_reasons:
        raise EvidenceIntegrityError(
            f"trial {trial_id}: baseline/isolation failures recorded: "
            + "; ".join(state.infra_invalid_reasons)
        )

    trial_dir = state.trial_dir
    if trial_dir is None:
        state.evidence_ok = False
        state.evidence_issues.append(
            "no trusted trial directory recorded at trial start"
        )
        state.mark_infra_invalid("evidence: no trusted trial directory")
        raise EvidenceIntegrityError(
            f"trial {trial_id}: no trusted trial directory was recorded at "
            "trial start — refusing to guess the evidence location"
        )

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


async def finalize_trial_record(event: Any, context: Any) -> None:
    """TRIAL_END hook: record-only audit finalization.

    Never raises: an I/O failure here is recorded as an issue; the
    sealed evidence (or its absence) was already gated at
    verification-start. The audit summary is written next to the
    evidence, inside the owner-recorded trial directory.
    """
    trial_id = str(getattr(event, "trial_id", ""))
    state = context.trial_state(trial_id)
    try:
        trial_dir = state.trial_dir
        if trial_dir is None:
            raise EvidenceIntegrityError("no trusted trial directory recorded")
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
            "broker_diagnostics": state.broker_diagnostics,
            "stop_reason": state.stop_reason,
        }
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # audit write failure must not break the trial
        reason = f"audit finalize failed: {exc}"
        state.evidence_issues.append(reason)
        state.mark_infra_invalid(reason)
