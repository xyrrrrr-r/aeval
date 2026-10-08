#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""health —— 服务自查用例的执行脚本：直接探测被测引擎
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

# health 类 3 用例：服务存活、引擎就绪、记忆系统连通性诊断。
# （health.sam_memory_diagnosis 的任务 id 保持不变——id 是稳定锚点，
# 0.4.1 只改显示名：SAM 记忆诊断 → 记忆系统连通性诊断。）


def _service_alive():
    status, text = api("GET", "/health")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /health -> %d (want 200)" % status]
    if not re.search(r'"status"\s*:\s*"?(ok|healthy|up)"?', text):
        return False, ["health body missing ok status: %r" % text[:120]]
    return True, ["GET /health -> 200, status ok"]


def _engine_ready():
    status, text = api("GET", "/health/ready")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /health/ready -> %d (want 200)" % status]
    ready = jget(text, "ready")
    if ready is not True and str(ready).lower() not in ("true", "yes", "1"):
        return False, ["ready flag not true: %r" % text[:120]]
    return True, ["engine reports ready"]


def _sam_memory_diagnosis():
    status, text = api("GET", "/health/memory")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /health/memory -> %d (want 200)" % status]
    if jget(text, "status") is None:
        return False, ["memory diagnosis missing status field: %r" % text[:120]]
    return True, ["SAM memory diagnosis reachable, status present"]


CASES = {
    "health.service_alive": (
        "服务存活", "GET /health 返回 200 且状态字段为 ok", _service_alive),
    "health.engine_ready": (
        "引擎就绪", "GET /health/ready 返回 200 且 ready 为真", _engine_ready),
    "health.sam_memory_diagnosis": (
        "记忆系统连通性诊断", "GET /health/memory 返回 200 且携带诊断状态",
        _sam_memory_diagnosis),
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
