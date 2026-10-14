"""Failure perception: what the panel is allowed to say about a failed trial.

The rules under test are the honesty rules: grader reasons are quoted verbatim,
a task verifier's failing assertions are read only from a trace whose sha256
matches the run's sealed attestation, and "related steps" are stated only when
either the grader named a step or a path literally matches a tool argument.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from aeval.verdict.trajectory.failures import (
    attestation_index,
    build_failure_report,
    grader_reasons,
    implicated_steps,
    load_attested_verifier,
    locate_steps,
    parse_failing_tests,
)

_OPENSSL_STDOUT = """\
============================= test session starts ==============================
platform linux -- Python 3.10.12, pytest-9.1.1, pluggy-1.6.0
rootdir: /tests
collected 6 items

../tests/test_outputs.py F.....                                          [100%]

=================================== FAILURES ===================================
____________________________ test_verification_file ____________________________

    def test_verification_file():
        verification_file = "/app/ssl/verification.txt"
>       assert "sha256" in verification_content.lower(), (
            "Verification file does not contain SHA-256 fingerprint"
        )
E       AssertionError: Verification file does not contain SHA-256 fingerprint
E       assert 'sha256' in 'sha-256 fingerprint: 90:a1'
=========================== short test summary info ============================
FAILED ../tests/test_outputs.py::test_verification_file - AssertionError: Verification file does not contain SHA-256 fingerprint
========================= 1 failed, 5 passed in 0.08s =========================
"""


def _grade(**over):
    base = dict(
        grader_id="terminal-beach-trajectory",
        grader_version="v1",
        layer="trajectory",
        status="ok",
        reasons=[],
        metrics=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _metric(name, status, reasons):
    return SimpleNamespace(name=name, status=status, reasons=list(reasons))


def _record(verdict, grades):
    return SimpleNamespace(verdict=verdict, grades=list(grades))


# --- parsing ---------------------------------------------------------------


def test_parses_the_failing_test_and_its_assertion():
    failing = parse_failing_tests(_OPENSSL_STDOUT)
    assert len(failing) == 1
    assert failing[0].name == "test_verification_file"
    assert "SHA-256 fingerprint" in failing[0].message


def test_falls_back_to_sections_when_there_is_no_summary_line():
    text = """\
=================================== FAILURES ===================================
________________________________ test_a _________________________________________
    def test_a():
>       assert False, "boom"
E       AssertionError: boom

