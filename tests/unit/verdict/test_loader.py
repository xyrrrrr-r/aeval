"""Grader loader tests: versioned loading is fail-closed.

A grader module that lies about its identity, mismatches the declared
version, or lacks the coroutine entry point never executes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aeval.suite_models import GraderDeclaration
from aeval.verdict.loader import GraderLoadError, _load_module, load_grader, split_impl

_GOOD = '''
GRADER_ID = "outcome"
GRADER_VERSION = "v7"
LAYER = "outcome"
REQUIRED_FIELDS = ["events"]

async def grade(record):
    from aeval.contracts import GradeResult, Score
    return GradeResult(
        grader_id=GRADER_ID,
        grader_version=GRADER_VERSION,
        layer="outcome",
        score=Score(value=1.0),
        status="pass",
        reasons=["ok"],
    )
'''


def _write(tmp_path: Path, body: str, name: str = "grader.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_split_impl_parses_reference_and_version():
    assert split_impl("graders/outcome.py@v7") == ("graders/outcome.py", "v7")


@pytest.mark.parametrize("bad", ["no-version", "x.txt@v1", "a.py@", "@v1", ""])
def test_split_impl_rejects_malformed(bad):
    with pytest.raises(GraderLoadError, match="must be '<path>.py@<version>'"):
        split_impl(bad)


async def test_load_grader_returns_protocol_object(tmp_path):
    path = _write(tmp_path, _GOOD)
    resolved = load_grader(path, GraderDeclaration(impl="grader.py@v7"))
    assert resolved.grader.id == "outcome"
    assert resolved.grader.version == "v7"
    assert resolved.execution == "pure"
    assert resolved.requires_fields == ["events"]
    assert resolved.veto is False


def test_load_grader_missing_file(tmp_path):
    with pytest.raises(GraderLoadError, match="does not exist"):
        load_grader(tmp_path / "nope.py", GraderDeclaration(impl="nope.py@v1"))


def test_load_grader_import_crash_is_load_error(tmp_path):
    path = _write(tmp_path, "raise RuntimeError('boom during import')\n")
    with pytest.raises(GraderLoadError, match="raised during import"):
        load_grader(path, GraderDeclaration(impl="grader.py@v1"))


def test_load_grader_requires_grader_id(tmp_path):
    body = _GOOD.replace('GRADER_ID = "outcome"', "")
    with pytest.raises(GraderLoadError, match="non-empty string GRADER_ID"):
        load_grader(_write(tmp_path, body), GraderDeclaration(impl="grader.py@v7"))


def test_load_grader_requires_version(tmp_path):
    body = _GOOD.replace('GRADER_VERSION = "v7"', 'GRADER_VERSION = ""')
    with pytest.raises(GraderLoadError, match="non-empty string GRADER_VERSION"):
        load_grader(_write(tmp_path, body), GraderDeclaration(impl="grader.py@v7"))


def test_load_grader_layer_mismatch(tmp_path):
    body = _GOOD.replace('LAYER = "outcome"', 'LAYER = "trajectory"')
    with pytest.raises(GraderLoadError, match="declares layer 'trajectory'"):
        load_grader(_write(tmp_path, body), GraderDeclaration(impl="grader.py@v7"))


def test_load_grader_sync_grade_rejected(tmp_path):
    body = _GOOD.replace("async def grade", "def grade")
    with pytest.raises(GraderLoadError, match="async def grade"):
        load_grader(_write(tmp_path, body), GraderDeclaration(impl="grader.py@v7"))


def test_load_grader_missing_grade_rejected(tmp_path):
    body = "GRADER_ID = 'outcome'\nGRADER_VERSION = 'v7'\nLAYER = 'outcome'\n"
    with pytest.raises(GraderLoadError, match="async def grade"):
        load_grader(_write(tmp_path, body), GraderDeclaration(impl="grader.py@v7"))


def test_load_grader_bad_required_fields(tmp_path):
    body = _GOOD.replace('REQUIRED_FIELDS = ["events"]', "REQUIRED_FIELDS = [1, 2]")
    with pytest.raises(GraderLoadError, match="REQUIRED_FIELDS"):
        load_grader(_write(tmp_path, body), GraderDeclaration(impl="grader.py@v7"))


def test_load_grader_exec_declaration_still_pure_object(tmp_path):
    """Loading does not change execution class; refusing exec happens at
    execution time (see executor tests). The loader only verifies the
    module contract."""
    resolved = load_grader(
        _write(tmp_path, _GOOD),
        GraderDeclaration(impl="grader.py@v7", layer="outcome", veto=True),
    )
    assert resolved.veto is True


async def test_loaded_grader_grade_returns_result(tmp_path):
    from aeval.contracts import TrialRecord

    resolved = load_grader(_write(tmp_path, _GOOD), GraderDeclaration(impl="grader.py@v7"))
    record = TrialRecord(
        trial_id="t",
        coordinates={"run_id": "r", "suite_id": "s", "suite_version": "1",
                     "task_id": "task", "trial_index": 0},
        stop_reason="agent_exit_0",
    )
    result = await resolved.grader.grade(record)
    assert result.grader_id == "outcome"
    assert result.status == "pass"
    assert result.score.value == 1.0


def test_loading_a_grader_writes_nothing_into_the_suite(tmp_path: Path) -> None:
    """Importing a grader must not leave ``__pycache__`` in a sealed suite.

    Found by the first real run of the generic facade flavor: grading the trial
    created ``graders/__pycache__`` inside the suite, and the NEXT run was
    refused by the provenance gate ("Suite ... has uncommitted changes") because
    the suite tree is an input, not an output. Evaluating a suite may not mutate
    the version it claims to have run.
    """
    grader = tmp_path / "graders" / "hello.py"
    grader.parent.mkdir(parents=True)
    grader.write_text(
        'IMPLEMENTATION = "graders/hello.py@v1"\n'
        'VERSION = "v1"\n'
        "async def grade(record):\n"
        "    return None\n",
        encoding="utf-8",
    )

    module = _load_module(grader)

    assert module.IMPLEMENTATION == "graders/hello.py@v1"
    assert list(grader.parent.iterdir()) == [grader]


def test_the_bytecode_flag_is_restored(tmp_path: Path) -> None:
    """The guard is scoped: the rest of the process keeps its own setting."""
    grader = tmp_path / "grader.py"
    grader.write_text("async def grade(record):\n    return None\n", encoding="utf-8")
    before = sys.dont_write_bytecode

    _load_module(grader)

    assert sys.dont_write_bytecode is before
