"""Baseline arrival tests: a non-baseline copy must never score.

Probes go through the REAL environment API (``await env.exec``),
missing handles/policies are failures (never silent skips), a broker
``allowlist`` egress is legitimate, and observed identities bind to
the expected lock fail-closed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aeval.contracts import ImageIdentity, ObservedIdentity
from aeval.hooks.baseline_arrival import (
    assert_baseline_arrival,
    assert_clock_effective,
    assert_egress_effective,
    assert_isolation_policy,
    on_environment_started,
    probe_observable,
)
from aeval.hooks.context import EvaluationContext
from aeval.provenance import (
    BackendNotAvailableError,
    LockMismatchError,
    bind_observed_identity,
    verify_e2b_backend,
)
from aeval.suite_models import BaselineAssertion, ClockSpec, ObservableSpec


class ExecResult(SimpleNamespace):
    pass


class FakeEnv:
    """Stands in for a Harbor environment handle: the REAL API surface.

    exec() is out-of-band (L3 isolation); network_policy is the live
    runtime policy property; approval_policy is the DSH-side policy the
    owner injects.
    """

    def __init__(self, *, files=None, network_mode="no-network",
                 allowed_hosts=None, approval_policy="allow",
                 has_network_policy=True, has_approval_policy=True,
                 capabilities="e2b"):
        self._files = files or {}
        self.approval_policy = approval_policy if has_approval_policy else None
        if capabilities == "e2b":
            self.capabilities = SimpleNamespace(
                disable_internet=True, network_allowlist=True,
                dynamic_network_policy=True,
            )
        elif capabilities == "none":
            self.capabilities = SimpleNamespace(
                disable_internet=False, network_allowlist=False,
                dynamic_network_policy=False,
            )
        if has_network_policy:
            self.network_policy = SimpleNamespace(
                network_mode=network_mode, allowed_hosts=allowed_hosts or [],
            )

    async def exec(self, command, *args, **kwargs):
        # the only out-of-band read surface used by probes
        if command.startswith("cat "):
            path = command[4:].strip()
            if path in self._files:
                return ExecResult(exit_code=0, stdout=self._files[path])
            return ExecResult(exit_code=1, stdout="", stderr="no such file")
        return ExecResult(exit_code=127, stdout="", stderr="not found")


def _file_observable(name="order_status", type_="string"):
    return ObservableSpec(name=name, type=type_, source=f"file:/workspace/{name}")


async def test_baseline_pass_on_matching_values():
    env = FakeEnv(files={"/workspace/order_status": "refunded"})
    ok, failures = await assert_baseline_arrival(
        env,
        [BaselineAssertion(
            id="status", probe="observable:order_status", equals="refunded",
        )],
        [_file_observable()],
    )
    assert ok, failures


async def test_baseline_fail_on_seed_mismatch():
    env = FakeEnv(files={"/workspace/order_count": "99"})
    ok, failures = await assert_baseline_arrival(
        env,
        [BaselineAssertion(
            id="seeded", probe="observable:order_count", equals=120,
        )],
        [_file_observable("order_count", "number")],
    )
    assert not ok
    assert "expected=120" in failures[0] and "actual=99" in failures[0]


async def test_baseline_fail_on_missing_env_handle():
    ok, failures = await assert_baseline_arrival(
        None,
        [BaselineAssertion(id="x", probe="observable:anything", equals=1)],
        [_file_observable("anything")],
    )
    assert not ok
    assert "no environment handle" in failures[0]


async def test_baseline_fail_on_undeclared_observable():
    """A baseline cannot probe an observable the suite never declared."""
    env = FakeEnv(files={"/workspace/ghost": "x"})
    ok, failures = await assert_baseline_arrival(
        env,
        [BaselineAssertion(id="x", probe="observable:ghost", equals="x")],
        [_file_observable("order_status")],
    )
    assert not ok
    assert "not declared by the suite" in failures[0]


async def test_probe_rejects_db_sources_fail_closed():
    env = FakeEnv()
    with pytest.raises(Exception, match="unsupported observable source"):
        await probe_observable(
            env,
            ObservableSpec(name="order", type="string", source="db:orders.status"),
        )


async def test_probe_reads_harbor_exec_result_return_code():
    """Harbor's ExecResult names the field ``return_code``; reading only
    ``exit_code`` made every real probe fail (aarch64 finding)."""
    class HarborExecEnv:
        async def exec(self, command):
            return SimpleNamespace(return_code=0, stdout="true", stderr=None)

    value = await probe_observable(
        HarborExecEnv(), ObservableSpec(name="ready", type="string",
                                        source="file:/workspace/ready")
    )
    assert value == "true"

    class FailingHarborExecEnv:
        async def exec(self, command):
            return SimpleNamespace(return_code=1, stdout="", stderr="missing")

    with pytest.raises(Exception, match="exited 1"):
        await probe_observable(
            FailingHarborExecEnv(),
            ObservableSpec(name="ready", type="string", source="file:/workspace/ready"),
        )


async def test_probe_rejects_env_without_exec():
    class NoExec:
        pass

    with pytest.raises(Exception, match="no exec"):
        await probe_observable(NoExec(), _file_observable())


async def test_probe_reports_cat_failure():
    env = FakeEnv(files={})  # file does not exist
    with pytest.raises(Exception, match="exited 1"):
        await probe_observable(env, _file_observable())


async def test_probe_parses_by_observable_type():
    env = FakeEnv(files={
        "/workspace/blob": '{"k": [1, 2]}',
        "/workspace/num": "42",
    })
    v_json = await probe_observable(env, ObservableSpec(
        name="blob", type="json", source="file:/workspace/blob"))
    assert v_json == {"k": [1, 2]}
    v_num = await probe_observable(env, ObservableSpec(
        name="num", type="number", source="file:/workspace/num"))
    assert v_num == 42
    with pytest.raises(Exception, match="not a number"):
        await probe_observable(env, ObservableSpec(
            name="blob", type="number", source="file:/workspace/blob"))


async def test_assert_expr_restricted_parser():
    class P:
        approval_policy = "allow"

    env = P()
    ok, failures = await assert_baseline_arrival(
        env, [BaselineAssertion(id="no_ask", assert_expr="approval_policy != 'ask'")]
    )
    assert ok, failures
    ok2, failures2 = await assert_baseline_arrival(
        env, [BaselineAssertion(id="no_ask", assert_expr="approval_policy == 'ask'")]
    )
    assert not ok2
    # Reject anything that is not a simple dotted-path comparison.
    ok3, failures3 = await assert_baseline_arrival(
        env, [BaselineAssertion(id="evil", assert_expr="__import__('os').system('x')")]
    )
    assert not ok3


async def test_clock_effective_detects_missing_handle():
    issues = await assert_clock_effective(
        None, ClockSpec(mode="virtual_offset", epoch="2026-09-16T00:00:00+08:00")
    )
    assert issues and "missing" in issues[0]


async def test_isolation_missing_handle_is_a_failure():
    """Unverifiable isolation must block, not silently pass."""
    issues = await assert_isolation_policy(None)
    assert issues and "no environment handle" in issues[0]


async def test_isolation_without_any_observable_fact_is_a_failure():
    """An environment exposing nothing to assert must not pass."""
    env = FakeEnv(has_approval_policy=False, has_network_policy=False)
    del env.capabilities
    issues = await assert_isolation_policy(env)
    assert issues and "unverifiable" in issues[0]


async def test_isolation_requires_a_policy_enforcing_environment():
    """A provider that can enforce no network policy cannot isolate."""
    issues = await assert_isolation_policy(FakeEnv(capabilities="none"))
    assert issues and "cannot isolate" in issues[0]


async def test_isolation_passes_on_a_real_shaped_e2b_environment():
    """The real e2b handle exposes capabilities (not approval_policy);
    that is a valid, observable isolation fact."""
    env = FakeEnv(has_approval_policy=False, capabilities="e2b")
    assert await assert_isolation_policy(env) == []


async def test_isolation_rejects_ask_policy():
    issues = await assert_isolation_policy(FakeEnv(approval_policy="ask"))
    assert issues and "ask" in issues[0]
    assert await assert_isolation_policy(FakeEnv(approval_policy="allow")) == []


async def test_egress_allows_no_network():
    assert await assert_egress_effective(FakeEnv(network_mode="no-network")) == []


async def test_egress_accepts_broker_allowlist():
    """A legitimate broker allowlist must NOT be misrejected
    as 'egress is not none'."""
    issues = await assert_egress_effective(
        FakeEnv(network_mode="allowlist", allowed_hosts=["broker.host"])
    )
    assert issues == []


async def test_egress_rejects_public():
    issues = await assert_egress_effective(FakeEnv(network_mode="public"))
    assert issues and "public" in issues[0]


async def test_egress_rejects_empty_allowlist():
    issues = await assert_egress_effective(
        FakeEnv(network_mode="allowlist", allowed_hosts=[])
    )
    assert issues and "empty" in issues[0]


async def test_egress_missing_handle_or_policy_is_a_failure():
    issues = await assert_egress_effective(None)
    assert issues and "no environment handle" in issues[0]
    issues2 = await assert_egress_effective(FakeEnv(has_network_policy=False))
    assert issues2 and "unverifiable" in issues2[0]


async def test_on_environment_started_marks_infra_invalid(tmp_path, demo_suite, runtime_lock):
    class Event:
        trial_id = "trial-7"
        environment = FakeEnv(files={"/workspace/order_count": "99"})  # wrong seed

    ctx = EvaluationContext(
        run_id="r", runtime_lock=runtime_lock, suite=demo_suite,
        run_dir=tmp_path, store_path=tmp_path / "s.db",
    )
    # Give the demo suite a file-backed observable + baseline we can hit.
    ctx.suite.overlay.observables.append(_file_observable("order_count", "number"))
    ctx.suite.overlay.baselines.append(
        BaselineAssertion(id="orders", probe="observable:order_count", equals=120)
    )
    await on_environment_started(Event(), ctx)
    state = ctx.trials["trial-7"]
    assert not state.baseline_ok
    assert state.stop_reason == "infra_error"
    assert any("baseline" in r for r in state.infra_invalid_reasons)


# --- e2b backend detection + observed identity binding -------


def test_verify_e2b_backend_missing_sdk(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "e2b":
            raise ImportError("no e2b")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(BackendNotAvailableError, match="not importable"):
        verify_e2b_backend()


def _lock_with_sandbox(runtime_lock):
    image = ImageIdentity(
        reference="ubuntu@sha256:" + "a" * 64, digest="a" * 64, platform="arm64",
    )
    return runtime_lock.model_copy(update={"images": {"sandbox": image}})


def test_bind_observed_identity_happy_path(runtime_lock):
    lock = _lock_with_sandbox(runtime_lock)
    observed = ObservedIdentity(
        backend="e2b",
        e2b_sdk_version="1.0.0",
        image_digest="sha256:" + "a" * 64,
        architecture="arm64",
        node_version="24.20.0",
    )
    bind_observed_identity(observed, lock)  # no raise


def test_bind_observed_identity_failures(runtime_lock):
    lock = _lock_with_sandbox(runtime_lock)
    base = dict(
        backend="e2b", e2b_sdk_version="1.0.0",
        image_digest="sha256:" + "a" * 64,
        architecture="arm64", node_version="24.20.0",
    )
    # wrong backend
    with pytest.raises(LockMismatchError, match="backend"):
        bind_observed_identity(ObservedIdentity(**{**base, "backend": "docker"}), lock)
    # missing SDK version
    with pytest.raises(LockMismatchError, match="e2b SDK version"):
        bind_observed_identity(ObservedIdentity(**{**base, "e2b_sdk_version": None}), lock)
    # unobserved image digest
    with pytest.raises(LockMismatchError, match="no image digest"):
        bind_observed_identity(ObservedIdentity(**{**base, "image_digest": None}), lock)
    # digest mismatch
    with pytest.raises(LockMismatchError, match="image digest"):
        bind_observed_identity(
            ObservedIdentity(**{**base, "image_digest": "sha256:" + "b" * 64}), lock)
    # architecture mismatch (wrong arch image)
    with pytest.raises(LockMismatchError, match="architecture"):
        bind_observed_identity(
            ObservedIdentity(**{**base, "architecture": "amd64"}), lock)
    # unobserved architecture
    with pytest.raises(LockMismatchError, match="no architecture"):
        bind_observed_identity(ObservedIdentity(**{**base, "architecture": None}), lock)
    # node version outside matrix
    with pytest.raises(LockMismatchError, match="outside.*matrix"):
        bind_observed_identity(
            ObservedIdentity(**{**base, "node_version": "24.19.0"}), lock)
    # unobserved node version
    with pytest.raises(LockMismatchError, match="no Node version"):
        bind_observed_identity(ObservedIdentity(**{**base, "node_version": None}), lock)


def test_bind_observed_identity_requires_locked_sandbox_image(runtime_lock):
    observed = ObservedIdentity(
        backend="e2b", e2b_sdk_version="1.0.0",
        image_digest="sha256:" + "a" * 64, architecture="arm64",
        node_version="24.20.0",
    )
    with pytest.raises(LockMismatchError, match="pins no 'sandbox' image"):
        bind_observed_identity(observed, runtime_lock)


@pytest.mark.parametrize(
    "observed_arch, expected_platform, ok",
    [
        # the real arm64 e2b sandbox reports uname -m aarch64 while the
        # lock/OCI manifest says arm64 (found on the aarch64 host)
        ("aarch64", "arm64", True),
        ("arm64", "arm64", True),
        ("x86_64", "amd64", True),
        ("amd64", "amd64", True),
        # a genuinely wrong architecture must still fail
        ("x86_64", "arm64", False),
        ("aarch64", "amd64", False),
        # an unrecognized name must not match by accident
        ("not-an-arch", "arm64", False),
    ],
)
def test_observed_architecture_uses_canonical_names(
    runtime_lock, observed_arch, expected_platform, ok
):
    lock = _lock_with_sandbox(runtime_lock).model_copy(
        update={
            "images": {
                "sandbox": ImageIdentity(
                    reference="harbor:443/e2b-orchestration/ubuntu@sha256:" + "a" * 64,
                    digest="a" * 64, platform=expected_platform,
                )
            }
        }
    )
    observed = ObservedIdentity(
        backend="e2b", e2b_sdk_version="2.50.0",
        image_digest="sha256:" + "a" * 64,
        architecture=observed_arch, node_version="24.20.0",
    )
    if ok:
        bind_observed_identity(observed, lock)
    else:
        with pytest.raises(LockMismatchError, match="architecture"):
            bind_observed_identity(observed, lock)


def test_node_matrix_minor_range_matches():
    from aeval.provenance import _node_in_matrix

    assert _node_in_matrix("22.19.4", ["22.19.x", "24.20.0"])
    assert _node_in_matrix("v24.20.0", ["22.19.x", "24.20.0"])
    assert not _node_in_matrix("22.20.0", ["22.19.x", "24.20.0"])
    assert not _node_in_matrix("24.19.0", ["22.19.x", "24.20.0"])


# --- what must be observed is declared, not hardcoded to DSH -------------

def test_a_non_dsh_agent_binds_without_a_node_observation(runtime_lock):
    """A python-only agent must not be gated on a Node fact it does not have."""
    without_dsh = runtime_lock.model_copy(update={"dsh": None})
    lock = _lock_with_sandbox(without_dsh)
    observed = ObservedIdentity(
        backend="e2b",
        e2b_sdk_version="1.0.0",
        image_digest="sha256:" + "a" * 64,
        architecture="arm64",
    )
    bind_observed_identity(observed, lock)  # no raise: nothing pins a node runtime
    # ...but declaring the node observation without a matrix is still refused
    with pytest.raises(LockMismatchError, match="no node matrix"):
        bind_observed_identity(observed, lock, required_observations=["node"])


def test_a_pinned_node_runtime_cannot_opt_out_of_the_observation(runtime_lock):
    """A lock that pins node forces the node observation — no declaration needed."""
    from aeval.contracts import AgentReleaseLock

    without_dsh = runtime_lock.model_copy(update={"dsh": None})
    without_dsh.agents["otheragent"] = AgentReleaseLock(
        id="otheragent", version="1.0.0", runtime="node", runtime_versions=["24.20.0"]
    )
    lock = _lock_with_sandbox(without_dsh)
    base = dict(
        backend="e2b", e2b_sdk_version="1.0.0",
        image_digest="sha256:" + "a" * 64, architecture="arm64",
    )
    # no declaration, but the pinned runtime is what decides
    with pytest.raises(LockMismatchError, match="no Node version"):
        bind_observed_identity(ObservedIdentity(**base), lock)
    with pytest.raises(LockMismatchError, match="outside"):
        bind_observed_identity(ObservedIdentity(**{**base, "node_version": "22.1.0"}), lock)
    bind_observed_identity(ObservedIdentity(**{**base, "node_version": "24.20.0"}), lock)


def test_the_dsh_adapter_declares_its_observed_facts():
    from aeval.agents.contract import declared_observations
    from aeval.agents.dsh.agent import DshAgent

    assert declared_observations(DshAgent) == frozenset({"node"})
