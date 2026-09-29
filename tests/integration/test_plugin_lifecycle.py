from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harbor.cli.job_plugins import attach_job_plugin, finalize_job_plugins
from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.job.lock import TrialLock
from harbor.models.job.result import JobResult, JobStats
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig
from harbor.models.trial.result import AgentInfo, ExceptionInfo, TrialResult
from harbor.trial.hooks import TrialEvent, TrialHookEvent

from aeval.bundle.manifest import write_intent_manifest
from aeval.contracts import (
    BundleDescriptor, EvidenceBundle, OverlayIdentity, RunManifest, TrialPaths,
    VersionsBundle, control_config_digest, job_config_hash,
)
from aeval.hooks.context import LifecycleError
from aeval.hooks.evidence import EvidenceIntegrityError
from aeval.hooks.plugin import AevalPlugin, HookRegistrationError, create_run_context
from aeval.suite_loader.loader import load_suite


@pytest.fixture
async def owned_job(tmp_path, native_suite_dir, runtime_lock, monkeypatch):
    run_dir = tmp_path / "run"
    config = JobConfig(
        job_name="owned", jobs_dir=run_dir / "harbor", n_attempts=1,
        agents=[AgentConfig(name="nop")],
        tasks=[TaskConfig(path=native_suite_dir / "tasks" / "example")],
    )
    config_bytes = config.model_dump_json(indent=2, exclude_none=True).encode("utf-8")
    suite = load_suite(native_suite_dir)
    manifest = RunManifest(
        run_id="run-owned", runtime_lock=runtime_lock,
        runtime_lock_digest=runtime_lock.digest(),
        config_hash=job_config_hash(config),
        config_file_sha256=sha256(config_bytes).hexdigest(),
        overlay=OverlayIdentity(
            suite_id=suite.id, suite_version=suite.version,
            overlay_digest=suite.suite_yaml_digest, source_commit="a" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0", converter_version="test"),
    )
    write_intent_manifest(manifest, run_dir)
    (run_dir / "harbor-job.json").write_bytes(config_bytes)
    lock_path = run_dir / "runtime_lock.json"
    lock_path.write_text(runtime_lock.model_dump_json(), encoding="utf-8")
    for key, value in {
        "AEVAL_RUN_ID": manifest.run_id, "AEVAL_RUN_DIR": run_dir,
        "AEVAL_SUITE_DIR": native_suite_dir, "AEVAL_STORE_PATH": tmp_path / "store.db",
        "AEVAL_RUNTIME_LOCK": lock_path,
    }.items():
        monkeypatch.setenv(key, str(value))
    job = await Job.create(config)
    try:
        yield job
    finally:
        job._close_logger_handlers()


def event_for(job, name="trial", trial_id=None):
    config = TrialConfig(
        trial_name=name, trials_dir=job.job_dir, job_id=job.id,
        task=TaskConfig(path=job.config.tasks[0].path), agent=AgentConfig(name="nop"),
    )
    result = TrialResult(
        id=trial_id or uuid4(), task_name="example", trial_name=name,
        trial_uri=(job.job_dir / name).as_uri(),
        task_id=LocalTaskId(path=job.config.tasks[0].path), task_checksum="0" * 64,
        config=config, agent_info=AgentInfo(name="nop", version="1"),
    )
    (job.job_dir / name).mkdir(parents=True, exist_ok=True)
    return TrialHookEvent(
        event=TrialEvent.START, task_name="example", config=config,
        result=result, lock=TrialLock.model_construct(),
    )


async def emit(job, event, kind):
    event = event.model_copy(update={"event": kind})
    await job._trial_queue._hooks[kind][-1](event)


def job_result(job, *events, total=None):
    return JobResult(
        id=job.id, started_at=datetime.now(timezone.utc),
        n_total_trials=len(events) if total is None else total, stats=JobStats(),
        trial_results=[e.result for e in events],
    )


def exception(name="RuntimeError"):
    return ExceptionInfo(
        exception_type=name, exception_message="fixture failure",
        exception_traceback="fixture traceback", occurred_at=datetime.now(timezone.utc),
    )


def bind(context, event):
    state = context.trials[str(event.trial_id)]
    paths = TrialPaths(
        sandbox_cwd="/workspace", dsh_home="/logs/agent/dsh-home",
        bundle_path="/logs/agent/bundle_descriptor.json", session_root="dsh-home",
        download_root=(state.trial_dir / "agent").relative_to(context.run_dir).as_posix(),
    )
    config = {
        "run": context.run_binding.model_dump(),
        "trialId": state.trial_id, "sessionId": state.session_id,
        "sessionRoot": paths.session_root, "bundlePath": paths.bundle_path,
        "gatewayUrl": "http://127.0.0.1:9000", "jobTokenFile": "/run/trial-token",
        "provider": "fixture", "model": "model", "refuseAuxiliaryCalls": True,
    }
    config["configDigest"] = control_config_digest(config)
    binding = context.bind_control(state.trial_id, config, paths)
    return config, paths, binding


async def test_real_attach_and_jobresult_keep_same_context(owned_job):
    job = owned_job
    plugin = await attach_job_plugin(job, "aeval.hooks:AevalPlugin")
    context = plugin._context
    event = event_for(job)
    await emit(job, event, TrialEvent.START)
    await emit(job, event, TrialEvent.END)
    result = job_result(job, event)
    assert not hasattr(result, "_aeval_contexts")
    assert not hasattr(job, "_aeval_contexts")
    await finalize_job_plugins([plugin], result)
    summary = json.loads((context.run_dir / "aeval_run_summary.json").read_text())
    assert summary["job_id"] == str(job.id)
    assert summary["run_binding"] == context.run_binding.model_dump()
    assert summary["trials"][str(event.trial_id)]["phase"] == "ended"
    assert context.closed
    assert plugin._context is context


@pytest.mark.parametrize("kind", ["RuntimeError", "CancelledError"])
async def test_failure_and_cancellation_survive_late_success(owned_job, kind, monkeypatch):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    await emit(owned_job, event, TrialEvent.START)
    event.result.exception_info = exception(kind)
    terminal = TrialEvent.CANCEL if kind == "CancelledError" else TrialEvent.END
    await emit(owned_job, event, terminal)
    audit = owned_job.job_dir / event.trial_name / "aeval_audit.json"
    original = audit.read_bytes()
    event.result.exception_info = None
    await emit(owned_job, event, TrialEvent.END)
    await emit(owned_job, event, TrialEvent.END)
    assert audit.read_bytes() == original
    state = plugin._context.trials[str(event.trial_id)]
    assert state.phase == ("cancelled" if kind == "CancelledError" else "failed")
    assert state.exception["exception_type"] == kind
    assert state.stop_reason not in ("agent_exit_0", "agent_claimed_done")
    await plugin.on_job_end(job_result(owned_job, event))
    summary_path = plugin._context.run_dir / "aeval_run_summary.json"
    original_summary = summary_path.read_bytes()
    await plugin.on_job_end(job_result(owned_job, event))
    assert summary_path.read_bytes() == original_summary
    with pytest.raises(LifecycleError, match="closed"):
        await emit(owned_job, event, TrialEvent.END)


async def test_duplicate_end_does_not_repeat_audit(owned_job, monkeypatch):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    writes = []

    async def record(event, context):
        writes.append(str(event.trial_id))

    monkeypatch.setattr("aeval.hooks.plugin.finalize_trial_record", record)
    await emit(owned_job, event, TrialEvent.START)
    await emit(owned_job, event, TrialEvent.START)
    await emit(owned_job, event, TrialEvent.END)
    await emit(owned_job, event, TrialEvent.END)
    assert writes == [str(event.trial_id)]
    with pytest.raises(LifecycleError, match="restarted"):
        await emit(owned_job, event, TrialEvent.START)


async def test_unobserved_end_and_late_environment_do_not_forge_success(owned_job):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    with pytest.raises(LifecycleError, match="before start"):
        await emit(owned_job, event, TrialEvent.END)
    await emit(owned_job, event, TrialEvent.START)
    await plugin.on_job_end(job_result(owned_job, event, total=2))
    state = plugin._context.trials[str(event.trial_id)]
    assert state.phase == "failed"
    assert state.stop_reason == "infra_error"
    summary = json.loads((plugin._context.run_dir / "aeval_run_summary.json").read_text())
    assert summary["unobserved_trials"] == 1
    with pytest.raises(LifecycleError, match="closed"):
        await emit(owned_job, event, TrialEvent.ENVIRONMENT_START)


async def test_wrong_job_or_changed_trial_identity_cannot_mutate_owner(owned_job):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    context = plugin._context
    with pytest.raises(LifecycleError, match="already owns"):
        await plugin.on_job_start(owned_job)
    wrong_result = job_result(owned_job).model_copy(update={"id": uuid4()})
    with pytest.raises(LifecycleError, match="JobResult"):
        await plugin.on_job_end(wrong_result)
    event = event_for(owned_job)
    event.config.job_id = uuid4()
    with pytest.raises(LifecycleError, match="another job"):
        await emit(owned_job, event, TrialEvent.START)
    assert not context.trials
    event.config.job_id = owned_job.id
    await emit(owned_job, event, TrialEvent.START)
    event.config.agent.name = "oracle"
    with pytest.raises(LifecycleError, match="changed"):
        await emit(owned_job, event, TrialEvent.END)
    assert context.trials[str(event.trial_id)].phase == "running"


async def test_two_trials_keep_distinct_sessions_paths_and_terminal_state(owned_job):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    a, b = event_for(owned_job, "a"), event_for(owned_job, "b")
    await emit(owned_job, a, TrialEvent.START)
    await emit(owned_job, b, TrialEvent.START)
    context = plugin._context
    sa, sb = (context.trials[str(e.trial_id)] for e in (a, b))
    assert sa.session_id != sb.session_id
    a.result.exception_info = exception("CancelledError")
    await emit(owned_job, a, TrialEvent.CANCEL)
    assert sb.phase == "running"
    await emit(owned_job, b, TrialEvent.END)
    assert sa.phase == "cancelled"
    assert sb.phase == "ended"
    conflicting = event_for(owned_job, "b")
    with pytest.raises(LifecycleError, match="already owned"):
        await emit(owned_job, conflicting, TrialEvent.START)


async def test_full_config_comparison_does_not_use_harbor_identity_blind_equality(owned_job):
    await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    event.result.config = event.config.model_copy(update={"job_id": uuid4()})
    assert event.result.config == event.config
    with pytest.raises(LifecycleError, match="identities differ"):
        await emit(owned_job, event, TrialEvent.START)


async def test_owner_binding_checks_digest_identity_and_download_paths(owned_job):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    await emit(owned_job, event, TrialEvent.START)
    context = plugin._context
    config, paths, binding = bind(context, event)
    assert context.bind_control(binding.trial_id, config, paths) == binding
    for key, value in [("trialId", "wrong"), ("sessionId", "wrong"), ("configDigest", "e" * 64)]:
        with pytest.raises(LifecycleError):
            context.bind_control(binding.trial_id, {**config, key: value}, paths)
    wrong_run = {**config["run"], "runtime_lock_digest": "b" * 64}
    with pytest.raises(LifecycleError, match="run binding"):
        context.bind_control(binding.trial_id, {**config, "run": wrong_run}, paths)
    with pytest.raises(LifecycleError, match="download root"):
        context.bind_control(binding.trial_id, config, paths.model_copy(update={"download_root": "other/agent"}))
    changed = {**config, "model": "other"}
    changed["configDigest"] = control_config_digest(changed)
    with pytest.raises(LifecycleError, match="cannot be replaced"):
        context.bind_control(binding.trial_id, changed, paths)
    await emit(owned_job, event, TrialEvent.CANCEL)
    with pytest.raises(LifecycleError, match="active trial"):
        context.bind_control(binding.trial_id, config, paths)


async def test_verifier_rejects_unbound_or_cross_trial_evidence(owned_job, monkeypatch):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    first = event_for(owned_job, "unbound")
    await emit(owned_job, first, TrialEvent.START)
    with pytest.raises(EvidenceIntegrityError, match="no trusted control binding"):
        await emit(owned_job, first, TrialEvent.VERIFICATION_START)
    second = event_for(owned_job, "bound")
    await emit(owned_job, second, TrialEvent.START)
    context = plugin._context
    _, _, binding = bind(context, second)
    descriptor = BundleDescriptor(
        run=binding.run, trial_id=binding.trial_id, session_id=binding.session_id,
        config_digest=binding.config_digest, session_root=binding.paths.session_root,
        stop_reason="agent_exit_0",
    )

    async def collect(event, ctx):
        ctx.artifacts[str(event.trial_id)] = EvidenceBundle(
            trial_id=binding.trial_id, stop_reason="agent_exit_0", bundle_descriptor=descriptor,
        )

    monkeypatch.setattr("aeval.hooks.plugin.gate_verification", collect)

    async def noop_collection(**_kwargs):
        return None

    # this test is about descriptor identity, not evidence collection
    monkeypatch.setattr("aeval.hooks.plugin.collect_trial_evidence", noop_collection)
    await emit(owned_job, second, TrialEvent.VERIFICATION_START)
    descriptor = descriptor.model_copy(update={"session_id": "wrong-session"})
    with pytest.raises(EvidenceIntegrityError, match="session_id"):
        await emit(owned_job, second, TrialEvent.VERIFICATION_START)
    assert context.trials[binding.trial_id].stop_reason == "infra_error"


@pytest.mark.parametrize("field", ["config_hash", "config_file_sha256", "runtime_lock_digest", "run_id"])
async def test_start_rejects_each_intent_binding_mismatch(owned_job, field):
    path = owned_job.job_dir.parent.parent / "run_manifest.json"
    data = json.loads(path.read_text())
    data[field] = "wrong-run" if field == "run_id" else "f" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HookRegistrationError):
        create_run_context(owned_job)


