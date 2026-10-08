"""Control-artifact discovery: the one resolution point.

Both historical call sites (facade dist, DSH session reader) now build their
candidate lists here; these tests pin the shared discipline itself so a new
control package cannot grow its own private search order.
"""

from __future__ import annotations

from pathlib import Path

from aeval.control.artifacts import control_artifact_candidates
from aeval.control.bootstrap import facade_dist_candidates
from aeval.agents.dsh.agent import session_reader_candidates


def test_the_environment_override_is_always_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AEVAL_FACADE_DIST", str(tmp_path / "override"))
    candidates = control_artifact_candidates(
        env="AEVAL_FACADE_DIST", package="deepagents-eval-control", inner="dist",
    )
    assert candidates[0] == tmp_path / "override"
    # and it is not duplicated by the walk
    assert candidates.count(tmp_path / "override") == 1


def test_an_env_naming_a_package_root_appends_its_inner_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AEVAL_DSH_CONTROL_ROOT", str(tmp_path / "root"))
    candidates = control_artifact_candidates(
        env="AEVAL_DSH_CONTROL_ROOT", env_inner="dist/session_reader.js",
        package="dsh-eval-control", inner="dist/session_reader.js",
    )
    assert candidates[0] == tmp_path / "root" / "dist" / "session_reader.js"


def test_the_sibling_walk_climbs_from_the_anchor(tmp_path):
    anchor = tmp_path / "a" / "b" / "c"
    candidates = control_artifact_candidates(
        package="pkg", inner="dist", start=anchor,
    )
    assert candidates[0] == tmp_path / "a" / "b" / "c" / "pkg" / "dist"
    assert tmp_path / "pkg" / "dist" in candidates
    # climbing stops at the filesystem root, nothing after
    assert candidates[-1] == Path(candidates[-1].anchor) / "pkg" / "dist"


def test_explicit_anchors_replace_the_walk_and_extra_candidates_append(tmp_path):
    candidates = control_artifact_candidates(
        package="pkg", inner="dist", walk_from=(tmp_path / "here", tmp_path / "there"),
        extra=(tmp_path / "repo" / "node_modules" / "pkg" / "dist",),
    )
    assert candidates == [
        tmp_path / "here" / "pkg" / "dist",
        tmp_path / "there" / "pkg" / "dist",
        tmp_path / "repo" / "node_modules" / "pkg" / "dist",
    ]


def test_candidates_never_duplicate(tmp_path):
    duplicated = tmp_path / "pkg" / "dist"
    candidates = control_artifact_candidates(
        package="pkg", inner="dist", walk_from=(tmp_path,), extra=(duplicated,),
    )
    assert candidates == [duplicated]


def test_both_call_sites_delegate_with_their_historical_order(tmp_path, monkeypatch):
    """The unified discipline must not move a resolved artifact: each call
    site's candidate list keeps its pre-unification shape."""
    monkeypatch.delenv("AEVAL_FACADE_DIST", raising=False)
    facade = facade_dist_candidates(tmp_path)
    assert facade[0] == tmp_path / "deepagents-eval-control" / "dist"
    assert facade[1] == tmp_path.parent / "deepagents-eval-control" / "dist"

    monkeypatch.delenv("AEVAL_DSH_CONTROL_ROOT", raising=False)
    reader = session_reader_candidates()
    # sibling checkout first, then the installed node_modules layout
    assert reader[0].parts[-3:-1] == ("dsh-eval-control", "dist")
    assert any(
        part == "node_modules" for part in reader[1].parts
    ), reader
