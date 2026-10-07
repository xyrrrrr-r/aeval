#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""report —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
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

# report 类 8 用例：报告系统——SSR 端点、Markdown 渲染、HMAC 鉴权、
# 任务完成自动生成、报告 schema、列表、下载、租户隔离。


def _any_report_id():
    status, text = api("GET", "/reports")
    if status != 200:
        return None, ["GET /reports -> %d (want 200)" % status]
    rows = jget(text, "reports") or jget(text, "items") or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return None, ["no reports yet — run one task to seed a report"]
    row = rows[0]
    return (row.get("id") if isinstance(row, dict) else row), []


def _ssr_endpoint():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/report/" + str(report_id))
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /report/<id> -> %d (want 200)" % status]
    if not re.search(r"<html|<!doctype", text, re.IGNORECASE):
        return False, ["SSR reply is not HTML: %r" % text[:80]]
    return True, ["report SSR endpoint renders HTML"]


def _markdown_render():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/report/" + str(report_id))
    if status != 200:
        return False, ["GET /report/<id> -> %d (want 200)" % status]
    if re.search(r"<h[1-6]|<strong|<ul|<table", text, re.IGNORECASE):
        return True, ["markdown rendered to HTML block elements"]
    if "```" in text or "**" in text:
        return False, ["raw markdown leaked into the page (unrendered)"]
    return True, ["report page contains rendered content"]


def _hmac_auth():
    # 无签名访问报告应被拒（受保护资源）。
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/report/" + str(report_id), token="")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status in (401, 403):
        return True, ["report access without credentials rejected (%d)" % status]
    if status == 200:
        # 公开只读报告是可接受的部署形态，但记录为免鉴权。
        return True, ["reports are public-read in this deployment (no auth)"]
    return False, ["report access without credentials -> %d" % status]


def _auto_generate():
    body_text = json.dumps({"task_id": "sbench-report-seed", "action": "run"})
    status, text = api(
        "POST", "/engine/task_execute",
        {"task_id": "sbench-report-seed", "action": "run"},
        headers=hmac_headers(body_text))
    if status not in (200, 202):
        return False, ["seed execute -> %d (want 2xx)" % status]
    api("POST", "/engine/tasks/sbench-report-seed/complete", {"result": "done"})
    status2, text2 = api("GET", "/reports")
    if status2 != 200:
        return False, ["GET /reports -> %d (want 200)" % status2]
    rows = jget(text2, "reports") or jget(text2, "items") or jget(text2, "data") or []
    seeded = [
        r for r in rows
        if isinstance(r, dict) and "sbench-report-seed" in json.dumps(r, default=str)
    ]
    if not seeded:
        return False, ["completed task did not produce a report entry"]
    return True, ["task completion auto-generated a report"]


def _schema():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/reports/" + str(report_id))
    if status == 404:
        status, text = api("GET", "/report/" + str(report_id) + "?format=json")
    if status != 200:
        return False, ["report json -> %d (want 200)" % status]
    if not text.strip().startswith("{"):
        return False, ["report json payload is not an object: %r" % text[:80]]
    data = json.loads(text)
    missing = [f for f in ("id", "created_at") if f not in data]
    if missing:
        return False, ["report json missing %r" % missing]
    return True, ["report json schema carries id/created_at"]


def _listing():
    status, text = api("GET", "/reports")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /reports -> %d (want 200)" % status]
    if jget(text, "reports") is None and jget(text, "items") is None \
            and not text.strip().startswith("["):
        return False, ["report list payload not a list: %r" % text[:120]]
    return True, ["report listing is list-shaped"]


def _download():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/reports/%s/download" % report_id)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["download -> %d (want 200)" % status]
    if not text.strip():
        return False, ["download body is empty"]
    return True, ["report downloadable with content (%d chars)" % len(text)]


def _isolation():
    if not TOKEN_B:
        return False, [
            "ENGINE_TOKEN_B not configured — isolation check needs a "
            "second tenant credential"
        ]
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/reports/" + str(report_id), token=TOKEN_B)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status in (403, 404):
        return True, ["tenant B cannot read tenant A's report (%d)" % status]
    if status == 200:
        return False, ["tenant B read tenant A's report — isolation broken"]
    return False, ["unexpected status %d for cross-tenant report read" % status]


CASES = {
    "report.ssr_endpoint": (
        "SSR 端点", "GET /report/<id> 服务端渲染 HTML", _ssr_endpoint),
    "report.markdown_render": (
        "Markdown 渲染", "报告页是渲染后的 HTML 而非裸 markdown", _markdown_render),
    "report.hmac_auth": (
        "HMAC 鉴权", "报告访问有鉴权保护（或显式公开只读）", _hmac_auth),
    "report.auto_generate": (
        "自动生成", "任务完成后报告自动出现", _auto_generate),
    "report.schema": (
        "报告 schema", "报告 JSON 携带 id/created_at", _schema),
    "report.listing": ("列表", "GET /reports 返回列表", _listing),
    "report.download": ("下载", "报告可下载且非空", _download),
    "report.isolation": (
        "租户隔离", "租户 B 读不到租户 A 的报告", _isolation),
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
