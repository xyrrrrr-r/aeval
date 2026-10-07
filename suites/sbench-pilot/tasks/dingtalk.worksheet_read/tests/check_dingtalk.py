#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""dingtalk —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
执行脚本：直接探测被测引擎（ENGINE_BASE_URL）并发布
/logs/verifier/reward.txt（检查通过 = 1，失败 = 0；引擎不可达视为
失败，原因打印到 verifier 日志留痕）。"""

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

# dingtalk 类 5 用例：钉钉集成——工具注册、工作表读写（干运行）、消息
# 发送（干运行）、错误处理。干运行 = 不真正外发，部署侧以
# DINGTALK_DRY_RUN 标记。


def _tool_registry():
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
    dingtalk = {n for n in names if "dingtalk" in n.lower() or "worksheet" in n.lower()}
    if not dingtalk:
        return False, ["no dingtalk-ish tools registered: %r" % sorted(names)[:16]]
    return True, ["dingtalk tools registered: %r" % sorted(dingtalk)]


def _worksheet_read():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.worksheet.read",
         "arguments": {"worksheet_id": "sbench-sheet", "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["worksheet read (dry run) -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["worksheet read returned empty content"]
    return True, ["worksheet read (dry run) answered with content"]


def _worksheet_write():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.worksheet.write",
         "arguments": {"worksheet_id": "sbench-sheet", "values": {"k": "v"},
                       "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["worksheet write (dry run) -> %d (want 200)" % status]
    body = json.loads(text) if text.strip().startswith("{") else {}
    if body.get("dry_run") is False:
        return False, ["write executed for real despite dry_run=true"]
    return True, ["worksheet write honored dry_run"]


def _message_send_dryrun():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.send_message",
         "arguments": {"chat_id": "sbench-chat", "content": "干运行消息",
                       "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["send message (dry run) -> %d (want 200)" % status]
    body = json.loads(text) if text.strip().startswith("{") else {}
    if body.get("dry_run") is False or body.get("sent") is True:
        return False, ["message actually sent despite dry run"]
    return True, ["message send stayed a dry run"]


def _error_handling():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.worksheet.read",
         "arguments": {"worksheet_id": "", "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status >= 500:
        return False, ["bad worksheet id -> %d (5xx)" % status]
    return True, ["dingtalk bad input handled with %d (no 5xx)" % status]


CASES = {
    "dingtalk.tool_registry": (
        "工具注册", "钉钉族工具出现在注册表中", _tool_registry),
    "dingtalk.worksheet_read": (
        "工作表读取", "干运行下工作表可读", _worksheet_read),
    "dingtalk.worksheet_write": (
        "工作表写入", "干运行下写操作不落盘", _worksheet_write),
    "dingtalk.message_send_dryrun": (
        "消息发送干运行", "干运行下消息不真正外发", _message_send_dryrun),
    "dingtalk.error_handling": (
        "错误处理", "非法工作表 id 得到 4xx 而非 5xx", _error_handling),
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
