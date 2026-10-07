"""Offline validation of the sbench-pilot suite (service benchmark).

Everything checkable without a deployed engine: suite/job composition
with the full 123-task dataset registered via ``datasets/local.yaml``,
the outcome-only verdict contract, the per-category generated execution
scripts (compile + case dispatch + reward semantics), and the inventory
counts against the source plan (14 service categories, 123 cases).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aeval.suite_loader.composition import compose_harbor_job
from aeval.suite_loader.loader import load_suite

SUITE = Path(__file__).parents[2] / "suites" / "sbench-pilot"
REPO = Path(__file__).parents[2]

# 源方案《Benchmark 测评指标设计方案》§2 的 14 个服务类用例数。
EXPECTED_COUNTS = {
    "health": 3,
    "chat": 10,
    "session": 8,
    "tools": 6,
    "plan": 15,
    "a2a": 8,
    "ddl": 15,
    "error": 9,
    "dingtalk": 5,
    "task_center": 12,
    "artifact": 5,
    "engine_lifecycle": 8,
    "tool_audit": 11,
    "report": 8,
}


@pytest.fixture(scope="module")
def suite():
    return load_suite(SUITE)


@pytest.fixture(scope="module")
def job(suite):
    return compose_harbor_job(suite)


def test_suite_identity_and_outcome_only_contract(suite):
    assert suite.id == "sbench-pilot"
    assert suite.overlay.harbor.dataset == "datasets/local.yaml"
    assert suite.overlay.harbor.job == "jobs/sbench-smoke.yaml"
    # 服务自查：唯一判分层是 outcome（② 道）——闲置 agent 的轨迹不是
    # 信号，且 cannot_judge 会毒化最终判定，所以不设轨迹层。
    graders = suite.overlay.verdict.graders
    assert set(graders) == {"default"}
    assert graders["default"].layer == "outcome"
    assert graders["default"].impl == "graders/sbench_outcome.py@v1"
    assert [obs.name for obs in suite.overlay.observables] == ["ready", "reward"]
    assert [(m.id, m.kind, m.k) for m in suite.overlay.metrics] == [
        ("reliability", "pass_pow_k", 1)
    ]


def test_job_composes_with_all_123_tasks(job):
    assert len(job.tasks) == 123
    assert job.n_attempts == 1


def test_task_tree_matches_the_source_inventory():
    tasks = SUITE / "tasks"
    names = sorted(p.name for p in tasks.iterdir() if (p / "task.toml").is_file())
    assert len(names) == 123
    per_category: dict[str, int] = {}
    for name in names:
        category, _, case = name.partition(".")
        assert case, f"task id {name!r} is not <category>.<case> shaped"
        assert category in EXPECTED_COUNTS, f"unknown category {category!r}"
        per_category[category] = per_category.get(category, 0) + 1
    assert per_category == EXPECTED_COUNTS


def test_every_task_carries_the_five_file_skeleton():
    for task_dir in sorted(p for p in (SUITE / "tasks").iterdir() if p.is_dir()):
        for rel in (
            "instruction.md",
            "task.toml",
            "environment/Dockerfile",
            "tests/test.sh",
        ):
            assert (task_dir / rel).is_file(), f"{task_dir.name}/{rel} missing"
        checkers = list((task_dir / "tests").glob("check_*.py"))
        assert len(checkers) == 1, f"{task_dir.name}: expected exactly one checker"
        assert checkers[0].stem == "check_" + task_dir.name.partition(".")[0]


def test_generated_execution_scripts_compile_and_dispatch():
    """按类别生成的执行脚本：语法有效（compile 不落 pyc，任务树保持
    纯净），且每个任务的 test.sh 以自己的 case id 调用其类别脚本。"""
    for task_dir in sorted(p for p in (SUITE / "tasks").iterdir() if p.is_dir()):
        checker = next((task_dir / "tests").glob("check_*.py"))
        compile(checker.read_text("utf-8"), str(checker), "exec")
        test_sh = (task_dir / "tests" / "test.sh").read_text("utf-8")
        category = task_dir.name.partition(".")[0]
        assert (
            f"exec python3 /tests/check_{category}.py {task_dir.name}" in test_sh
        ), f"{task_dir.name}: test.sh does not dispatch its own case id"


def test_checkers_claim_the_full_category_inventory():
    """任务树不得偏离注入清单：--check 模式在临时目录里注入一遍并与
    tasks/ 的库属任务逐文件比对（不写仓库树）。"""
    import sys as _sys

    gen = REPO / "cases" / "generate.py"
    result = subprocess.run(
        [_sys.executable, str(gen), "--suite", str(SUITE), "--check"],
        capture_output=True, text=True, check=True,
    )
    assert "no drift: sbench-pilot/tasks matches the injected categories" in result.stdout


def test_the_library_injects_by_category_into_another_suite(tmp_path):
    """跨评测集可用性：用例库不属于 sbench-pilot。第二个套件只声明
    [health, error] 两个类别，注入后得到且仅得到这两类任务；反选类别
    的残留（模拟此前全量注入过的 chat.*）被清掉；套件可加载、可组
    合——注册机制对任何消费方一视同仁。"""
    import shutil as _shutil
    import sys as _sys

    other = tmp_path / "sbench-subset"
    other.mkdir()
    for rel in ("datasets", "jobs", "graders"):
        _shutil.copytree(SUITE / rel, other / rel)
    suite_yaml = (SUITE / "suite.yaml").read_text("utf-8")
    (other / "suite.yaml").write_text(
        suite_yaml.replace("id: sbench-pilot", "id: sbench-subset"),
        encoding="utf-8",
    )
    (other / "cases.yaml").write_text(
        "categories: [health, error]\n", encoding="utf-8"
    )
    # 模拟反选残留：此前注入过的 chat 类任务还在树里。
    stale = other / "tasks" / "chat.basic"
    stale.mkdir(parents=True)
    (stale / "task.toml").write_text("schema_version = \"1.4\"\n", encoding="utf-8")

    gen = REPO / "cases" / "generate.py"
    result = subprocess.run(
        [_sys.executable, str(gen), "--suite", str(other)],
        capture_output=True, text=True, check=True,
    )
    assert "12 tasks injected under tasks/" in result.stdout

    names = sorted(p.name for p in (other / "tasks").iterdir() if p.is_dir())
    assert len(names) == 12
    assert {n.partition(".")[0] for n in names} == {"health", "error"}
    assert not (other / "tasks" / "chat.basic").exists()  # 反选残留被清理

    # 显示名/类别声明文件也按类别注入：只含选中类别的条目。
    import yaml as _yaml

    titles = _yaml.safe_load((other / "task_titles.cases.yaml").read_text("utf-8"))
    assert set(titles) == set(names)
    assert all(isinstance(title, str) and title for title in titles.values())
    categories = _yaml.safe_load(
        (other / "task_categories.cases.yaml").read_text("utf-8")
    )["categories"]
    assert set(categories) == {"health", "error"}

    from aeval.suite_loader.composition import compose_harbor_job
    from aeval.suite_loader.loader import load_suite

    suite = load_suite(other, suites_root=SUITE.parent)
    assert suite.id == "sbench-subset"
    assert suite.task_titles == titles  # 加载器合并生成文件后可见
    job = compose_harbor_job(suite)
    assert len(job.tasks) == 12


def test_outcome_grader_identity_matches_the_declaration(suite):
    from aeval.verdict.pipeline import load_suite_graders

    graders = {g.grader.id: g for g in load_suite_graders(suite)}
    assert set(graders) == {"sbench-outcome"}
    assert graders["sbench-outcome"].grader.version == "v1"
    assert graders["sbench-outcome"].grader.layer == "outcome"
    assert graders["sbench-outcome"].veto is False
