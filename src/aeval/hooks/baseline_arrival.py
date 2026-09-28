"""Baseline arrival assertions (plan §2): prove the copy reached baseline.

Runs on ENVIRONMENT_START, i.e. after the environment is created but
before the agent starts. Every failure writes stop_reason=infra_error
and marks the trial infra_invalid — a non-baseline environment must
never produce a valid score.

Hook raises are not reliable isolation in Harbor (START is outside the
try block; other hook errors have no unified isolation), so the
canonical failure mode is recording, not raising.
"""

from __future__ import annotations

import re
from typing import Any

from aeval.hooks.context import EvaluationContext, TrialState
from aeval.suite_models import BaselineAssertion, ClockSpec, ObservableSpec

__all__ = [
    "on_environment_started",
    "assert_baseline_arrival",
    "probe_observable",
    "assert_clock_effective",
    "assert_isolation_policy",
    "assert_egress_effective",
    "mark_infra_invalid",
]


class _BaselineFailure(Exception):
    """A probe/assertion failure, tagged with WHY it failed.

    ``kind`` separates two very different situations:

    - ``absent``: the environment answered and the fact is not there
      (the file does not exist, the value does not parse). That is a
      statement about the RUN, so collection records it as an observed
      absence and the grader scores it — it is NOT an infrastructure
      failure;
    - ``unobservable``: nothing could be observed at all (no handle, no
      exec, unsupported source). That blocks the trial.
    """

    def __init__(self, assertion_id: str, expected: Any, actual: Any, probe: str,
                 kind: str = "unobservable"):
        self.assertion_id = assertion_id
        self.expected = expected
        self.actual = actual
        self.probe = probe
        self.kind = kind
        super().__init__(
            f"baseline {assertion_id!r} failed: probe={probe!r} "
            f"expected={expected!r} actual={actual!r}"
        )


def mark_infra_invalid(context: EvaluationContext, trial_id: str, reason: str) -> None:
    """Record an infra failure for the trial (never raise out of hooks)."""
    state = context.trial_state(trial_id)
    state.mark_infra_invalid(reason)


async def probe_observable(
    env_handle: Any, observable: ObservableSpec
) -> Any:
    """Read one observable through the environment's out-of-band API.

    The read uses the REAL environment API — ``await env.exec(...)`` —
    which is a different surface from the agent's tools (L3 isolation):
    the agent can never invoke this. There is no ``env.inspect()`` in
    Harbor (P0-2: that was a fabricated API).

    Supported ``source`` kinds:
    - ``file:<path>`` — read the file inside the sandbox and parse it
      according to the observable type;
    - anything else (``db:...``) is an explicit unsupported probe: it
      fails closed with a clear message instead of fabricating a value.
    """
    if env_handle is None:
        raise _BaselineFailure(
            observable.name, observable.type,
            "<no environment handle>", observable.source,
        )
    source = observable.source or ""
    kind, _, payload = source.partition(":")
    if kind != "file" or not payload:
        raise _BaselineFailure(
            observable.name, observable.type,
            f"<unsupported observable source: {source!r}>", source,
        )
    exec_fn = getattr(env_handle, "exec", None)
    if not callable(exec_fn):
        raise _BaselineFailure(
            observable.name, observable.type,
            "<environment handle exposes no exec()>", source,
        )
    path = payload.strip()
    result = await exec_fn(f"cat {path}")
    exit_code = _exec_return_code(result)
    if exit_code is None:
        raise _BaselineFailure(
            observable.name, observable.type,
            "<exec returned no exit code>", source,
        )
    if exit_code != 0:
        raise _BaselineFailure(
            observable.name, observable.type,
            f"<cat {path} exited {exit_code}>", source, kind="absent",
        )
    stdout = getattr(result, "stdout", "")
    return _parse_observable_value(stdout, observable)


