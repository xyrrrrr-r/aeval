#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""tools —— 服务自查用例的执行脚本：直接探测被测引擎
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
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — 连不上等传输层失败
        return -1, "{}: {}".format(type(exc).__name__, exc)


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
    return {"X-Timestamp": ts, "X-Signature": digest}

# tools 类 6 用例：工具系统——注册列表、get_current_time、web_fetch、
# read_skill、sub_agent、并发安全。

import threading


def _registry_list():
    status, text = api("GET", "/tools")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /tools -> %d (want 200)" % status]
    names = set()
    for entry in (jget(text, "tools") or jget(text, "items") or []):
        if isinstance(entry, dict):
            names.add(str(entry.get("name") or entry.get("id")))
        else:
            names.add(str(entry))
    expected = {"get_current_time", "web_fetch", "read_skill", "sub_agent"}
    missing = expected - names
    if missing:
        return False, ["tool registry missing %r (has %r)" % (sorted(missing), sorted(names)[:12])]
    return True, ["registry lists all expected tools: %r" % sorted(expected)]


def _get_current_time():
    status, text = api(
        "POST", "/tools/call", {"name": "get_current_time", "arguments": {}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call get_current_time -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not re.search(r"\d{4}", body):
        return False, ["no plausible year in time output: %r" % body[:120]]
    return True, ["get_current_time returns a plausible timestamp: %r" % body[:60]]


def _web_fetch():
    # 抓取引擎自身的 /health：无外部网络依赖的连通性证明。
    status, text = api(
        "POST", "/tools/call",
        {"name": "web_fetch",
         "arguments": {"url": BASE + "/health"}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call web_fetch -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["web_fetch returned empty content"]
    return True, ["web_fetch fetched %s/health (%d chars)" % (BASE, len(body))]


def _read_skill():
    status, text = api(
        "POST", "/tools/call", {"name": "read_skill", "arguments": {}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call read_skill -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["read_skill returned empty content"]
    return True, ["read_skill answers with skill content (%d chars)" % len(body)]


def _sub_agent():
    status, text = api(
        "POST", "/tools/call",
        {"name": "sub_agent",
         "arguments": {"task": "报告 1+1 的结果"}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call sub_agent -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["sub_agent returned empty result"]
    return True, ["sub_agent answers with a result (%d chars)" % len(body)]


def _concurrent_safety():
    results = []

    def one(index):
        status, text = api(
            "POST", "/tools/call",
            {"name": "get_current_time", "arguments": {"label": index}})
        results.append((index, status))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    bad = [i for i, status in results if status != 200]
    if bad:
        return False, ["concurrent tool calls failed for indices %r" % bad]
    return True, ["5 concurrent tool calls all returned 200"]


CASES = {
    "tools.registry_list": (
        "工具注册列表", "GET /tools 列出预期注册的工具", _registry_list),
    "tools.get_current_time": (
        "get_current_time", "时间工具返回可信时间戳", _get_current_time),
    "tools.web_fetch": ("web_fetch", "抓取工具可用（引擎自身端点）", _web_fetch),
    "tools.read_skill": ("read_skill", "技能读取工具返回内容", _read_skill),
    "tools.sub_agent": ("sub_agent", "子代理工具返回执行结果", _sub_agent),
    "tools.concurrent_safety": (
        "并发安全", "5 路并发工具调用全部成功", _concurrent_safety),
}


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
        fh.write(reward + "\n")
    print("[sbench] reward=" + reward)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
