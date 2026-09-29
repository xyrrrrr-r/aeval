"""The conformance kit: what any adapter must prove before it is evaluated.

Not a style guide. Each check maps to a way a second agent breaks an evaluation
*silently* — which is the only kind of breakage that matters here, because a loud
failure is cheap and a quiet one is a wrong number:

1. ``declaration``   — the declaration resolves and agrees with the adapter class
                       (a drifting declaration is a lie nobody notices);
2. ``contract``      — the members the evidence chain reads all exist, so
                       collection cannot fail with an ``AttributeError`` midway;
3. ``capabilities``  — the adapter offers what the suite requires *before* a
                       sandbox is built;
4. ``accounting``    — spend can be enforced when the suite caps it, and a cap the
                       adapter cannot enforce is refused rather than overspent;
5. ``transcript``    — ``read_trial_session()`` returns real ATIF, or raises. What
                       it must never do is return something that looks complete
                       and is not.

A check that cannot run reports ``skipped`` with its reason — never ``pass``.
"Could not exercise" and "works" are different facts and are recorded as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from aeval.agents.contract import (
    adapter_declaration_gap,
    adapter_member_gap,
    session_record_output_of,
    budget_gate_violation,
    build_adapter_spec,
    capabilities_of,
    declared_observations,
    load_adapter_class,
)
from aeval.agents.declaration import (
    declaration_class_mismatches,
    resolve_agent_declaration,
)
from aeval.contracts import AdapterSpec, CanonicalTranscript
from aeval.suite_models import SuiteError

CheckStatus = Literal["pass", "fail", "skipped"]


@dataclass(frozen=True)
class ConformanceCheck:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class ConformanceReport:
    adapter: str
    checks: list[ConformanceCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    @property
    def failures(self) -> list[ConformanceCheck]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def skipped(self) -> list[ConformanceCheck]:
        return [check for check in self.checks if check.status == "skipped"]

    def render(self) -> str:
        lines = [f"conformance: {self.adapter}"]
        for check in self.checks:
            lines.append(f"  [{check.status}] {check.name}: {check.detail}")
        verdict = "OK" if self.ok else "FAILED"
        lines.append(f"  -> {verdict} ({len(self.failures)} failed, {len(self.skipped)} skipped)")
        return "\n".join(lines)


def _instantiate(adapter: type) -> tuple[object | None, str | None]:
    try:
        return adapter(), None
    except Exception as exc:  # noqa: BLE001 - an unbuildable adapter is a finding, not a crash
        return None, f"{type(exc).__name__}: {exc}"


def check_declaration(declaration_path: Path, adapter: type, root: Path) -> ConformanceCheck:
    resolved = resolve_agent_declaration(declaration_path, agents_root=root)
    mismatches = declaration_class_mismatches(resolved.declaration, adapter)
    if mismatches:
        return ConformanceCheck(
            "declaration",
            "fail",
            f"{resolved.declaration.id}: declaration disagrees with the adapter class: "
            + "; ".join(mismatches),
        )
    chain = " -> ".join(source.path for source in resolved.sources)
    return ConformanceCheck("declaration", "pass", f"{resolved.declaration.id}: {chain}")


def check_contract(adapter: type) -> ConformanceCheck:
    members = adapter_member_gap(adapter)
    if members:
        return ConformanceCheck(
            "contract",
            "fail",
            f"missing members {members} — the evidence chain reads them at trial end",
        )
    declarations = adapter_declaration_gap(adapter)
    if declarations:
        return ConformanceCheck(
            "contract",
            "fail",
            f"missing declarations {declarations} — the run could not record which adapter ran",
        )
    spec = build_adapter_spec(adapter)
    observations = sorted(declared_observations(adapter))
    return ConformanceCheck(
        "contract",
        "pass",
        f"members present; id={spec.id} mode={spec.mode} observations={observations}",
    )


def check_capabilities(
    import_path: str,
    adapter: type,
    required: list[str] | None,
    session_record: str | None = None,
) -> ConformanceCheck:
    provided = capabilities_of(adapter)
    if required is None:
        return ConformanceCheck(
            "capabilities",
            "skipped",
            f"no suite given; adapter provides {sorted(provided)}",
        )
    missing = sorted({item for item in required if item not in provided})
    if missing:
        return ConformanceCheck(
            "capabilities",
            "fail",
            f"suite requires {sorted(required)}, adapter provides {sorted(provided)}, missing {missing}",
        )
    if session_record is not None:
        # The session-record slot pairing (refused at composition in a real
        # run) is reported here too: a conformance pass must not stay silent
        # about a pairing that would be refused the moment it is composed.
        flavor = session_record_output_of(adapter)
        if flavor != session_record:
            return ConformanceCheck(
                "capabilities",
                "fail",
                f"suite's session record is {session_record!r}, adapter "
                f"produces {flavor!r} — the pairing is refused at composition",
            )
    detail = f"provides {sorted(required)} required by the suite"
    if session_record is not None:
        detail += f"; session record {session_record!r}"
    return ConformanceCheck("capabilities", "pass", detail)


def check_accounting(
    adapter: type, budget: object | None
) -> ConformanceCheck:
    spec = build_adapter_spec(adapter)
    if budget is None:
        return ConformanceCheck(
            "accounting",
            "skipped",
            f"no suite budget given; enforcement={spec.budget_enforcement!r}",
        )
    violation = budget_gate_violation([spec], budget, accepted=False)
    if violation:
        return ConformanceCheck("accounting", "fail", violation)
    return ConformanceCheck(
        "accounting",
        "pass",
        f"enforcement={spec.budget_enforcement!r} satisfies the suite's cap",
    )


def check_transcript(adapter: type, instance: object | None = None) -> ConformanceCheck:
    """Exercise ``read_trial_session()`` for real when an instance is available.

    A live instance is preferred over constructing one: an adapter that needs
    constructor arguments (``DshAgent`` needs ``logs_dir``) can only be exercised
    with one, and "could not run" must not be confused with "works". Callers with
    a real instance — a run, a plugin, a fixture — pass it in.
    """
    supplied = instance is not None
    if instance is None:
        instance, error = _instantiate(adapter)
        if instance is None:
            return ConformanceCheck(
                "transcript",
                "skipped",
                f"adapter is not instantiable without arguments ({error}); "
                "pass instance=... from a real run to exercise it",
            )
    try:
        transcript = instance.read_trial_session()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - a fresh instance has no session yet
        return ConformanceCheck(
            "transcript",
            "skipped",
            f"read_trial_session() raised {type(exc).__name__}: {exc} — exercised but "
            "nothing was recorded to read",
        )
    if not isinstance(transcript, CanonicalTranscript):
        return ConformanceCheck(
            "transcript",
            "fail",
            f"read_trial_session() returned {type(transcript).__name__}, not a "
            "CanonicalTranscript — grading would read fields that do not exist",
        )
    if transcript.completeness is None:
        return ConformanceCheck(
            "transcript",
            "fail",
            "transcript carries no completeness record — cannot_judge could never fire",
        )
    source = "supplied instance" if supplied else "fresh instance"
    return ConformanceCheck(
        "transcript",
        "pass",
        f"{source}: ATIF step count={len(transcript.atif.steps)} "
        f"fields={[item.field for item in transcript.completeness.fields]}",
    )


def run_conformance(
    adapter: type,
    *,
    import_path: str,
    declaration_path: Path | None = None,
    agents_root: Path | None = None,
    required_capabilities: list[str] | None = None,
    session_record: str | None = None,
    budget: object | None = None,
    instance: object | None = None,
) -> ConformanceReport:
    """Run every check and report, including what could not be exercised."""
    report = ConformanceReport(adapter=import_path)
    if declaration_path is not None:
        try:
            report.checks.append(
                check_declaration(
                    declaration_path, adapter, agents_root or Path(declaration_path).parent
                )
            )
        except SuiteError as exc:
            report.checks.append(ConformanceCheck("declaration", "fail", str(exc)))
    else:
        report.checks.append(
            ConformanceCheck(
                "declaration",
                "skipped",
                "no agents/<id>.yaml given; the adapter is only described by its class",
            )
        )
    report.checks.append(check_contract(adapter))
    report.checks.append(
        check_capabilities(import_path, adapter, required_capabilities, session_record)
    )
    report.checks.append(check_accounting(adapter, budget))
    report.checks.append(check_transcript(adapter, instance))
    return report


def run_conformance_for(
    import_path: str,
    *,
    declaration_path: Path | None = None,
    agents_root: Path | None = None,
    suite_paths: list[Path] | None = None,
) -> list[ConformanceReport]:
    """Conformance for one adapter, once per suite it is paired with.

    The pairing is what makes the capability and budget checks meaningful: an
    adapter is not conformant in the abstract, only against the suites it runs.
    """
    adapter = load_adapter_class(import_path)
    if not suite_paths:
        return [
            run_conformance(
                adapter,
                import_path=import_path,
                declaration_path=declaration_path,
                agents_root=agents_root,
            )
        ]
    from aeval.suite_loader.loader import load_suite

    reports = []
    for suite_path in suite_paths:
        suite = load_suite(suite_path)
        reports.append(
            run_conformance(
                adapter,
                import_path=import_path,
                declaration_path=declaration_path,
                agents_root=agents_root,
                required_capabilities=list(suite.overlay.driver.require),
                session_record=getattr(
                    suite.overlay.driver, "session_record", "dsh_session"
                ),
                budget=suite.overlay.budget,
            )
        )
    return reports


__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "check_accounting",
    "check_capabilities",
    "check_contract",
    "check_declaration",
    "check_transcript",
    "run_conformance",
    "run_conformance_for",
]
