#!/usr/bin/env python3
"""共享服务用例库生成器：按类别把用例注入到消费套件。

这个用例库不属于任何单个评测集。terminal-bench、服务基准、会话
基准……都只是消费方：一个套件想用哪些类别，在自己的 ``cases.yaml``
里声明，本生成器据此把 ``tasks/<category>.<case>/`` 物化进该套件的
数据集目录（``datasets/local.yaml`` 的 ``path: tasks`` 注册约定不
变，Harbor 的任务发现平铺 ``iterdir()``，点分任务 id 正好匹配源方
案的 ``<category>.<case>`` 命名）。

    .venv/bin/python cases/generate.py --suite suites/sbench-pilot
    .venv/bin/python cases/generate.py --suite <dir> --check   # 漂移检测

所有权规则：套件 ``tasks/`` 下目录名以 ``<库类别>.`` 开头的任务归生
成器所有（重新注入时先删后写；反选类别后残留的也算漂移）。不带点
或点前缀不是库类别的任务（如 ``hello-world``、``memory.*``）是套
件自己的，生成器永不触碰。

改用例 = 改 ``cases/checkers/<category>.py`` 的 CASES 表后对消费套
件重新注入；任务 id 集合变更意味着数据集变更，消费套件应升版本号。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

LIBRARY = Path(__file__).resolve().parent
CHECKERS = LIBRARY / "checkers"

# 服务类别用例数（12 类共 89 例；会话契约类用例——智能度与记忆——锚定在
# 消费套件的判分器里，不属于本库。第三方集成类与流程编排类已按 0.2.0
# 需求移出评测范围，checker 一并删除；0.3.0 收窄 ddl 类，五张表与消息
# 顺序/双写检测不再单独成例；0.4.0 移出 error 类 HMAC 三用例；0.5.0 移出
# report 的 HMAC 鉴权、task_center 的 HMAC 执行与 tool_audit 的两个 SSRF
# 用例）。
EXPECTED_COUNTS = {
    "health": 3,
    "chat": 10,
    "session": 8,
    "tools": 6,
    "a2a": 8,
    "ddl": 8,
    "error": 6,
    "task_center": 11,
    "artifact": 5,
    "engine_lifecycle": 8,
    "tool_audit": 9,
    "report": 7,
}

# 默认采集链：与 dsh 会话记录配套的消费形态。评测集不同（agent 家
# 族不同）可在 cases.yaml 里用 collect_command 覆盖。
DEFAULT_COLLECT_COMMAND = (
    "mkdir -p /logs/verifier; bash /tests/test.sh; "
    "aeval-collect runtime_dump mock_call_log dsh_session canonical_transcript"
)

# --- 生成的执行脚本的共享运行时（头 + 尾） -------------------------------

HEADER = '''#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""{doc_title} —— 服务自查用例的执行脚本：直接探测被测引擎
（ENGINE_BASE_URL）并发布 /logs/verifier/reward.txt（检查通过 = 1，
失败 = 0；引擎不可达视为失败，原因打印到 verifier 日志留痕）。"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ENGINE_BASE_URL", "http://engine:8080").rstrip("/")
TOKEN = os.environ.get("ENGINE_TOKEN", "")
TOKEN_B = os.environ.get("ENGINE_TOKEN_B", "")
HMAC_SECRET = os.environ.get("ENGINE_HMAC_SECRET", "sbench-hmac-secret")
REWARD_PATH = "/logs/verifier/reward.txt"


def api(method, path, body=None, headers=None, token=None, timeout=30.0, raw=False):
    """对引擎发一次 HTTP 调用；返回 (status, text)，status < 0 表示连不上。

    raw=True 时 body 按原始字节发送（用于畸形 JSON 等负向用例）。
    """
    if raw:
        data = body.encode() if isinstance(body, str) else body
    else:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    bearer = TOKEN if token is None else token
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
    for key, value in (headers or {{}}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — 连不上等传输层失败
        return -1, "{{}}: {{}}".format(type(exc).__name__, exc)


def jget(text, *path):
    """从 JSON 回复里取嵌套字段；解析失败或路径不存在返回 None。"""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def hmac_headers(body_text):
    """对 body 计算约定形制的 HMAC-SHA256 签名头。"""
    ts = str(int(time.time()))
    digest = _hmac.new(
        HMAC_SECRET.encode(), (ts + "." + body_text).encode(), hashlib.sha256
    ).hexdigest()
    return {{"X-Timestamp": ts, "X-Signature": digest}}

'''

FOOTER = '''

def check(case_id):
    title, _doc, run = CASES[case_id]
    reasons = [case_id + " " + title]
    try:
        ok, detail = run()
    except Exception as exc:  # noqa: BLE001 — 用例脚本的任何异常都判失败
        return False, reasons + ["check raised {}: {}".format(type(exc).__name__, exc)]
    if not isinstance(detail, list):
        detail = [str(detail)]
    return bool(ok), reasons + detail


def main(argv):
    if len(argv) != 2 or argv[1] not in CASES:
        print("usage: check <case_id>; cases: " + ", ".join(sorted(CASES)))
        return 2
    ok, reasons = check(argv[1])
    for reason in reasons:
        print("[sbench] " + reason)
    reward = "1" if ok else "0"
    os.makedirs(os.path.dirname(REWARD_PATH), exist_ok=True)
    with open(REWARD_PATH, "w", encoding="utf-8") as fh:
        fh.write(reward + "\\n")
    print("[sbench] reward=" + reward)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''

TASK_TOML = '''schema_version = "1.4"

[verifier]
timeout_sec = 120.0

[[verifier.collect]]
# 用例执行脚本先跑并发布 /logs/verifier/reward.txt；采集链随后，
# 即使用例失败也照常密封证据。
command = "{collect_command}"

[environment]
# 服务自查需要访问被测引擎：部署侧把 "engine" 别名映射到真实服务，
# 必要时用 ENGINE_BASE_URL / ENGINE_TOKEN / ENGINE_HMAC_SECRET 覆盖。
network_mode = "allowlist"
allowed_hosts = ["engine"]

[metadata.provenance]
source = "authored-internally"
license = "MIT"
'''

DOCKERFILE = '''# ubuntu:24.04 pinned by its multi-arch manifest digest (same pin as the
# e2e-hello seed image). The engine under test is NOT in this image: the
# case script reaches it over the network as ENGINE_BASE_URL.
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# Baseline seed (suite base: baselines.ready): proves the seeded copy
# reached the sandbox before the agent starts.
RUN mkdir -p /workspace \\
    && printf 'true' > /workspace/ready
'''

INSTRUCTION = """（服务自查用例 · {category} 类）{title}