========================= 1 failed in 0.01s =========================
"""
    failing = parse_failing_tests(text)
    assert [t.name for t in failing] == ["test_a"]
    assert failing[0].message == "AssertionError: boom"


def test_an_unrecognised_trace_yields_nothing_rather_than_a_guess():
    assert parse_failing_tests("Command exited with code 1\nno pytest output\n") == ()
    assert parse_failing_tests("") == ()


# --- reasons ---------------------------------------------------------------


def test_healthy_metric_statements_are_not_listed_as_failure_reasons():
    """"0 of 7 tool calls failed" / "no failed tool calls" say nothing failed."""
    record = _record(
        "fail",
        [
            _grade(
                status="fail",
                reasons=["score below threshold"],
                metrics=[
                    _metric("tool_error_rate", "ok", ["0 of 7 tool calls failed"]),
                    _metric("recovery", "ok", ["no failed tool calls — nothing to recover from"]),
                    _metric("budget_adherence", "degraded", ["trial stopped by infrastructure (agent_exit_0); not an agent behaviour"]),
                    _metric("secret_leakage", "violated", ["the agent read the verifier's tests"]),
                ],
            )
        ],
    )
    reasons = grader_reasons(record)
    assert reasons == (
        "terminal-beach-trajectory@v1: score below threshold",
        "terminal-beach-trajectory@v1 · secret_leakage: the agent read the verifier's tests",
    )


def test_grader_reasons_are_quoted_with_their_source_and_verbatim():
    record = _record(
        "fail",
        [
            _grade(
                grader_id="terminal-beach-outcome",
                status="fail",
                reasons=["upstream verifier reward is 0 (the task's tests failed)"],
            ),
            _grade(
                status="pass",
                reasons=["this passing reason is not part of the failure digest"],
                metrics=[
                    _metric("tool_error_rate", "ok", ["1 of 8 tool calls failed", "failures at: step 7"]),
                    _metric("loop_detection", "ok", ["no call repeated 3+ times consecutively"]),
                ],
            ),
        ],
    )
    reasons = grader_reasons(record)
    assert reasons[0] == (
        "terminal-beach-outcome@v1: upstream verifier reward is 0 "
        "(the task's tests failed)"
    )
    assert "terminal-beach-trajectory@v1 · tool_error_rate: failures at: step 7" in reasons
    assert all("no call repeated" not in reason for reason in reasons)
    assert all("not part of the failure digest" not in reason for reason in reasons)


# --- implicated steps ------------------------------------------------------


def _step(step_id, file_path=None, command=None):
    arguments = {}
    if file_path is not None:
        arguments["file_path"] = file_path
    if command is not None:
        arguments["command"] = command
    return SimpleNamespace(
        step_id=step_id,
        tool_calls=[SimpleNamespace(function_name="edit", arguments=arguments)],
    )


def test_an_explicit_grader_step_hint_is_kept_in_its_own_bucket():
    steps = [_step(6, file_path="/app/ssl/verification.txt")]
    found, basis = implicated_steps(["tool_error_rate: failures at: step 7"], steps)
    assert found == (7,)
    assert basis == "grader"
    path_steps, composed_steps, grader_steps = locate_steps(
        ["tool_error_rate: failures at: step 7"], steps
    )
    assert path_steps == ()
    assert composed_steps == ()
    assert grader_steps == (7,)


def test_a_path_match_locates_the_step_that_touched_it():
    steps = [
        _step(2, command="mkdir -p /app/ssl"),
        _step(6, file_path="/app/ssl/verification.txt"),
    ]
    found, basis = implicated_steps(
        ["assert 'sha256' in open('/app/ssl/verification.txt').read()"], steps
    )
    assert found == (6,)
    assert basis == "path"


def test_python_node_paths_are_not_treated_as_artifacts():
    # ``../tests/test_outputs.py`` is the verifier's own file, not an artifact
    # the agent ever touched: nothing must be attributed from it.
    steps = [_step(3, command="ls -la /app")]
    found, basis = implicated_steps(["FAILED ../tests/test_outputs.py::test_x"], steps)
    assert found == ()
    assert basis is None


def test_a_relative_write_after_cd_is_a_candidate_not_an_equality():
    """``cd /app/ssl && … > verification.txt`` for ``/app/ssl/verification.txt``."""
    steps = [
        _step(4, command='cd /app/ssl && echo "x" > verification.txt'),
        _step(9, command="ls -la /app"),
    ]
    path_steps, composed_steps, grader_steps = locate_steps(
        ["assert 'sha256' in open('/app/ssl/verification.txt').read()"], steps
    )
    assert path_steps == ()
    assert composed_steps == (4,)
    assert grader_steps == ()
    union, basis = implicated_steps(
        ["assert 'sha256' in open('/app/ssl/verification.txt').read()"], steps
    )
    assert union == (4,)
    assert basis == "composed"


def test_a_literal_path_wins_over_the_composed_candidate():
    steps = [
        _step(4, command="cd /app/ssl && echo x > verification.txt"),
        _step(6, file_path="/app/ssl/verification.txt"),
    ]
    path_steps, composed_steps, _ = locate_steps(
        ["/app/ssl/verification.txt"], steps
    )
    assert path_steps == (6,)
    assert composed_steps == (4,)


def test_no_match_stays_empty_instead_of_guessing():
    steps = [_step(4, file_path="/app/other.txt")]
    found, basis = implicated_steps(["assert False, 'nothing to match'"], steps)
    assert found == ()
    assert basis is None


# --- attested verifier trace ----------------------------------------------


def _attested_trial(tmp_path):
    trial = tmp_path / "harbor" / "job" / "openssl__ABC"
    (trial / "verifier").mkdir(parents=True)
    trace = trial / "verifier" / "test-stdout.txt"
    trace.write_text(_OPENSSL_STDOUT, encoding="utf-8")
    digest = hashlib.sha256(trace.read_bytes()).hexdigest()
    run_dir = tmp_path
    (run_dir / "attestation.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "path": "harbor/job/openssl__ABC/verifier/test-stdout.txt",
                        "sha256": digest,
                    },
                    {"path": "harbor/job/openssl__ABC/canonical_transcript.json", "sha256": "x"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return run_dir, trial


def test_an_attested_trace_is_read_and_indexed_by_trial_dir(tmp_path):
    run_dir, trial = _attested_trial(tmp_path)
    index = attestation_index(run_dir)
    assert "openssl__ABC" in index
    loaded = load_attested_verifier(trial, index)
    assert loaded is not None
    assert loaded.rel_path.endswith("verifier/test-stdout.txt")
    assert parse_failing_tests(loaded.text)[0].name == "test_verification_file"


def test_a_trace_whose_digest_does_not_match_is_refused(tmp_path):
    run_dir, trial = _attested_trial(tmp_path)
    index = attestation_index(run_dir)
    (trial / "verifier" / "test-stdout.txt").write_text("tampered", encoding="utf-8")
    assert load_attested_verifier(trial, index) is None


def test_a_trial_absent_from_the_manifest_is_refused(tmp_path):
    run_dir, trial = _attested_trial(tmp_path)
    assert load_attested_verifier(trial, {"other__XYZ": ("p", "d")}) is None
    assert load_attested_verifier(None, attestation_index(run_dir)) is None


# --- the digest the panel renders -----------------------------------------


def test_a_passing_trial_has_no_failure_digest(tmp_path):
    run_dir, trial = _attested_trial(tmp_path)
    record = _record("pass", [_grade(status="pass", reasons=[])])
    assert build_failure_report(record, trial_dir=trial, index=attestation_index(run_dir)) is None


def test_the_digest_states_reason_test_and_steps():
    steps = [
        _step(2, command="mkdir -p /app/ssl"),
        _step(6, file_path="/app/ssl/verification.txt"),
    ]
    record = _record(
        "fail",
        [
            _grade(
                grader_id="terminal-beach-outcome",
                status="fail",
                reasons=["upstream verifier reward is 0 (the task's tests failed)"],
            )
        ],
    )
    trial = None  # no verifier trace at all
    report = build_failure_report(record, steps=steps, trial_dir=trial, index={})
    assert report is not None
    assert report.verdict == "fail"
    assert report.reasons and "reward is 0" in report.reasons[0]
    assert report.failing_tests == ()
    assert report.implicated_steps == ()
    assert any("不可用" in note for note in report.unavailable)
    assert any("相关步骤未定位" in note for note in report.unavailable)
    assert report.summary.startswith("terminal-beach-outcome@v1")


def test_both_step_signals_are_reported_separately(tmp_path):
    """A failed tool call and the artifact the verifier named are different facts."""
    run_dir, trial = _attested_trial(tmp_path)
    steps = [
        _step(6, file_path="/app/ssl/verification.txt"),
        _step(7, command="cd /app && python3 check_cert.py"),
    ]
    record = _record(
        "fail",
        [
            _grade(
                grader_id="terminal-beach-outcome",
                status="fail",
                reasons=["upstream verifier reward is 0"],
            ),
            _grade(
                status="pass",
                metrics=[_metric("tool_error_rate", "ok", ["failures at: step 7"])],
            ),
        ],
    )
    report = build_failure_report(
        record, steps=steps, trial_dir=trial, index=attestation_index(run_dir)
    )
    assert report is not None
    assert report.path_steps == (6,)
    assert report.grader_steps == (7,)
    assert report.implicated_steps == (6, 7)
    assert report.step_basis == "path"


def test_the_digest_carries_the_composed_candidate(tmp_path):
    run_dir, trial = _attested_trial(tmp_path)
    steps = [_step(4, command="cd /app/ssl && echo x > verification.txt")]
    record = _record(
        "fail",
        [_grade(grader_id="terminal-beach-outcome", status="fail", reasons=["reward is 0"])],
    )
    report = build_failure_report(
        record, steps=steps, trial_dir=trial, index=attestation_index(run_dir)
    )
    assert report is not None
    assert report.path_steps == ()
    assert report.composed_steps == (4,)
    assert report.implicated_steps == (4,)
    assert report.step_basis == "composed"


def test_the_digest_uses_the_attested_trace_for_detail_and_steps(tmp_path):
    run_dir, trial = _attested_trial(tmp_path)
    steps = [_step(6, file_path="/app/ssl/verification.txt")]
    record = _record(
        "fail",
        [_grade(grader_id="terminal-beach-outcome", status="fail", reasons=["reward is 0"])],
    )
    report = build_failure_report(
        record, steps=steps, trial_dir=trial, index=attestation_index(run_dir)
    )
    assert report is not None
    assert report.failing_tests[0].name == "test_verification_file"
    assert report.summary.startswith("test_verification_file: AssertionError")
    assert report.implicated_steps == (6,)
    assert report.path_steps == (6,)
    assert report.grader_steps == ()
    assert report.step_basis == "path"
    assert report.verifier_sha256 and len(report.verifier_sha256) == 64
    assert report.verifier_excerpt and "test_verification_file" in report.verifier_excerpt
    assert report.unavailable == ()


@pytest.mark.parametrize("verdict", ["fail", "cannot_judge", "infra_invalid"])
def test_non_pass_verdicts_get_a_digest(verdict):
    record = _record(verdict, [])
    report = build_failure_report(record)
    assert report is not None and report.verdict == verdict
