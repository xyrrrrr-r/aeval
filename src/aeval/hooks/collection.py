"""Production wiring for the P0-6 collectors (the missing real producer).

``collectors.py`` knows how to write each fixed output; this module is
what actually DRIVES them against a live trial, so the declared
``[[verifier.collect]]`` outputs stop being a declaration without a
producer (environment-verification finding F1).

Everything collected here is observed, never synthesised:

- ``runtime_dump``     — facts read back from the running sandbox
  (``uname -m`` through the environment API, backend type, SDK version)
  plus the trial/session identity;
- ``observable:<name>``— each suite-declared observable probed
  out-of-band through ``await env.exec`` (P0-2 semantics);
- ``dsh_session``      — the synced official session record bytes;
- ``canonical_transcript`` — built by the official reader through the
  DSH agent's own ``read_trial_session()``;
- ``mock_call_log``    — the broker's observed call log, or an explicit
  "no calls were made" record (doc §10.2: never a fake ``{}``).

Fail-closed: any required output that cannot be produced raises
``CollectionError``. The caller marks the trial infra_invalid — a trial
missing evidence must not reach grading.
"""

from __future__ import annotations

import importlib.metadata as _md
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aeval.agents.dsh.agent import find_session_record, host_session_root
from aeval.contracts import ArtifactRef, CollectionManifest, CollectOutcome, RuntimeLock
from aeval.hooks.baseline_arrival import (
    _BaselineFailure,
    _exec_return_code,
    probe_observable,
)
from aeval.hooks.collectors import (
    produce_canonical_transcript,
    produce_mock_call_log,
    produce_observable,
    produce_runtime_dump,
    produce_session_record,
    write_collection_manifest,
)

__all__ = [
    "CollectionError",
    "observe_runtime_dump",
    "collect_trial_evidence",
]