async def test_config_bytes_and_effective_config_are_distinct_checks(owned_job):
    path = owned_job.job_dir.parent.parent / "harbor-job.json"
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(HookRegistrationError, match="file digest"):
        create_run_context(owned_job)
    path.write_bytes(original)
    owned_job.config.n_attempts += 1
    with pytest.raises(HookRegistrationError, match="Job config hash"):
        create_run_context(owned_job)


async def test_job_end_io_failure_is_reported_by_harbor_not_swallowed(owned_job, monkeypatch, caplog):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")

    def fail(*args):
        raise OSError("summary disk failure")

    monkeypatch.setattr("aeval.hooks.plugin._atomic_write_json", fail)
    await finalize_job_plugins([plugin], job_result(owned_job))
    assert "summary disk failure" in caplog.text
    assert not plugin._summary_written
    assert plugin._context.closed


async def test_jobresult_validates_all_trials_before_reconciling_any(owned_job):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    a, b = event_for(owned_job, "a"), event_for(owned_job, "b")
    await emit(owned_job, a, TrialEvent.START)
    await emit(owned_job, b, TrialEvent.START)
    a.result.exception_info = exception()
    b.result.trial_name = "forged-name"
    with pytest.raises(LifecycleError, match="identity differs"):
        await plugin.on_job_end(job_result(owned_job, a, b))
    assert all(s.phase == "running" for s in plugin._context.trials.values())
    assert not plugin._context.closed


