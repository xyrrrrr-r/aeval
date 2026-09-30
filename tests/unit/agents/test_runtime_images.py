"""Runtime declarations and the runtime→image table (方案二).

The fact is split on purpose: the declaration says what the agent's sandbox must
provide, the table says where that is met for a given suite. What matters here is
that the join is checked BEFORE a sandbox is built — an unmapped pairing is a
refusal naming the fix, not a "command not found" inside a running trial.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aeval.agents.declaration import resolve_agent_declaration
from aeval.agents.runtime import (
    RuntimeDeclaration,
    RuntimeImageTable,
    default_runtime_images_path,
    image_problem_for,
    load_runtime_images,
)
from aeval.suite_models import SuiteError

REPO = Path(__file__).resolve().parents[3]
AGENTS = REPO / "agents"


# --- the declaration half ----------------------------------------------------


def test_the_shipped_adapters_declare_what_their_sandbox_needs():
    dsh = resolve_agent_declaration(AGENTS / "dsh.yaml").declaration
    assert dsh.runtime is not None and dsh.runtime.key == "dsh"
    assert dsh.runtime.needs, "the needs are what a refusal quotes back"

    deepagent = resolve_agent_declaration(AGENTS / "deepagent.yaml").declaration
    assert deepagent.runtime is not None
    assert deepagent.runtime.key == "deepagents-code"


def test_a_runtime_key_must_be_a_slug():
    assert RuntimeDeclaration(key="deepagents-code").key == "deepagents-code"
    with pytest.raises(Exception, match="lowercase slug"):
        RuntimeDeclaration(key="Deep Agents Code")
    with pytest.raises(Exception, match="non-empty strings"):
        RuntimeDeclaration(key="dsh", needs=[""])


# --- the table half ----------------------------------------------------------


def test_the_shipped_table_maps_every_shipped_pairing():
    table = load_runtime_images(AGENTS / "_runtime" / "images.yaml")
    pairs = {(entry.suite, entry.runtime) for entry in table.images}
    assert ("tbench-pilot", "dsh") in pairs
    assert ("e2e-hello", "dsh") in pairs
    assert ("deepagent-hello", "deepagents-code") in pairs
    assert ("deepagent-budget", "deepagents-code") in pairs


def test_a_missing_table_is_an_empty_table_not_an_error(tmp_path: Path):
    assert load_runtime_images(tmp_path / "nope.yaml").images == []


def test_the_env_override_points_at_another_table(tmp_path, monkeypatch):
    table = tmp_path / "images.yaml"
    table.write_text("schema_version: 1\nimages: []\n", encoding="utf-8")
    monkeypatch.setenv("AEVAL_RUNTIME_IMAGES", str(table))
    assert default_runtime_images_path() == table
    assert load_runtime_images().images == []


def test_a_pinned_image_must_be_digest_pinned_with_a_platform(tmp_path: Path):
    bad = tmp_path / "images.yaml"
    bad.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "images": [{"suite": "s", "runtime": "r", "image": "nginx:1"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="digest-pinned"):
        load_runtime_images(bad)

    no_platform = tmp_path / "no-platform.yaml"
    no_platform.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "images": [
                    {"suite": "s", "runtime": "r", "image": "ref@sha256:" + "a" * 64}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SuiteError, match="platform"):
        load_runtime_images(no_platform)


# --- the join -----------------------------------------------------------------


def _table(*rows) -> RuntimeImageTable:
    return RuntimeImageTable.model_validate({"schema_version": 1, "images": list(rows)})


def test_an_unmapped_pairing_is_refused_with_the_fix_named():
    problem = image_problem_for(
        _table(),
        suite_id="tbench-pilot",
        runtime=RuntimeDeclaration(key="dsh", needs=["node"]),
        image=None,
        platform=None,
        table_path=Path("/tmp/images.yaml"),
    )
    assert problem is not None
    assert "no sandbox image is mapped" in problem
    assert "/tmp/images.yaml" in problem and "--sandbox-image" in problem
    assert "node" in problem


def test_an_explicit_image_states_the_assumption_instead_of_hiding_it():
    assert image_problem_for(
        _table(),
        suite_id="tbench-pilot",
        runtime=RuntimeDeclaration(key="dsh"),
        image="ref@sha256:" + "b" * 64,
        platform="arm64",
    ) is None


def test_a_row_that_rides_the_task_image_requires_nothing():
    table = _table({"suite": "tbench-pilot", "runtime": "dsh", "image": None})
    assert image_problem_for(
        table,
        suite_id="tbench-pilot",
        runtime=RuntimeDeclaration(key="dsh"),
        image=None,
        platform=None,
    ) is None


def test_a_pinned_row_must_be_used_exactly():
    pinned = "ref@sha256:" + "c" * 64
    table = _table(
        {"suite": "tbench-pilot", "runtime": "dsh", "image": pinned, "platform": "arm64"}
    )
    runtime = RuntimeDeclaration(key="dsh")
    assert image_problem_for(
        table, suite_id="tbench-pilot", runtime=runtime, image=pinned, platform="arm64"
    ) is None
    mismatch = image_problem_for(
        table,
        suite_id="tbench-pilot",
        runtime=runtime,
        image="ref@sha256:" + "d" * 64,
        platform="arm64",
    )
    assert mismatch is not None and "must match" in mismatch
    wrong_platform = image_problem_for(
        table, suite_id="tbench-pilot", runtime=runtime, image=pinned, platform="x86"
    )
    assert wrong_platform is not None


# --- the run path uses both --------------------------------------------------


def test_the_run_path_collects_the_selected_runtime_key():
    from types import SimpleNamespace

    from aeval.cli import _check_runtime_images

    job = SimpleNamespace(
        agents=[SimpleNamespace(import_path="aeval.agents.dsh.agent:DshAgent")]
    )
    keys = _check_runtime_images(
        "tbench-pilot",
        job,
        agents_root=AGENTS,
        sandbox_image=None,
        sandbox_platform=None,
    )
    assert keys == ["dsh"]


def test_the_run_path_refuses_an_unmapped_pairing(tmp_path: Path):
    from types import SimpleNamespace

    from aeval.cli import _check_runtime_images

    empty = tmp_path / "empty.yaml"
    empty.write_text("schema_version: 1\nimages: []\n", encoding="utf-8")
    job = SimpleNamespace(
        agents=[SimpleNamespace(import_path="aeval.agents.dsh.agent:DshAgent")]
    )
    with pytest.raises(SuiteError, match="no sandbox image is mapped"):
        _check_runtime_images(
            "tbench-pilot",
            job,
            agents_root=AGENTS,
            sandbox_image=None,
            sandbox_platform=None,
            table_path=empty,
        )
    # an explicit image is the documented escape hatch
    assert _check_runtime_images(
        "tbench-pilot",
        job,
        agents_root=AGENTS,
        sandbox_image="ref@sha256:" + "e" * 64,
        sandbox_platform="arm64",
        table_path=empty,
    ) == ["dsh"]


def test_an_adapter_without_a_declaration_is_reported_not_silently_passed():
    from types import SimpleNamespace

    from aeval.cli import _selected_runtime_keys

    job = SimpleNamespace(agents=[SimpleNamespace(import_path="some.other:Agent")])
    found, unresolved = _selected_runtime_keys(job, agents_root=AGENTS)
    assert found == [] and unresolved == ["some.other:Agent"]
