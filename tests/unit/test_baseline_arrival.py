"""Baseline arrival tests: a non-baseline copy must never score."""

from __future__ import annotations

from aeval.hooks.baseline_arrival import (
    assert_baseline_arrival,
    assert_clock_effective,
    assert_egress_effective,
    assert_isolation_policy,
    on_environment_started,
)
from aeval.hooks.context import EvaluationContext
from aeval.suite_models import BaselineAssertion, ClockSpec, ObservableSpec


class FakeEnv:
    def __init__(self, *, inspect_values=None, clock_epoch=None,
                 approval_policy=None, egress_policy=None):
        self._inspect = inspect_values or {}
        self.clock_epoch = clock_epoch
        self.approval_policy = approval_policy
        self.egress_policy = egress_policy

    def inspect(self, name):
        return self._inspect.get(name)


async def test_baseline_pass_on_matching_values():
    env = FakeEnv(inspect_values={"order_status": "refunded"})
    ok, failures = await assert_baseline_arrival(
        env,
        [
            BaselineAssertion(
                id="status", probe="observable:order_status", equals="refunded"
            )
        ],
    )
    assert ok, failures


async def test_baseline_fail_on_seed_mismatch():
    env = FakeEnv(inspect_values={"order_count": 99})
    ok, failures = await assert_baseline_arrival(
        env,
        [
            BaselineAssertion(
                id="seeded", probe="observable:order_count", equals=120
            )
        ],
    )
    assert not ok
    assert "expected=120" in failures[0] and "actual=99" in failures[0]


async def test_baseline_fail_on_missing_env_handle():
    ok, failures = await assert_baseline_arrival(
        None,
        [BaselineAssertion(id="x", probe="observable:anything", equals=1)],
    )
    assert not ok


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


async def test_clock_effective_detects_missing_and_mismatch():
    issues = await assert_clock_effective(
        None, ClockSpec(mode="virtual_offset", epoch="2026-09-16T00:00:00+08:00")
    )
    assert issues and "missing" in issues[0]
    issues2 = await assert_clock_effective(
        FakeEnv(clock_epoch="2026-09-16T00:00:00+08:00"),
        ClockSpec(mode="virtual_offset", epoch="2026-09-16T00:00:00+08:00"),
    )
    assert issues2 == []
    issues3 = await assert_clock_effective(
        FakeEnv(clock_epoch="2026-01-01T00:00:00Z"),
        ClockSpec(mode="virtual_offset", epoch="2026-09-16T00:00:00+08:00"),
    )
    assert issues3 and "mismatch" in issues3[0]
    # real clock mode: nothing to assert
    assert await assert_clock_effective(FakeEnv(), ClockSpec(mode="real")) == []


async def test_isolation_rejects_ask_policy():
    issues = await assert_isolation_policy(FakeEnv(approval_policy="ask"))
    assert issues and "ask" in issues[0]
    assert await assert_isolation_policy(FakeEnv(approval_policy="allow")) == []
    assert await assert_isolation_policy(FakeEnv()) == []


async def test_egress_must_be_none():
    issues = await assert_egress_effective(FakeEnv(egress_policy="public"))
    assert issues and "not 'none'" in issues[0]
    assert await assert_egress_effective(FakeEnv(egress_policy="none")) == []


async def test_on_environment_started_marks_infra_invalid(tmp_path, demo_suite, runtime_lock):
    class Event:
        trial_id = "trial-7"

        class environment:
            pass

        env = FakeEnv(inspect_values={"order_count": 99})  # wrong seed

    ctx = EvaluationContext(
        run_id="r", runtime_lock=runtime_lock, suite=demo_suite,
        run_dir=tmp_path, store_path=tmp_path / "s.db",
    )
    # Give the demo suite a baseline we can hit through the env handle.
    ctx.suite.overlay.baselines.append(
        BaselineAssertion(id="orders", probe="observable:order_count", equals=120)
    )
    await on_environment_started(Event(), ctx)
    state = ctx.trials["trial-7"]
    assert not state.baseline_ok
    assert state.stop_reason == "infra_error"
    assert any("baseline" in r for r in state.infra_invalid_reasons)
