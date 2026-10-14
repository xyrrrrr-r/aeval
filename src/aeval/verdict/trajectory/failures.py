"""失败感知：从密封证据里读出「为什么失败」与「哪一步相关」。

判分只给二值判决，面板此前只能呈现「失败」这个词——理由与相关步骤都在证据里，
只是没有被整理出来。这个模块把它们整理成可展示的失败摘要，边界写死在下面：

- **判分器理由逐字引用**（``GradeResult.reasons`` / ``MetricOutcome.reasons``），
  不改写、不概括；只在前面加上来源标签（grader 或 metric 名）。
- **任务自带 verifier 的失败详情只在摘要匹配时读取**：它的 sha256 必须等于运行级
  密封清单 ``attestation.json`` 里同一文件的摘要；不匹配 / 缺失 → 明确写「不可用」，
  绝不猜一个原因。
- **「相关步骤」只有两条可陈述的来源**，且必须写明用的是哪条：
  (a) ``grader``：判分器证据里显式的 ``failures at: step N``；
  (b) ``path``：失败详情里出现的文件路径与该步工具调用参数里的路径字面相等。
  两条都不成立就留空（``None``），宁可不说。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "AttestedVerifier",
    "FailureReport",
    "FailingTest",
    "attestation_index",
    "build_failure_report",
    "grader_reasons",
    "implicated_steps",
    "load_attested_verifier",
    "locate_steps",
    "parse_failing_tests",
]

#: ``FAILED ../tests/test_outputs.py::test_x - AssertionError: msg``
_FAILED_LINE = re.compile(r"^FAILED\s+(?P<node>\S+)\s+-\s+(?P<message>.+)$", re.M)
#: pytest's ``____________________________ test_x ____________________________``
_SECTION = re.compile(r"^_{3,}\s*(?P<name>[^\s_][^\n]*?)\s*_{3,}\s*$", re.M)
#: assertion body lines inside a failure section
_E_LINE = re.compile(r"^E\s+(?P<line>\S.*)$", re.M)
#: the trajectory graders state implicated steps explicitly
_STEP_HINT = re.compile(r"\bfailures?\s+at:\s*step\s*(?P<step>\d+)", re.I)
#: a metric reason that states a *nonzero* failure count ("1 of 8 tool calls failed").
#: "0 of 7 tool calls failed" / "no failed tool calls" are healthy statements and
#: must never be listed as failure reasons.
_FAILURE_COUNT = re.compile(
    r"\b[1-9]\d*\s+of\s+\d+\s+(?:tool calls|calls|steps)\s+failed", re.I
)
#: absolute-ish paths mentioned by a failure; a leading ``../`` is not one
_PATH = re.compile(r"(?<![\w.\-/])(/[A-Za-z0-9_.\-/]{2,})")

#: how much of a verifier trace is quoted into the panel (bounded on purpose)
_EXCERPT_LINES = 40
_MAX_FAILING_TESTS = 5


@dataclass(frozen=True)
class FailingTest:
    """One failing test as the task's own verifier reported it."""

    name: str
    message: str


