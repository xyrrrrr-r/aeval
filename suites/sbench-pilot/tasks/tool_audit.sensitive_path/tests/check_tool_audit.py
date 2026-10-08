#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""tool_audit —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
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

# tool_audit 类 9 用例：工具安全审计——schema 格式规范、必填字段、
# 敏感路径白名单、路径穿越拒绝、参数完整性、参数类型校验、未知工
# 具拒绝、调用留痕、审计查询。0.5.0 需求移出 SSRF 两个用例（内网
# 127.0.0.1 / 云元数据 169.254.169.254 防护）。


def _schema_format():
    status, text = api("GET", "/tools")
    if status != 200:
        return False, ["GET /tools -> %d (want 200)" % status]
    tools = [t for t in (jget(text, "tools") or jget(text, "items") or [])
             if isinstance(t, dict)]
    if not tools:
        return False, ["no structured tool entries"]
    for tool in tools:
        params = tool.get("parameters") or tool.get("input_schema")
        if params is None:
            return False, ["tool %r has no parameters schema" % tool.get("name")]
        if not isinstance(params, dict) or "properties" not in params:
            return False, [
                "tool %r schema is not JSON-Schema shaped" % tool.get("name")
            ]
    return True, ["every tool carries a JSON-Schema shaped parameters block"]


def _schema_required_fields():
    status, text = api("GET", "/tools")
    if status != 200:
        return False, ["GET /tools -> %d (want 200)" % status]
    tools = [t for t in (jget(text, "tools") or jget(text, "items") or [])
             if isinstance(t, dict)]
    with_params = [t for t in tools if isinstance(t.get("parameters"), dict)]
    if not with_params:
        return False, ["no tool with a parameters block"]
    for tool in with_params:
        params = tool["parameters"]
        if "required" not in params:
            # 无必填参数的工具可以省略 required；至少 properties 必须在。
            if "properties" not in params:
                return False, ["tool %r lacks properties" % tool.get("name")]
    return True, ["parameter schemas expose required/properties consistently"]


def _sensitive_path():
    status, text = api(
        "POST", "/tools/call",
        {"name": "parse_file_path", "arguments": {"path": "/etc/passwd"}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status == 200:
        body = str(jget(text, "result") or jget(text, "output") or text)
        if "refus" in body.lower() or "blocked" in body.lower() \
                or "denied" in body.lower() or "not allowed" in body.lower():
            return True, ["sensitive path answered with an explicit refusal"]
        return False, ["/etc/passwd accepted — path whitelist missing"]
    if status >= 500:
        return False, ["sensitive path -> %d (5xx)" % status]
    return True, ["sensitive path rejected with %d" % status]


def _path_traversal():
    status, text = api(
        "POST", "/tools/call",
        {"name": "parse_file_path",
         "arguments": {"path": "../../etc/shadow"}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status == 200:
        body = str(jget(text, "result") or jget(text, "output") or text)
        if "refus" in body.lower() or "blocked" in body.lower() \
                or "denied" in body.lower() or "traversal" in body.lower():
            return True, ["traversal answered with an explicit refusal"]
        return False, ["path traversal accepted — no traversal check"]
    if status >= 500:
        return False, ["traversal path -> %d (5xx)" % status]
    return True, ["path traversal rejected with %d" % status]


def _param_completeness():
    status, text = api(
        "POST", "/tools/call", {"name": "web_fetch", "arguments": {}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (400, 422):
        if status == 200:
            return False, ["web_fetch without url accepted — no completeness check"]
        return False, ["web_fetch without url -> %d (want 400/422)" % status]
    return True, ["missing required parameter rejected with %d" % status]


def _param_type_validation():
    status, text = api(
        "POST", "/tools/call",
        {"name": "web_fetch", "arguments": {"url": 12345}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (400, 422):
        if status == 200:
            return False, ["numeric url accepted — no type validation"]
        return False, ["numeric url -> %d (want 400/422)" % status]
    return True, ["wrong parameter type rejected with %d" % status]


def _unknown_tool():
    status, text = api(
        "POST", "/tools/call",
        {"name": "definitely_not_a_tool", "arguments": {}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (400, 404):
        return False, ["unknown tool -> %d (want 400/404)" % status]
    return True, ["unknown tool rejected with %d" % status]


def _call_logging():
    api("POST", "/tools/call", {"name": "get_current_time", "arguments": {}})
    status, text = api("GET", "/audit/tool_calls")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /audit/tool_calls -> %d (want 200)" % status]
    rows = jget(text, "calls") or jget(text, "items") or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["tool call log is empty after a call"]
    return True, ["tool calls are logged (%d entries)" % len(rows)]


def _audit_query():
    status, text = api("GET", "/audit/tool_calls?tool=get_current_time&limit=10")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["filtered audit query -> %d (want 200)" % status]
    rows = jget(text, "calls") or jget(text, "items") or jget(text, "data") or []
    if not isinstance(rows, list):
        return False, ["filtered audit payload not a list"]
    for row in rows:
        if isinstance(row, dict) and "get_current_time" not in str(row.get("tool") or row.get("name")):
            return False, ["filter ignored: %r" % row]
    return True, ["audit query supports filtering (returned %d rows)" % len(rows)]


CASES = {
    "tool_audit.schema_format": (
        "schema 格式规范", "每个工具带 JSON-Schema 形态的参数块", _schema_format),
    "tool_audit.schema_required_fields": (
        "schema 必填字段", "参数 schema 一致暴露 required/properties",
        _schema_required_fields),
    "tool_audit.sensitive_path": (
        "敏感路径白名单", "/etc/passwd 被拒", _sensitive_path),
    "tool_audit.path_traversal": (
        "路径穿越拒绝", "../../etc/shadow 被拒", _path_traversal),
    "tool_audit.param_completeness": (
        "参数完整性", "缺必填参数返回 400/422", _param_completeness),
    "tool_audit.param_type_validation": (
        "参数类型校验", "错误类型参数返回 400/422", _param_type_validation),
    "tool_audit.unknown_tool": (
        "未知工具拒绝", "调用不存在的工具返回 400/404", _unknown_tool),
    "tool_audit.tool_log_recording": (
        "调用留痕", "工具调用写入审计日志", _call_logging),
    "tool_audit.audit_query": (
        "审计查询", "审计日志支持过滤查询", _audit_query),
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