async def test_job_output_path_is_bound_despite_exclusion_from_evaluation_hash(owned_job):
    before = job_config_hash(owned_job.config)
    owned_job.config.job_name = "other-job"
    assert job_config_hash(owned_job.config) == before
    with pytest.raises(HookRegistrationError, match="output directory"):
        create_run_context(owned_job)


async def test_trial_output_path_cannot_leave_the_owned_job(owned_job, tmp_path):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    event.config.trials_dir = tmp_path / "outside"
    with pytest.raises(LifecycleError, match="job-owned directory"):
        await emit(owned_job, event, TrialEvent.START)
    assert not plugin._context.trials


async def test_audit_write_failure_is_retained_as_invalid(owned_job, monkeypatch):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    await emit(owned_job, event, TrialEvent.START)

    def fail(*args, **kwargs):
        raise OSError("audit disk failure")

    monkeypatch.setattr(Path, "write_text", fail)
    await emit(owned_job, event, TrialEvent.END)
    state = plugin._context.trials[str(event.trial_id)]
    assert state.stop_reason == "infra_error"
    assert any("audit disk failure" in reason for reason in state.infra_invalid_reasons)


async def test_job_end_exception_can_downgrade_a_nominal_end(owned_job):
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job)
    await emit(owned_job, event, TrialEvent.START)
    await emit(owned_job, event, TrialEvent.END)
    event.result.exception_info = exception()
    await plugin.on_job_end(job_result(owned_job, event))
    state = plugin._context.trials[str(event.trial_id)]
    assert state.phase == "failed"
    assert state.exception["exception_message"] == "fixture failure"