class CollectionError(RuntimeError):
    """A required evidence output could not be produced from the trial."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _exec_stdout(environment: Any, command: str) -> str | None:
    """Run a read-only command out-of-band; ``None`` when unobtainable.

    The return code is read through the shared helper: Harbor's
    ``ExecResult`` names the field ``return_code``, and reading only
    ``exit_code`` silently dropped every observation (found on the real
    e2b host — the runtime dump had no ``machine``/``kernel``).
    """
    exec_fn = getattr(environment, "exec", None)
    if not callable(exec_fn):
        return None
    try:
        result = await exec_fn(command)
    except Exception:
        return None
    if _exec_return_code(result) != 0:
        return None
    stdout = getattr(result, "stdout", "")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    return str(stdout).strip()


async def observe_runtime_dump(
    environment: Any,
    *,
    trial_id: str,
    session_id: str,
    backend: str = "e2b",
    template_alias: str | None = None,
) -> dict[str, Any]:
    """Observed runtime facts — read back, never copied from the intent.

    Every field is either measured from the live sandbox or read from
    the running process; nothing is taken from the expected config, so
    a sandbox that is not what the lock pins cannot produce a dump that
    looks like it is.
    """
    dump: dict[str, Any] = {
        "trial_id": trial_id,
        "session_id": session_id,
        "backend": backend,
        "observed_at": _now().isoformat(),
    }
    arch = await _exec_stdout(environment, "uname -m")
    if arch:
        dump["machine"] = arch
    uname = await _exec_stdout(environment, "uname -sr")
    if uname:
        dump["kernel"] = uname
    node = await _exec_stdout(environment, "node --version")
    if node:
        dump["node_version"] = node
    try:
        dump["e2b_sdk_version"] = _md.version("e2b")
    except Exception:
        pass
    if template_alias:
        dump["template_alias"] = template_alias
    try:
        import e2b  # noqa: F401
    except Exception:
        dump["e2b_sdk_present"] = False
    else:
        dump["e2b_sdk_present"] = True
    return dump


async def collect_trial_evidence(
    *,
    trial_dir: Path,
    trial_id: str,
    suite: Any,
    environment: Any,
    agent: Any,
    runtime_lock: RuntimeLock,
    session_id: str,
    broker_calls: list[dict[str, Any]] | None = None,
) -> CollectionManifest:
    """Produce every required artifact for one trial; return the manifest.

    Order matters: the manifest is written last (it is never its own
    artifact), and the whole set is all-or-nothing — a partial
    collection raises instead of leaving a bundle that could pass a
    weaker check.
    """
    trial_dir = Path(trial_dir)
    outcomes: list[CollectOutcome] = []
    artifacts: list[ArtifactRef] = []

    if environment is None:
        raise CollectionError(
            "no live environment handle — the runtime dump and observables "
            "would be unverifiable, so no collection is recorded"
        )

    dump = await observe_runtime_dump(
        environment, trial_id=trial_id, session_id=session_id
    )
    outcome, ref = produce_runtime_dump(trial_dir, dump)
    outcomes.append(outcome)
    artifacts.append(ref)

    for spec in suite.overlay.observables:
        try:
            value = await probe_observable(environment, spec)
        except _BaselineFailure as exc:
            if exc.kind != "absent":
                # nothing could be observed → the trial cannot be judged
                raise CollectionError(
                    f"observable {spec.name!r} could not be probed: {exc}"
                ) from exc
            # The environment answered and the fact is missing: that is a
            # statement about the run (the task was not done), not an
            # infrastructure failure. Record the absence explicitly so
            # the grader scores it instead of blocking the trial.
            value = {"present": False, "reason": str(exc)}
        except Exception as exc:
            # the probe itself blew up (unreachable sandbox, broken API):
            # that is infrastructure, and it blocks the trial
            raise CollectionError(
                f"observable {spec.name!r} could not be probed: {exc}"
            ) from exc
        outcome, ref = produce_observable(trial_dir, spec.name, value)
        outcomes.append(outcome)
        artifacts.append(ref)

    session_bytes = _read_session_record(agent)
    outcome, ref = produce_session_record(trial_dir, session_bytes)
    outcomes.append(outcome)
    artifacts.append(ref)

    try:
        transcript = agent.read_trial_session()
    except Exception as exc:
        raise CollectionError(
            f"canonical transcript could not be built from the official "
            f"session read: {exc}"
        ) from exc
    outcome, ref = produce_canonical_transcript(trial_dir, transcript)
    outcomes.append(outcome)
    artifacts.append(ref)

    # An explicit, truthful record of model/tool calls. A run whose
    # broker observed nothing writes a "no calls" fact rather than an
    # empty object that could be mistaken for a passing check.
    calls = list(broker_calls or [])
    if not calls:
        # Truthful wording: an empty list means THIS collector saw no call
        # records. The in-sandbox collect hook cannot see the host-side
        # broker log, and the previous wording asserted "no model broker was
        # started", which was false on the real chain where the broker had
        # served the very call the run was scored on (real-chain finding).
        calls = [{
            "event": "no_calls_observed",
            "reason": "this collector observed no broker call records for the trial",
            "recorded_at": _now().isoformat(),
        }]
    outcome, ref = produce_mock_call_log(trial_dir, calls)
    outcomes.append(outcome)
    artifacts.append(ref)

    manifest = CollectionManifest(
        trial_id=trial_id,
        outcomes=outcomes,
        artifacts=artifacts,
        runtime_lock_digest=runtime_lock.digest(),
    )
    write_collection_manifest(
        trial_dir,
        trial_id=trial_id,
        outcomes=outcomes,
        artifacts=artifacts,
        runtime_lock=runtime_lock,
    )
    return manifest


def _read_session_record(agent: Any) -> bytes:
    """Bytes of the synced official session record (host-side copy)."""
    paths_fn = getattr(agent, "paths", None)
    # DSH's own conversation session id — never Harbor's ``session_id``
    # attribute, which names the sandbox environment instead.
    # Contract member first (aeval.agents.contract); dsh_session_id kept as a
    # deprecated fallback for adapters written before the contract existed.
    session_id = getattr(agent, "agent_session_id", None)
    if session_id is None:
        session_id = getattr(agent, "dsh_session_id", None)
    if not callable(paths_fn) or not session_id:
        raise CollectionError(
            "the trial's agent exposes no DSH session — the official "
            "session record cannot be collected"
        )
    logs_dir = paths_fn().logs_dir
    source_root = host_session_root(Path(logs_dir))
    try:
        record = find_session_record(source_root, str(session_id))
    except Exception as exc:
        raise CollectionError(f"official session record is ambiguous: {exc}") from exc
    if record is None:
        raise CollectionError(
            f"official session record for {session_id} not found under "
            f"{source_root} — the synced session must exist before evidence "
            "can be collected"
        )
    return record.read_bytes()


def load_broker_calls(path: Path | None) -> list[dict[str, Any]]:
    """Read a broker call log (JSONL); missing file → empty list."""
    if path is None:
        return []
    path = Path(path)
    if not path.is_file():
        return []
    calls: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            calls.append(parsed)
    return calls