def _exec_return_code(result: Any) -> int | None:
    """Return code of a Harbor ``ExecResult`` (``return_code``).

    Harbor's environment API names the field ``return_code``; older
    fakes and other providers used ``exit_code``. Reading only one of
    them silently yielded ``None`` on the real e2b backend and turned
    every probe into a baseline failure (found during the aarch64
    environment verification), so both spellings are accepted.
    """
    for attribute in ("return_code", "exit_code"):
        value = getattr(result, attribute, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _parse_observable_value(raw: Any, observable: ObservableSpec) -> Any:
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    if observable.type == "string":
        return text.strip()
    if observable.type == "number":
        try:
            return float(text.strip())
        except ValueError:
            raise _BaselineFailure(
                observable.name, observable.type,
                f"<not a number: {text.strip()!r}>", observable.source,
            )
    if observable.type == "json":
        import json

        try:
            return json.loads(text)
        except ValueError:
            raise _BaselineFailure(
                observable.name, observable.type,
                f"<not valid json: {text.strip()!r}>", observable.source,
            )
    raise _BaselineFailure(
        observable.name, observable.type,
        f"<unsupported observable type: {observable.type!r}>",
        observable.source,
    )


async def assert_baseline_arrival(
    env_handle: Any,
    assertions: list[BaselineAssertion],
    observables: list[ObservableSpec] | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate all baseline assertions; return (ok, failure messages).

    ``observable:<name>`` probes resolve to the SUITE-declared
    ObservableSpec (the real source syntax) before probing, so a
    baseline can never invent its own probe target.
    """
    by_name = {o.name: o for o in (observables or [])}
    failures: list[str] = []
    for assertion in assertions:
        try:
            if assertion.assert_expr is not None:
                if not _eval_assert_expr(env_handle, assertion.assert_expr):
                    raise _BaselineFailure(
                        assertion.id, True, False, assertion.assert_expr
                    )
                continue
            if assertion.probe is None:
                failures.append(f"baseline {assertion.id!r}: no probe and no assert")
                continue
            actual = await _run_probe(env_handle, assertion.probe, by_name)
            if _normalize(actual) != _normalize(assertion.equals):
                raise _BaselineFailure(
                    assertion.id, assertion.equals, actual, assertion.probe
                )
        except _BaselineFailure as exc:
            failures.append(str(exc))
    return (not failures, failures)


async def _run_probe(
    env_handle: Any, probe: str, observables: dict[str, ObservableSpec] | None = None
) -> Any:
    """Execute a probe of the form ``kind:payload``.

    Supported kinds: ``observable`` (resolve the suite-declared spec,
    then read it out-of-band via the environment API), ``env`` (raw env
    attribute access for policy assertions in tests), ``noop``. Any
    other kind — including ``db:`` — fails closed: fabricating a value
    is never an option.
    """
    kind, _, payload = probe.partition(":")
    kind = kind.strip()
    if kind == "noop":
        return None
    if kind == "observable":
        spec = (observables or {}).get(payload)
        if spec is None:
            raise _BaselineFailure(
                payload, "<suite-declared observable>",
                "<observable not declared by the suite>", probe,
            )
        return await probe_observable(env_handle, spec)
    if kind == "env":
        if env_handle is None:
            return None
        return getattr(env_handle, payload, None)
    raise _BaselineFailure(payload, "<probe executed>", "<unsupported probe kind>", probe)


def _eval_assert_expr(env_handle: Any, expr: str) -> bool:
    """Restricted ``a.b.c != value`` / ``a.b.c == value`` expressions.

    Only the two comparison operators against dotted attribute paths
    are accepted; anything else is a failure, never an eval().
    """
    match = re.fullmatch(r"\s*([A-Za-z_][\w.]*)\s*(!=|==)\s*(.+?)\s*", expr)
    if not match:
        return False
    path, op, literal = match.groups()
    obj: Any = env_handle
    for part in path.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    expected = _parse_literal(literal)
    if op == "==":
        return _normalize(obj) == _normalize(expected)
    return _normalize(obj) != _normalize(expected)


def _parse_literal(text: str) -> Any:
    import ast

    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text.strip().strip("'\"")


def _normalize(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


async def assert_clock_effective(env_handle: Any, clock: ClockSpec) -> list[str]:
    """Virtual clock must actually be visible inside the copy (case T4)."""
    if clock.mode != "virtual_offset":
        return []
    if env_handle is None:
        return ["virtual clock declared but env handle missing"]
    effective = getattr(env_handle, "clock_epoch", None)
    if effective is None:
        return [
            "virtual clock declared but not effective in the environment "
            f"(expected epoch {clock.epoch!r}, saw nothing)"
        ]
    if str(effective) != str(clock.epoch):
        return [
            f"virtual clock mismatch: expected {clock.epoch!r}, "
            f"actual {effective!r}"
        ]
    return []


async def assert_isolation_policy(env_handle: Any) -> list[str]:
    """The trial must run inside an isolated, policy-enforcing sandbox.

    Two things are asserted, both from facts the handle really owns:

    1. the DSH-side interaction approval policy, when the owner injected
       one, must not be ``ask`` (case D19) — an unasserted interactive
       approval silently changes agent behaviour mid-trial;
    2. the environment itself must be able to enforce the requested
       network policy: providers report this through ``capabilities``
       (e.g. e2b: ``disable_internet``/``network_allowlist``/
       ``dynamic_network_policy``). A provider that can enforce nothing
       cannot isolate the copy.

    P0-2: a missing handle, or an environment where NO isolation fact is
    observable at all, is an ISSUE — never a silent pass. Environment
    verification on the real e2b backend showed that ``approval_policy``
    does not exist on a Harbor environment object, so treating its
    absence as the only signal made the check permanently fail while
    proving nothing; the check now asserts the environment-owned facts
    and still refuses when nothing is observable.
    """
    if env_handle is None:
        return ["isolation policy unverifiable: no environment handle"]

    issues: list[str] = []
    observed: list[str] = []

    approval = getattr(env_handle, "approval_policy", None)
    if approval is not None:
        observed.append("approval_policy")
        if str(approval).strip().lower() == "ask":
            issues.append(
                "approval policy is 'ask' — interactive approval is not "
                "assertable; declare a non-interactive policy"
            )

    capabilities = getattr(env_handle, "capabilities", None)
    if capabilities is not None:
        observed.append("capabilities")
        enforceable = any(
            getattr(capabilities, attribute, False) is True
            for attribute in ("disable_internet", "network_allowlist",
                              "dynamic_network_policy")
        )
        if not enforceable:
            issues.append(
                "environment reports no network-policy capability "
                "(disable_internet/network_allowlist/dynamic_network_policy) "
                "— it cannot isolate the copy"
            )

    if getattr(env_handle, "network_policy", None) is not None:
        observed.append("network_policy")

    if not observed:
        issues.append(
            "isolation policy unverifiable: the environment exposes no "
            "approval policy, capabilities or network policy — refuse "
            "rather than assume a safe default"
        )
    return issues


async def assert_egress_effective(env_handle: Any) -> list[str]:
    """Egress must be actually enforced inside the copy, not just configured.

    P0-2: the assertion reads the REAL ``network_policy`` of the live
    environment (what the container can reach), never a fabricated
    ``egress_policy`` attribute. Semantics:

    - ``public`` — uncontrolled egress: rejected;
    - ``no-network`` — fixture semantics: accepted;
    - ``allowlist`` — the broker/package-source allowlist the doc
      mandates for the model phase: ACCEPTED (the old check
      misrejected it as "not none"); an empty host list is rejected as
      a misdeclared allowlist;
    - no handle / no observable policy — an ISSUE, never a silent pass.
    """
    if env_handle is None:
        return ["egress policy unverifiable: no environment handle"]
    policy = getattr(env_handle, "network_policy", None)
    if policy is None:
        return [
            "egress policy unverifiable: the environment exposes no "
            "network_policy — refuse rather than assume a safe default"
        ]
    mode = getattr(policy, "network_mode", None)
    mode_str = str(getattr(mode, "value", mode) or "").strip().lower()
    if mode_str == "public":
        return [
            "egress is 'public' inside the copy — uncontrolled network "
            "access is not assertable; use no-network or an allowlist"
        ]
    if mode_str == "allowlist":
        hosts = getattr(policy, "allowed_hosts", None) or []
        if not hosts:
            return [
                "egress allowlist is empty — declare no-network instead of "
                "an allowlist that reaches nothing"
            ]
        return []
    if mode_str in ("no-network", "no_network", "none", "off", "false"):
        return []
    return [
        f"egress policy is not recognizable as enforced: {mode_str!r}"
    ]


async def on_environment_started(
    event: Any, context: EvaluationContext, env_handle: Any = None
) -> None:
    """Baseline gate, always record-only.

    Failures mark the trial infra_invalid; we do not rely on raising.

    ``env_handle`` is the started environment supplied by the owner
    (``EvaluationContext.environments``), because Harbor's hook events
    carry no environment object. It must be called at the point where
    the sandbox actually exists: Harbor emits ENVIRONMENT_START *before*
    ``environment.start()``, so the owner invokes this from AGENT_START
    (sandbox created + healthcheck passed + agent installed, agent not
    yet running). A missing handle stays a blocking condition.
    """
    trial_id = str(getattr(event, "trial_id", ""))
    state = context.trial_state(trial_id)
    suite = context.suite.overlay

    if env_handle is None:
        env_handle = getattr(event, "environment", None) or getattr(event, "env", None)

    ok, failures = await assert_baseline_arrival(
        env_handle, suite.baselines, suite.observables
    )
    if not ok:
        state.baseline_ok = False
        state.baseline_failures.extend(failures)
        for line in failures:
            state.mark_infra_invalid(f"baseline_arrival: {line}")

    for issue in await assert_clock_effective(env_handle, suite.clock):
        state.mark_infra_invalid(f"clock: {issue}")
    for issue in await assert_isolation_policy(env_handle):
        state.mark_infra_invalid(f"isolation: {issue}")
    for issue in await assert_egress_effective(env_handle):
        state.mark_infra_invalid(f"egress: {issue}")

    if not state.infra_invalid_reasons:
        state.baseline_ok = state.baseline_ok and True