async def test_agent_start_on_tainted_trial_is_recorded(owned_job, monkeypatch):
    """P0-2: the model phase starting despite infra failures must be
    visible in the trial record — the agent ran on a tainted baseline."""
    from aeval.hooks.context import TrialState  # noqa: F401

    plugin = AevalPlugin()
    await plugin.on_job_start(owned_job)
    context = plugin._context
    start = event_for(owned_job)
    await emit(owned_job, start, TrialEvent.START)
    state = context.trials[str(start.trial_id)]
    state.mark_infra_invalid("baseline_arrival: seed mismatch")

    agent_start = start.model_copy(update={"event": TrialEvent.AGENT_START})
    hook = owned_job._trial_queue._hooks[TrialEvent.AGENT_START][-1]
    await hook(agent_start)

    assert "agent started despite recorded infra failures" in state.evidence_issues
    assert "agent started despite recorded infra failures" in state.infra_invalid_reasons


async def test_agent_start_with_a_started_environment_passes_the_audit(owned_job):
    """P0-4 seam: the owner resolves the live environment at AGENT_START,
    so a healthy sandbox passes the baseline/policy audit instead of being
    blocked for a missing handle."""

    class Env:
        def __init__(self):
            self.approval_policy = "allow"
            self.network_policy = type(
                "P", (), {"network_mode": "no-network", "allowed_hosts": []}
            )()

        async def exec(self, command):
            return type("R", (), {"exit_code": 0, "stdout": "true", "stderr": ""})()

    plugin = AevalPlugin()
    await plugin.on_job_start(owned_job)
    context = plugin._context
    # the suite's baseline must probe a suite-declared observable
    context.suite.overlay.baselines = [
        type(context.suite.overlay.baselines[0])(
            id="ready", probe="observable:ready", equals="true",
        )
    ]
    context.suite.overlay.observables = [
        type(context.suite.overlay.observables[0])(
            name="ready", type="string", source="file:/workspace/ready",
        )
    ]
    start = event_for(owned_job)
    await emit(owned_job, start, TrialEvent.START)
    state = context.trials[str(start.trial_id)]
    # the started handle the owner would have captured from the live trial
    state.trial_id  # noqa: B018 - explicit: registry is keyed by this id
    context.environments.capture(_FakeTrial(state.trial_id, Env()))

    hook = owned_job._trial_queue._hooks[TrialEvent.AGENT_START][-1]
    await hook(start.model_copy(update={"event": TrialEvent.AGENT_START}))

    assert state.baseline_ok, state.baseline_failures
    assert not state.infra_invalid_reasons, state.infra_invalid_reasons