本用例检查引擎服务的「{category}」能力：{doc_line}。
本次检查内容：{description}。

被测对象是引擎服务（ENGINE_BASE_URL 指向的部署），不是你的行为。
验证阶段由本任务自带的执行脚本 tests/check_{category}.py 直接探测
引擎端点并发布 reward——检查通过写入 1，检查失败（含引擎不可达，
属于部署依赖缺失）写入 0，失败原因会留在 verifier 日志里。

你无需执行任何操作：请保持会话待命，等待验证阶段完成即可。
"""

# 每个类别「测什么」的一行说明（供 instruction 引用）。
_CATEGORY_LINES = {
    "health": "基础连通：服务存活、引擎就绪、记忆系统连通性诊断",
    "chat": "核心对话：流式/非流式、空消息、超长输入、特殊字符、SSE 事件格式",
    "session": "会话管理：自动创建、多轮复用、并发隔离、跨租户隔离、10 轮上下文保持",
    "tools": "工具系统：工具注册列表、get_current_time、web_fetch、read_skill、sub_agent、并发安全",
    "a2a": "A2A 协议：send/get/cancel task、鉴权、错误码、重复 task_id 处理",
    "ddl": "数据持久化：核心表数据正确性、字段类型、时间合理性、用量累加一致性、主键唯一性",
    "error": "异常处理：401 鉴权、超大 body 防 OOM、畸形 JSON 400、未知端点 404、不允许的方法 405",
    "task_center": "任务中心：定时/一次性/即时任务创建、查询/取消/禁用/启用/删除全生命周期",
    "artifact": "产出物：API 可达、字段完整性、env 正确性",
    "engine_lifecycle": "引擎生命周期：task_execute 接收、去重(202)、心跳精确更新、任务完成、状态机流转、失败重试字段",
    "tool_audit": "工具安全审计：schema 格式规范、敏感路径拒绝、参数完整性",
    "report": "报告系统：SSR 端点、Markdown 渲染、任务完成后自动生成报告",
}

# 库类别的默认大块（雷达轴）与红线类别。键 = 大块键，值 = 显示名。
_CATEGORY_BLOCKS = {
    "basic": "基础连通",
    "conv": "对话与会话",
    "orch": "编排与协作",
    "data": "数据与产出",
    "redline": "红线",
}
_CATEGORY_BLOCK_OF = {
    "health": "basic", "tools": "basic", "engine_lifecycle": "basic",
    "chat": "conv", "session": "conv",
    "a2a": "orch", "task_center": "orch",
    "ddl": "data", "artifact": "data", "report": "data",
    "error": "redline", "tool_audit": "redline",
}
_REDLINE_CATEGORIES = {"error", "tool_audit"}


def _load_bodies(categories):
    """读取选中类别主体，exec 拼好的完整脚本以拿到 CASES 表。"""
    bodies = {}
    for category in categories:
        path = CHECKERS / (category + ".py")
        if not path.is_file():
            raise SystemExit(f"unknown category {category!r}: no {path}")
        body = path.read_text(encoding="utf-8")
        composed = HEADER.format(doc_title=category) + body + FOOTER
        namespace: dict = {}
        exec(compile(composed, f"<{category}>", "exec"), namespace)  # noqa: S102
        cases = namespace.get("CASES")
        if not isinstance(cases, dict) or not cases:
            raise SystemExit(f"{path}: no CASES table")
        expected = EXPECTED_COUNTS[category]
        if len(cases) != expected:
            raise SystemExit(
                f"{category}: {len(cases)} cases, source plan says {expected}"
            )
        for case_id, value in cases.items():
            if not case_id.startswith(category + "."):
                raise SystemExit(f"{path}: case id {case_id!r} must start with {category!r}")
            if not isinstance(value, tuple) or len(value) != 3:
                raise SystemExit(f"{path}: CASES[{case_id!r}] must be (title, doc, fn)")
        bodies[category] = (composed, cases)
    return bodies


def _load_manifest(suite_dir: Path):
    """消费套件的 cases.yaml：声明要注入的类别（+ 可选覆盖）。"""
    manifest_path = suite_dir / "cases.yaml"
    if not manifest_path.is_file():
        raise SystemExit(
            f"{suite_dir} has no cases.yaml — a suite consumes the shared "
            "case library by declaring categories there"
        )
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    categories = data.get("categories")
    if not isinstance(categories, list) or not categories:
        raise SystemExit(f"{manifest_path}: categories must be a non-empty list")
    unknown = [c for c in categories if c not in EXPECTED_COUNTS]
    if unknown:
        raise SystemExit(
            f"{manifest_path}: unknown categories {unknown}; "
            f"library has {sorted(EXPECTED_COUNTS)}"
        )
    if len(set(categories)) != len(categories):
        raise SystemExit(f"{manifest_path}: duplicate categories")
    overrides = data.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise SystemExit(f"{manifest_path}: overrides must be a mapping")
    unknown_keys = set(overrides) - {"collect_command"}
    if unknown_keys:
        raise SystemExit(f"{manifest_path}: unknown override keys {sorted(unknown_keys)}")
    return categories, overrides


_RETIRED_CATEGORIES = {"plan", "dingtalk"}


def _is_library_owned(name: str) -> bool:
    """任务目录名是否归本库所有：``<库类别>.<case>`` 形态。反选类别后
    的残留同样归库所有（要清理/报告），套件自有任务（无点前缀或前缀
    不是库类别，如 hello-world、memory.*）永不触碰。退役类别（已从
    库中删除的 checker）也归库所有——否则其残留目录会逃过清理。"""
    prefixes = set(EXPECTED_COUNTS) | _RETIRED_CATEGORIES
    return any(name.startswith(category + ".") for category in prefixes)


def _owned_task_dirs(tasks_root: Path):
    """tasks/ 下归生成器所有的任务目录。"""
    if not tasks_root.is_dir():
        return []
    return [
        path for path in sorted(tasks_root.iterdir())
        if path.is_dir() and _is_library_owned(path.name)
    ]


def _materialize(suite_dir: Path, categories, overrides) -> int:
    """按类别注入：清掉本套件里这些类别的旧任务，物化新任务树。"""
    tasks_root = suite_dir / "tasks"
    tasks_root.mkdir(parents=True, exist_ok=True)
    collect_command = overrides.get("collect_command", DEFAULT_COLLECT_COMMAND)
    task_toml = TASK_TOML.format(collect_command=collect_command)
    bodies = _load_bodies(categories)

    for stale in _owned_task_dirs(tasks_root):
        shutil.rmtree(stale)

    total = 0
    titles: dict[str, str] = {}
    for category in categories:
        composed, cases = bodies[category]
        for case_id, (title, description, _fn) in cases.items():
            task_dir = tasks_root / case_id
            (task_dir / "tests").mkdir(parents=True)
            (task_dir / "environment").mkdir(parents=True)
            (task_dir / "instruction.md").write_text(
                INSTRUCTION.format(
                    category=category, title=title, description=description,
                    doc_line=_CATEGORY_LINES[category],
                ),
                encoding="utf-8",
            )
            (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")
            (task_dir / "environment" / "Dockerfile").write_text(
                DOCKERFILE, encoding="utf-8"
            )
            test_sh = task_dir / "tests" / "test.sh"
            test_sh.write_text(
                "#!/bin/sh\n"
                f"# {case_id} {title} — injected by aeval/cases/generate.py.\n"
                "set -u\n"
                "mkdir -p /logs/verifier\n"
                f"exec python3 /tests/check_{category}.py {case_id}\n",
                encoding="utf-8",
            )
            test_sh.chmod(0o755)
            checker = task_dir / "tests" / f"check_{category}.py"
            checker.write_text(composed, encoding="utf-8")
            checker.chmod(0o755)
            titles[case_id] = title
            total += 1
        print(f"{suite_dir.name}: {category:16s} {len(cases):3d} cases")
    # 类别声明 = 分组式指标族（v5）：每类一个 families/<key>.yaml，
    # 族只分组（metrics 为空），判定由官方基线给出（engine.impl == 基线的
    # impl）；显示名/大块/权重/阈值/红线与逐任务标题都在族声明里，不再有
    # task_categories/task_titles 侧车文件。
    _write_families(suite_dir, categories, titles)
    return total


_LIBRARY_BASELINE_FALLBACK = "graders/sbench_outcome.py@v1"


def _baseline_impl(suite_dir: Path) -> str:
    """消费套件的官方基线 impl（分组族的 engine 必须与它相同）。

    注入进一个还没有 suite.yaml 的新套件时用库的默认基线。
    """
    suite_yaml = suite_dir / "suite.yaml"
    if suite_yaml.is_file():
        try:
            data = yaml.safe_load(suite_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        impl = (
            ((data.get("verdict") or {}).get("graders") or {})
            .get("default", {})
            .get("impl")
        )
        if isinstance(impl, str) and impl:
            return impl
    return _LIBRARY_BASELINE_FALLBACK


def _family_body(category: str, titles: dict[str, str], baseline_impl: str) -> str:
    model = _category_model(category)
    members = {
        task_id: {"title": title, "role": "home"}
        for task_id, title in sorted(titles.items())
        if task_id.split(".")[0] == category
    }
    body = {
        "name": model["name"],
        "block": model["block"],
        "weight": model["weight"],
        "threshold": model["threshold"],
        "redline": model["redline"],
        "engine": {"impl": baseline_impl, "layer": "outcome", "veto": False},
        "metrics": [],
        "scope": members,
    }
    return (
        "# 由 aeval/cases/generate.py 生成 —— 分组式指标族（v5）：族只分组，\n"
        "# 判定由官方基线给出（engine.impl == verdict.graders.default.impl）；\n"
        "# 评分参数（显示名/大块/权重/阈值/红线）与逐任务标题都在这里声明。\n"
        + yaml.safe_dump(body, allow_unicode=True, sort_keys=False)
    )


def _category_model(category: str) -> dict:
    """库属类别的评分参数（红线 = 阈值 1.0、权重翻倍）。"""
    return {
        "name": _CATEGORY_LINES[category].split("：", 1)[0],
        "block": _CATEGORY_BLOCK_OF[category],
        "weight": 2.0 if category in _REDLINE_CATEGORIES else 1.0,
        "threshold": 1.0 if category in _REDLINE_CATEGORIES else 0.9,
        "redline": category in _REDLINE_CATEGORIES,
    }


def _write_families(suite_dir: Path, categories, titles: dict[str, str]) -> None:
    """写选中类别的族声明；清掉反选/退役类别的族文件；同步激活清单。"""
    baseline_impl = _baseline_impl(suite_dir)
    families_dir = suite_dir / "families"
    families_dir.mkdir(exist_ok=True)
    owned = set(EXPECTED_COUNTS) | _RETIRED_CATEGORIES
    for stale in sorted(families_dir.glob("*.yaml")):
        if stale.stem in owned and stale.stem not in categories:
            stale.unlink()
    for category in categories:
        (families_dir / f"{category}.yaml").write_text(
            _family_body(category, titles, baseline_impl), encoding="utf-8"
        )
    _sync_activation(suite_dir, categories)
    _sync_blocks(suite_dir, categories)


_ACTIVATION_HEADER = "families:"
_BLOCKS_HEADER = "blocks:"


def _activation_lines(categories) -> list[str]:
    return [f"{_ACTIVATION_HEADER}\n"] + [
        f"  - families/{category}.yaml\n" for category in categories
    ]


def _block_lines(categories) -> list[str]:
    """选中类别用到的大块显示名（v5：块名随族声明回到套件里）。

    大块是雷达轴/维度表的显示维度，它的显示名本来住在被 v5 退役的
    ``task_categories*.yaml`` 里；族声明只带大块**键**，显示名必须有
    个家——就在 ``suite.yaml`` 的 ``blocks:``。少了它，报告和面板会
    退回原始键（``basic``/``core``）。
    """
    keys = sorted({_CATEGORY_BLOCK_OF[category] for category in categories})
    return [f"{_BLOCKS_HEADER}\n"] + [
        f"  {key}: {_CATEGORY_BLOCKS[key]}\n" for key in keys
    ]


def _sync_activation(suite_dir: Path, categories) -> None:
    """把套件的 families 激活清单同步到选中类别（行级编辑，
    保留套件自写的注释与格式）。"""
    suite_yaml = suite_dir / "suite.yaml"
    if not suite_yaml.is_file():
        return
    lines = suite_yaml.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == _ACTIVATION_HEADER),
        None,
    )
    desired = _activation_lines(categories)
    if start is None:
        insert = next(
            (i for i, line in enumerate(lines) if line.startswith("verdict:")),
            len(lines),
        )
        suite_yaml.write_text(
            "".join(lines[:insert] + desired + lines[insert:]), encoding="utf-8"
        )
        return
    end = start + 1
    while end < len(lines) and lines[end].lstrip().startswith("- "):
        end += 1
    suite_yaml.write_text(
        "".join(lines[:start] + desired + lines[end:]), encoding="utf-8"
    )


def _indented_block_end(lines, start: int) -> int:
    """``start`` 是一个顶层键行：吃掉它下面所有缩进行。"""
    end = start + 1
    while end < len(lines) and lines[end][:1] in (" ", "\t", "\n"):
        end += 1
    return end


def _sync_blocks(suite_dir: Path, categories) -> None:
    """把大块显示名同步进 suite.yaml（行级编辑，保留注释与其余格式）。

    位置紧跟在 ``families:`` 激活清单之后（与手写套件一致）；没有该
    段时插在 ``verdict:`` 之前。
    """
    suite_yaml = suite_dir / "suite.yaml"
    if not suite_yaml.is_file():
        return
    text = suite_yaml.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    desired = _block_lines(categories)
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == _BLOCKS_HEADER),
        None,
    )
    if start is not None:
        end = _indented_block_end(lines, start)
    else:
        anchor = next(
            (i for i, line in enumerate(lines) if line.strip() == _ACTIVATION_HEADER),
            None,
        )
        if anchor is None:
            end = start = next(
                (i for i, line in enumerate(lines) if line.startswith("verdict:")),
                len(lines),
            )
        else:
            end = start = _indented_block_end(lines, anchor)
    fresh = "".join(lines[:start] + desired + lines[end:])
    if fresh != text:
        suite_yaml.write_text(fresh, encoding="utf-8")


def _section_lines(text: str, header: str) -> list[str]:
    """取顶层键段（键行 + 其缩进条目），去掉空行与换行符后比较。"""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return []
    end = _indented_block_end(lines, start)
    return [line.strip() for line in lines[start:end] if line.strip()]


def _check(suite_dir: Path, categories, overrides) -> int:
    """漂移检测：临时目录注入一遍，与套件 tasks/ 的库属任务比对。"""
    with tempfile.TemporaryDirectory(prefix="cases-check-") as tmp:
        fresh = Path(tmp) / "suite"
        fresh.mkdir()
        _materialize(fresh, categories, overrides)
        drift = []
        live_root = suite_dir / "tasks"
        fresh_root = fresh / "tasks"
        for path in sorted(fresh_root.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(fresh_root)
            live = live_root / rel
            if not live.is_file():
                drift.append(f"missing from tasks/: {rel}")
            elif live.read_text(encoding="utf-8") != path.read_text(encoding="utf-8"):
                drift.append(f"differs from injected: {rel}")
        for path in sorted(live_root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(live_root)
            top_dir = live_root / rel.parts[0]
            if not (fresh_root / rel).is_file() and top_dir.is_dir() \
                    and _is_library_owned(rel.parts[0]):
                # 选中类别的缺失/漂移已在上一循环报过；这里抓的是反选
                # 类别的残留——Harbor 仍会发现它们，必须显式清理。
                drift.append(f"stale injected task: {rel}")
        # 族声明与激活清单同样纳入漂移检测：整文件比对（反选类别的残留
        # 条目会以文件 diff / 缺行 / 多行的形式暴露）。
        for category in categories:
            rel = f"families/{category}.yaml"
            live_family = suite_dir / rel
            # 与"新鲜注入"的字节比对（等价于重新生成，且不依赖外部状态）
            want = (fresh / rel).read_text(encoding="utf-8")
            if not live_family.is_file():
                drift.append(f"missing from suite root: {rel}")
            elif live_family.read_text(encoding="utf-8") != want:
                drift.append(f"differs from injected: {rel}")
        owned = set(EXPECTED_COUNTS) | _RETIRED_CATEGORIES
        for stale in sorted((suite_dir / "families").glob("*.yaml")):
            if stale.stem in owned and stale.stem not in categories:
                drift.append(f"stale family declaration: {stale.name}")
        live_yaml = suite_dir / "suite.yaml"
        activation = [
            line.strip() for line in live_yaml.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- families/")
        ]
        want_activation = [
            line.strip() for line in _activation_lines(categories)[1:]
        ]
        if activation != want_activation:
            drift.append(
                "suite.yaml families list differs from injected: "
                f"{activation} != {want_activation}"
            )
        live_blocks = _section_lines(
            live_yaml.read_text(encoding="utf-8"), _BLOCKS_HEADER
        )
        want_blocks = [line.strip() for line in _block_lines(categories) if line.strip()]
        if live_blocks != want_blocks:
            drift.append(
                "suite.yaml blocks differ from injected: "
                f"{live_blocks} != {want_blocks}"
            )
        if drift:
            for line in drift[:20]:
                print("drift: " + line)
            print(f"({len(drift)} drift item(s); rerun without --check to re-inject)")
            return 1
    print(f"no drift: {suite_dir.name}/tasks matches the injected categories")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite", action="append", required=True, dest="suites",
        help="consuming suite directory (repeatable)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="verify the suite's tasks/ matches the injected categories",
    )
    args = parser.parse_args()

    for suite in args.suites:
        suite_dir = Path(suite).resolve()
        categories, overrides = _load_manifest(suite_dir)
        if args.check:
            status = _check(suite_dir, categories, overrides)
            if status:
                return status
            continue
        total = _materialize(suite_dir, categories, overrides)
        print(f"{suite_dir.name}: {total} tasks injected under tasks/")
        if total != sum(EXPECTED_COUNTS[c] for c in categories):
            raise SystemExit("inventory mismatch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