@dataclass(frozen=True)
class AttestedVerifier:
    """A verifier trace that matched the run's sealed manifest."""

    rel_path: str
    sha256: str
    text: str

    @property
    def excerpt(self) -> str:
        lines = self.text.splitlines()
        if len(lines) <= _EXCERPT_LINES:
            return self.text
        head = lines[: _EXCERPT_LINES // 2]
        tail = lines[-(_EXCERPT_LINES // 2):]
        return "\n".join([*head, f"…（省略 {len(lines) - _EXCERPT_LINES} 行）", *tail])


@dataclass(frozen=True)
class FailureReport:
    """A trial's failure, as far as the sealed evidence can state it.

    Two step signals are kept apart on purpose — they mean different things:

    - ``path_steps``: steps whose tool arguments literally carry a path the
      failure detail mentions. For an outcome failure this is the artifact the
      verifier complained about, i.e. the closest thing to a cause the sealed
      evidence supports;
    - ``composed_steps``: steps whose arguments carry the failure path's
      directory and file name **separately** (e.g. the command ``cd /app/ssl &&
      … > verification.txt`` for the failure path ``/app/ssl/verification.txt``).
      A weaker, still mechanical rule: the transcript never states the resolved
      path, so this is a candidate, not an equality;
    - ``grader_steps``: steps the trajectory grader itself reported as a failed
      tool call (``failures at: step N``). Related, but not a cause claim.
    """

    verdict: str | None
    reasons: tuple[str, ...] = ()
    failing_tests: tuple[FailingTest, ...] = ()
    implicated_steps: tuple[int, ...] = ()   # union, for the track highlight
    path_steps: tuple[int, ...] = ()
    composed_steps: tuple[int, ...] = ()
    grader_steps: tuple[int, ...] = ()
    step_basis: str | None = None          # "path" | "grader" | None
    verifier_source: str | None = None     # attested relative path
    verifier_sha256: str | None = None
    verifier_excerpt: str | None = None
    unavailable: tuple[str, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> str:
        """One line for the at-a-glance strips (panel insights, cards).

        Prefers the failing test *with its name* — pytest's own summary line is
        often truncated ("AssertionError: Ver..."), so the name is what makes it
        identifiable at a glance.
        """
        if self.failing_tests:
            first = self.failing_tests[0]
            return f"{first.name}: {first.message}"
        if self.reasons:
            return self.reasons[0]
        return "无可陈述的失败详情"


def _tool_argument_blobs(step: Any) -> str:
    """Every string the step's tool calls carried, flattened for path matching."""
    calls = getattr(step, "tool_calls", None)
    if calls is None and isinstance(step, dict):
        calls = step.get("tool_calls")
    blobs: list[str] = []
    for call in calls or ():
        arguments = getattr(call, "arguments", None)
        if arguments is None and isinstance(call, dict):
            arguments = call.get("arguments")
        if arguments is None:
            continue
        if isinstance(arguments, str):
            blobs.append(arguments)
        else:
            try:
                blobs.append(json.dumps(arguments, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                blobs.append(str(arguments))
    return "\n".join(blobs)


def _step_id(step: Any) -> int | None:
    value = getattr(step, "step_id", None)
    if value is None and isinstance(step, dict):
        value = step.get("step_id")
    return value if isinstance(value, int) else None


def parse_failing_tests(text: str, limit: int = _MAX_FAILING_TESTS) -> tuple[FailingTest, ...]:
    """Parse pytest-style ``FAILED`` lines; fall back to the failure sections.

    ``limit`` bounds what the panel quotes, newest-first order is preserved.
    An unrecognised format yields an empty tuple — the caller then says the
    detail is unavailable rather than inventing a reason.
    """
    found: list[FailingTest] = []
    for match in _FAILED_LINE.finditer(text or ""):
        node = match.group("node")
        name = node.rsplit("::", 1)[-1] if "::" in node else node
        found.append(FailingTest(name=name, message=match.group("message").strip()))
        if len(found) >= limit:
            return tuple(found)
    if found:
        return tuple(found)
    # No summary line (e.g. an interrupted run): use the failure sections and
    # the first assertion line under each.
    sections = list(_SECTION.finditer(text or ""))
    for index, section in enumerate(sections):
        name = section.group("name").strip()
        if not name or name.startswith("short test summary"):
            continue
        body_start = section.end()
        body_end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        body = (text or "")[body_start:body_end]
        lines = _E_LINE.findall(body)
        message = lines[0].strip() if lines else "（无断言输出）"
        found.append(FailingTest(name=name, message=message))
        if len(found) >= limit:
            break
    return tuple(found)


def _metric_reason_reports_failure(status: Any, reason: str) -> bool:
    """Does one metric reason actually report a failure?

    Yes when the metric is ``violated`` (an integrity breach) or when the reason
    itself states a failure: a nonzero failure count or the explicit
    ``failures at: step N`` hint. Healthy statements — ``0 of 7 tool calls
    failed``, ``no failed tool calls``, a ``degraded`` informational metric —
    are left out: a failure list that cites them reads as evidence of failure
    when the evidence says the opposite.
    """
    if status == "violated":
        return True
    if _STEP_HINT.search(reason):
        return True
    return bool(_FAILURE_COUNT.search(reason))


def grader_reasons(record: Any) -> tuple[str, ...]:
    """The verbatim reasons the store recorded, prefixed by their source label.

    Includes every non-pass grade's reasons plus the metrics reasons that report
    a failure (see :func:`_metric_reason_reports_failure`) — those are the only
    step-level hints the sealed store actually carries.
    """
    out: list[str] = []
    for grade in getattr(record, "grades", None) or ():
        grader = f"{getattr(grade, 'grader_id', '?')}@{getattr(grade, 'grader_version', '')}"
        status = getattr(grade, "status", None)
        for reason in getattr(grade, "reasons", None) or ():
            if status != "pass":
                out.append(f"{grader}: {reason}")
        for metric in getattr(grade, "metrics", None) or ():
            name = getattr(metric, "name", "?")
            metric_status = getattr(metric, "status", None)
            for reason in getattr(metric, "reasons", None) or ():
                if _metric_reason_reports_failure(metric_status, reason):
                    out.append(f"{grader} · {name}: {reason}")
    return tuple(out)


def locate_steps(
    texts: Iterable[str], steps: Sequence[Any]
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """``(path_steps, composed_steps, grader_steps)``, one rule each.

    ``path_steps``: the failure's path appears verbatim in a step's tool
    arguments. ``composed_steps``: its directory and its file name appear
    separately (the transcript never states the resolved path, so this stays a
    candidate). ``grader_steps``: the ``failures at: step N`` hints the
    trajectory grader emitted. None of the three is a causal claim, so the panel
    labels them apart.
    """
    material = [text for text in texts if text]
    grader: set[int] = set()
    for text in material:
        grader.update(int(m.group("step")) for m in _STEP_HINT.finditer(text))
    paths = {
        path.rstrip(".,;:)")
        for text in material
        for path in _PATH.findall(text)
    }
    exact: list[int] = []
    composed: list[int] = []
    if paths:
        for step in steps:
            blob = _tool_argument_blobs(step)
            if not blob:
                continue
            step_id = _step_id(step)
            if step_id is None:
                continue
            literal = [path for path in paths if path in blob]
            if literal:
                exact.append(step_id)
                continue
            for path in paths:
                directory, _, name = path.rpartition("/")
                if directory and name and directory in blob and name in blob:
                    composed.append(step_id)
                    break
    return tuple(sorted(set(exact))), tuple(sorted(set(composed))), tuple(sorted(grader))


def implicated_steps(
    texts: Iterable[str], steps: Sequence[Any]
) -> tuple[tuple[int, ...], str | None]:
    """Union of both step signals plus the primary basis, for one-line callers."""
    path_steps, composed_steps, grader_steps = locate_steps(texts, steps)
    union = tuple(sorted({*path_steps, *composed_steps, *grader_steps}))
    if path_steps:
        basis = "path"
    elif composed_steps:
        basis = "composed"
    elif grader_steps:
        basis = "grader"
    else:
        basis = None
    return union, basis


def attestation_index(run_dir: Path) -> dict[str, tuple[str, str]]:
    """``trial dir name → (relative path, sha256)`` for attested verifier traces."""
    index: dict[str, tuple[str, str]] = {}
    try:
        payload = json.loads((Path(run_dir) / "attestation.json").read_text("utf-8"))
    except (OSError, ValueError):
        return index
    for entry in payload.get("entries", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(digest, str):
            continue
        parts = Path(rel).parts
        if len(parts) < 3 or parts[-2:] != ("verifier", "test-stdout.txt"):
            continue
        index[parts[-3]] = (rel, digest)
    return index


def load_attested_verifier(
    trial_dir: Path | None, index: dict[str, tuple[str, str]] | None
) -> AttestedVerifier | None:
    """The trial's verifier trace, only when its sha256 matches the seal."""
    if trial_dir is None or not index:
        return None
    entry = index.get(Path(trial_dir).name)
    if entry is None:
        return None
    rel, expected = entry
    path = Path(trial_dir) / "verifier" / "test-stdout.txt"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != expected:
        return None
    return AttestedVerifier(
        rel_path=rel, sha256=expected, text=data.decode("utf-8", errors="replace")
    )


def build_failure_report(
    record: Any,
    *,
    steps: Sequence[Any] = (),
    trial_dir: Path | None = None,
    index: dict[str, tuple[str, str]] | None = None,
) -> FailureReport | None:
    """A failed trial's digest, or ``None`` when the trial did not fail."""
    verdict = getattr(record, "verdict", None)
    if verdict == "pass":
        return None
    reasons = grader_reasons(record)
    verifier = load_attested_verifier(trial_dir, index)
    failing: tuple[FailingTest, ...] = ()
    unavailable: list[str] = []
    if verifier is None:
        unavailable.append(
            "任务自带 verifier 的失败详情不可用：密封清单里没有该试次的 "
            "verifier/test-stdout.txt 摘要，或文件摘要不匹配——只呈现判分器理由"
        )
    else:
        failing = parse_failing_tests(verifier.text)
        if not failing:
            unavailable.append(
                "verifier 输出里没有可解析的 FAILED 段——只呈现判分器理由"
            )
    texts = [*reasons]
    if verifier is not None:
        texts.append(verifier.text)
    path_steps, composed_steps, grader_steps = locate_steps(texts, steps)
    implicated = tuple(sorted({*path_steps, *composed_steps, *grader_steps}))
    if path_steps:
        basis = "path"
    elif composed_steps:
        basis = "composed"
    elif grader_steps:
        basis = "grader"
    else:
        basis = None
    if not implicated:
        unavailable.append(
            "相关步骤未定位：失败详情里的路径没有与任何一步的工具调用参数字面"
            "相等，判分器证据也没有给出 step 编号"
        )
    return FailureReport(
        verdict=verdict,
        reasons=reasons,
        failing_tests=failing,
        implicated_steps=implicated,
        path_steps=path_steps,
        composed_steps=composed_steps,
        grader_steps=grader_steps,
        step_basis=basis,
        verifier_source=verifier.rel_path if verifier else None,
        verifier_sha256=verifier.sha256 if verifier else None,
        verifier_excerpt=verifier.excerpt if verifier else None,
        unavailable=tuple(unavailable),
    )