class _FakeTrial:
    def __init__(self, trial_id, environment, agent=None):
        self.id = trial_id
        self.agent_environment = environment
        self.agent = agent




async def test_verification_collects_evidence_from_the_live_trial(owned_job, monkeypatch):
    """P0-6 producer wiring: a bound trial collects real artifacts through
    the owner's live handle, and the manifest reaches the gate."""
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job, "collecting")
    await emit(owned_job, event, TrialEvent.START)
    context = plugin._context
    state = context.trials[str(event.trial_id)]
    _, _, binding = bind(context, event)

    seen: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr("aeval.hooks.plugin.collect_trial_evidence", fake_collect)

    async def fake_gate(_event, ctx):
        ctx.artifacts[str(state.trial_id)] = EvidenceBundle(
            trial_id=state.trial_id, stop_reason="agent_exit_0",
            bundle_descriptor=BundleDescriptor(
                run=binding.run, trial_id=binding.trial_id,
                session_id=binding.session_id, config_digest=binding.config_digest,
                session_root=binding.paths.session_root, stop_reason="agent_exit_0",
            ),
        )

    monkeypatch.setattr("aeval.hooks.plugin.gate_verification", fake_gate)
    await emit(owned_job, event, TrialEvent.VERIFICATION_START)
    assert seen["trial_id"] == state.trial_id
    assert seen["trial_dir"] == state.trial_dir
    assert seen["session_id"] == binding.session_id
    assert seen["suite"] is context.suite


