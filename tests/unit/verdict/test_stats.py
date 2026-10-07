"""轨迹采集（stats）测试：只陈述密封事实，缺失如实标注。"""

from __future__ import annotations

from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.models.trajectories.observation import Observation, ObservationResult

from aeval.contracts import CanonicalTranscript, TrialRecord
from aeval.verdict.trajectory.base import build_evidence
from aeval.verdict.trajectory.stats import collect_stats


def _call(cid: str, name: str = "bash", args: dict | None = None) -> ToolCall:
    return ToolCall(tool_call_id=cid, function_name=name, arguments=args or {})


def _obs(pairs: list[tuple[str, str]]) -> Observation:
    return Observation(
        results=[
            ObservationResult(source_call_id=c, content=t) for c, t in pairs
        ]
    )


_TS = "2026-10-05T09:00:{:02d}+00:00"


def sample_steps(with_ts: bool = True):
    def ts(sec: int):
        return _TS.format(sec) if with_ts else None

    return [
        Step(step_id=1, source="user", message="记住密钥 KEY-A",
             timestamp=ts(0), is_copied_context=True),
        Step(step_id=2, source="agent", message="已记录", timestamp=ts(8),
             is_copied_context=True,
             metrics=Metrics(prompt_tokens=760, completion_tokens=10)),
        Step(step_id=3, source="user", message="查一下", timestamp=ts(16)),
        Step(step_id=4, source="agent", message="", timestamp=ts(24),
             tool_calls=[_call("c1")], observation=_obs([("c1", "ok")]),
             metrics=Metrics(prompt_tokens=990, completion_tokens=44)),
        # 相邻同名同参 → 重试；无观测 → 失败信号。
        Step(step_id=5, source="agent", message="", timestamp=ts(34),
             tool_calls=[_call("c2")],
             metrics=Metrics(prompt_tokens=1400, completion_tokens=6,
                             cached_tokens=100)),
        Step(step_id=6, source="agent", message="完成", timestamp=ts(40),
             metrics=Metrics(prompt_tokens=500, completion_tokens=20)),
    ]


def sample_record(trial_id: str = "t1", task_id: str = "alpha.one",
                  verdict: str | None = "pass") -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": task_id, "trial_index": 0},
        stop_reason="agent_claimed_done",
        verdict=verdict,
        observed_model={"provider": "acme", "model": "m1"},
    )


def sample_stats(trial_id: str = "t1", task_id: str = "alpha.one",
                 verdict: str | None = "pass"):
    record = sample_record(trial_id, task_id, verdict)
    transcript = Trajectory(
        agent=Agent(name="dsh", version="test"),
        steps=sample_steps(),
        final_metrics=FinalMetrics(
            total_prompt_tokens=3650, total_completion_tokens=80,
            total_cached_tokens=100,
        ),
    )
    ct = CanonicalTranscript(
        atif=transcript, stop_reason="agent_claimed_done"
    )
    return collect_stats(record, build_evidence(ct, "agent_claimed_done"))


def test_collect_stats_session_facts():
    s = sample_stats()
    assert s.trial_id == "t1" and s.task_id == "alpha.one"
    assert s.model == "m1" and s.verdict == "pass"
    assert s.started_at == "2026-10-05T09:00:00+00:00"
    assert s.wall_clock_seconds == 40.0
    assert s.total_steps == 6
    assert (s.live_turns, s.copied_turns) == (1, 1)
    assert s.total_tokens == 3730
    assert s.total_cached_tokens == 100
    assert s.peak_input_tokens == 1400 and s.peak_step_id == 5


def test_collect_stats_timeline_series():
    s = sample_stats()
    assert [p.seconds_from_start for p in s.steps] == [0, 8, 16, 24, 34, 40]
    assert [p.duration_seconds for p in s.steps] == [8, 8, 8, 10, 6, None]
    assert s.steps[0].copied and not s.steps[2].copied
    assert s.steps[3].tool_names == ("bash",)
    assert not s.steps[3].observation_missing
    assert s.steps[4].observation_missing  # c2 无观测


def test_collect_stats_tool_matrix_facts():
    s = sample_stats()
    (bash,) = s.tools
    assert bash.name == "bash"
    assert (bash.calls, bash.observed, bash.missing_obs) == (2, 1, 1)
    assert bash.retries == 1               # c2 与 c1 同名同参且相邻
    assert bash.step_seconds == 16.0       # step4(10s) + step5(6s)
    assert bash.avg_seconds == 8.0 and bash.longest_seconds == 10.0
    assert s.longest_call == ("bash", 10.0)
    assert s.tool_calls_total == 2
    assert s.tool_missing_obs_total == 1 and s.tool_retries_total == 1
    assert s.tool_time_share == 16.0 / 40.0


def test_totals_fall_back_to_step_sums():
    """final_metrics 缺总量时回退逐步求和——仍只来自密封证据。"""
    record = sample_record()
    transcript = Trajectory(
        agent=Agent(name="dsh", version="test"),
        steps=sample_steps(),
        final_metrics=FinalMetrics(),
    )
    ct = CanonicalTranscript(
        atif=transcript, stop_reason="agent_claimed_done"
    )
    s = collect_stats(record, build_evidence(ct, "agent_claimed_done"))
    assert s.total_prompt_tokens == 3650
    assert s.total_completion_tokens == 80
    assert s.total_tokens == 3730


def test_missing_timestamps_skip_with_reason():
    record = sample_record()
    transcript = Trajectory(
        agent=Agent(name="dsh", version="test"),
        steps=sample_steps(with_ts=False),
        final_metrics=FinalMetrics(
            total_prompt_tokens=3650, total_completion_tokens=80,
            total_cached_tokens=100,
        ),
    )
    ct = CanonicalTranscript(
        atif=transcript, stop_reason="agent_claimed_done"
    )
    s = collect_stats(record, build_evidence(ct, "agent_claimed_done"))
    assert s.wall_clock_seconds is None
    assert s.started_at is None
    assert any("时间戳" in m for m in s.missing)
    (bash,) = s.tools
    assert bash.avg_seconds is None and bash.longest_seconds is None
    assert s.longest_call is None and s.tool_time_share is None
    # token 面不受影响。
    assert s.peak_input_tokens == 1400 and s.total_tokens == 3730


def test_collect_stats_is_deterministic():
    assert sample_stats() == sample_stats()