async def test_verification_marks_infra_invalid_when_collection_fails(
    owned_job, monkeypatch
):
    """A trial whose evidence cannot be produced must not reach grading."""
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job, "uncollectable")
    await emit(owned_job, event, TrialEvent.START)
    context = plugin._context
    state = context.trials[str(event.trial_id)]
    bind(context, event)

    async def boom(**_kwargs):
        from aeval.hooks.collection import CollectionError

        raise CollectionError("official session record missing")

    monkeypatch.setattr("aeval.hooks.plugin.collect_trial_evidence", boom)
    with pytest.raises(EvidenceIntegrityError, match="official session record missing"):
        await emit(owned_job, event, TrialEvent.VERIFICATION_START)
    assert state.evidence_ok is False
    assert any("evidence collection failed" in r for r in state.infra_invalid_reasons)


async def _agent_start_with_neutral_audit(owned_job, monkeypatch, name):
    """Attach the plugin and emit START+AGENT_START with the audit stubbed.

    The audit itself is covered elsewhere; these tests target the owner's
    control-bootstrap wiring at AGENT_START.
    """
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    context = plugin._context

    async def neutral_audit(_event, _ctx, _handle=None):
        return None

    monkeypatch.setattr("aeval.hooks.plugin.on_environment_started", neutral_audit)
    event = event_for(owned_job, name)
    await emit(owned_job, event, TrialEvent.START)
    return plugin, context, event


async def test_agent_start_bootstraps_the_control_binding(owned_job, monkeypatch):
    """P0-4: at AGENT_START the owner deploys the token and binds control."""
    plugin, context, event = await _agent_start_with_neutral_audit(
        owned_job, monkeypatch, "bootstrapping"
    )
    state = context.trials[str(event.trial_id)]
    context.broker_spec = SimpleNamespace(identity={"provider": "offline", "model": "m"})
    state.broker = SimpleNamespace(url="http://127.0.0.1:9", token_path=Path("/tmp/tok"))
    seen: dict[str, Any] = {}

    async def fake_bootstrap(**kwargs):
        seen.update(kwargs)
        binding = SimpleNamespace(config_digest="a" * 64, trial_id=state.trial_id)
        return binding, {"digest": "a" * 64}

    monkeypatch.setattr("aeval.hooks.plugin.bootstrap_trial_control", fake_bootstrap)
    await emit(
        owned_job, event.model_copy(update={"event": TrialEvent.AGENT_START}),
        TrialEvent.AGENT_START,
    )
    assert seen["trial_id"] == state.trial_id
    assert seen["provider"] == "offline" and seen["model"] == "m"
    assert seen["broker"] is state.broker
    assert state.binding is not None and state.control_config == {"digest": "a" * 64}
    assert not any("control bootstrap failed" in r for r in state.infra_invalid_reasons)


async def test_agent_start_derives_control_paths_from_the_adapter(owned_job, monkeypatch):
    """The paths handed to the owner come from the ADAPTER, not the DSH defaults.

    hooks/broker_lifecycle.py composes the authoritative control config from
    ``trial_control_paths(..., agent=...)``, and the owner refuses a binding
    whose paths differ. Omitting the agent here fell back to the historical DSH
    defaults, so an adapter declaring different ones had its binding refused on
    a real sandbox (example-lab: deepagent -> /root/.deepagents, deepagent-home).
    """
    plugin, context, event = await _agent_start_with_neutral_audit(
        owned_job, monkeypatch, "declared-paths"
    )
    state = context.trials[str(event.trial_id)]
    context.broker_spec = SimpleNamespace(identity={"provider": "offline", "model": "m"})
    state.broker = SimpleNamespace(url="http://127.0.0.1:9", token_path=Path("/tmp/tok"))
    agent_handle = SimpleNamespace(
        SANDBOX_HOME="/root/.deepagents", SESSION_ARTIFACT_DIR="deepagent-home"
    )
    context.environments.capture(
        _FakeTrial(state.trial_id, SimpleNamespace(), agent=agent_handle)
    )
    seen: dict[str, Any] = {}

    async def fake_bootstrap(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(config_digest="a" * 64, trial_id=state.trial_id), {}

    monkeypatch.setattr("aeval.hooks.plugin.bootstrap_trial_control", fake_bootstrap)
    await emit(
        owned_job, event.model_copy(update={"event": TrialEvent.AGENT_START}),
        TrialEvent.AGENT_START,
    )

    assert seen["agent"] is agent_handle
    assert seen["paths"].dsh_home == "/root/.deepagents"
    assert seen["paths"].session_root == "deepagent-home"


async def test_tainted_trial_never_receives_a_control_binding(owned_job, monkeypatch):
    """The owner's hard block: a trial that failed its environment audit
    must not get a model token or a control binding."""
    plugin, context, event = await _agent_start_with_neutral_audit(
        owned_job, monkeypatch, "tainted"
    )
    state = context.trials[str(event.trial_id)]
    context.broker_spec = SimpleNamespace(identity={"provider": "offline", "model": "m"})
    state.broker = SimpleNamespace(url="http://127.0.0.1:9", token_path=Path("/tmp/tok"))
    state.mark_infra_invalid("baseline_arrival: seed mismatch")
    called = False

    async def fake_bootstrap(**_kwargs):
        nonlocal called
        called = True
        return None, None

    monkeypatch.setattr("aeval.hooks.plugin.bootstrap_trial_control", fake_bootstrap)
    await emit(
        owned_job, event.model_copy(update={"event": TrialEvent.AGENT_START}),
        TrialEvent.AGENT_START,
    )
    assert called is False, "a tainted trial must not be bootstrapped"
    assert state.binding is None


async def test_bootstrap_failure_taints_the_trial(owned_job, monkeypatch):
    """A binding that cannot be created keeps the trial out of grading."""
    plugin, context, event = await _agent_start_with_neutral_audit(
        owned_job, monkeypatch, "bootstrap-fails"
    )
    state = context.trials[str(event.trial_id)]
    context.broker_spec = SimpleNamespace(identity={"provider": "offline", "model": "m"})
    state.broker = SimpleNamespace(url="http://127.0.0.1:9", token_path=Path("/tmp/tok"))

    async def failing_bootstrap(**_kwargs):
        from aeval.control.bootstrap import BootstrapError

        raise BootstrapError("environment exposes no upload_file")

    monkeypatch.setattr("aeval.hooks.plugin.bootstrap_trial_control", failing_bootstrap)
    await emit(
        owned_job, event.model_copy(update={"event": TrialEvent.AGENT_START}),
        TrialEvent.AGENT_START,
    )
    assert state.binding is None
    assert any(
        "control bootstrap failed" in r for r in state.infra_invalid_reasons
    ), state.infra_invalid_reasons


async def test_trial_end_grades_and_persists_when_evidence_is_verified(
    owned_job, monkeypatch
):
    """D34 (real-chain finding): the plugin must run the grading pipeline
    at trial end, or no trial reaches the store and the run can never
    seal."""
    from aeval.contracts import BundleDescriptor, EvidenceBundle

    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job, "graded")
    await emit(owned_job, event, TrialEvent.START)
    context = plugin._context
    state = context.trials[str(event.trial_id)]
    _, _, binding = bind(context, event)
    descriptor = BundleDescriptor(
        run=binding.run, trial_id=binding.trial_id, session_id=binding.session_id,
        config_digest=binding.config_digest, session_root=binding.paths.session_root,
        stop_reason="agent_exit_0",
    )
    context.artifacts[state.trial_id] = EvidenceBundle(
        trial_id=state.trial_id, stop_reason="agent_exit_0",
        bundle_descriptor=descriptor,
    )

    seen: dict[str, Any] = {}

    async def fake_grade(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr("aeval.verdict.pipeline.grade_and_record", fake_grade)
    await emit(owned_job, event, TrialEvent.END)

    assert seen, "the grading pipeline ran"
    assert seen["trial_id"] == state.trial_id
    assert seen["coordinates"].run_id == context.run_id
    assert seen["coordinates"].task_id == "example"
    assert seen["stop_reason"] == "agent_exit_0"
    assert seen["evidence"].bundle_descriptor is descriptor
    snapshot = seen["progress"].snapshot()
    assert snapshot.input_complete and snapshot.integration_valid
    assert snapshot.artifact_schema_ok
    assert snapshot.agent_finished, "the trial ended cleanly"
    assert snapshot.judge_finished is False, "only the pipeline sets judging"


async def test_trial_without_verified_evidence_is_not_recorded(
    owned_job, monkeypatch
):
    """A trial whose evidence never passed the gate must stay unrecorded:
    a fabricated record would let an incomplete run seal."""
    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job, "unverified")
    await emit(owned_job, event, TrialEvent.START)
    called = False

    async def fake_grade(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("aeval.verdict.pipeline.grade_and_record", fake_grade)
    await emit(owned_job, event, TrialEvent.END)
    assert called is False


async def test_grading_pipeline_failure_taints_the_trial(owned_job, monkeypatch):
    from aeval.contracts import BundleDescriptor, EvidenceBundle
    from aeval.verdict.pipeline import GradingPipelineError

    plugin = await attach_job_plugin(owned_job, "aeval.hooks:AevalPlugin")
    event = event_for(owned_job, "grading-fails")
    await emit(owned_job, event, TrialEvent.START)
    context = plugin._context
    state = context.trials[str(event.trial_id)]
    _, _, binding = bind(context, event)
    context.artifacts[state.trial_id] = EvidenceBundle(
        trial_id=state.trial_id, stop_reason="agent_exit_0",
        bundle_descriptor=BundleDescriptor(
            run=binding.run, trial_id=binding.trial_id, session_id=binding.session_id,
            config_digest=binding.config_digest,
            session_root=binding.paths.session_root, stop_reason="agent_exit_0",
        ),
    )

    async def failing_grade(**_kwargs):
        raise GradingPipelineError("grader loading failed")

    monkeypatch.setattr("aeval.verdict.pipeline.grade_and_record", failing_grade)
    await emit(owned_job, event, TrialEvent.END)
    assert any("grading failed" in r for r in state.infra_invalid_reasons)
