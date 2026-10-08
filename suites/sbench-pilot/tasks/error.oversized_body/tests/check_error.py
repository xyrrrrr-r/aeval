#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""error —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
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

# error 类 6 用例：异常处理——401 鉴权（无/错 token）、超大 body 防
# OOM、畸形 JSON 400、未知端点 404、不允许的方法 405。0.4.0 需求收
# 窄：HMAC 三用例（缺失/错误/重放）不再单独成例（task_center 的
# HMAC 执行检查仍在）。

def _no_auth():
    status, text = api("GET", "/plans", token="")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 401:
        return False, ["no-token request -> %d (want 401)" % status]
    return True, ["missing token rejected with 401"]


def _bad_auth():
    status, text = api("GET", "/plans", token="definitely-not-a-token")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 401:
        return False, ["garbage token -> %d (want 401)" % status]
    return True, ["invalid token rejected with 401"]


def _oversized_body():
    status, text = api(
        "POST", "/chat", "y" * (5 * 1024 * 1024), timeout=90.0, raw=True)
    if status < 0:
        return False, ["engine unreachable or timed out: " + text]
    if status >= 500:
        return False, ["5MB body -> %d (5xx: OOM risk)" % status]
    return True, ["5MB body handled with %d (no 5xx)" % status]


def _invalid_json():
    status, text = api("POST", "/chat", "{definitely-not-json", raw=True)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 400:
        return False, ["malformed JSON -> %d (want 400)" % status]
    return True, ["malformed JSON rejected with 400"]


def _unknown_endpoint():
    status, text = api("GET", "/no-such-endpoint-sbench")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 404:
        return False, ["unknown endpoint -> %d (want 404)" % status]
    return True, ["unknown endpoint returns 404"]


def _method_not_allowed():
    status, text = api("DELETE", "/health")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 405 and status != 404:
        return False, ["DELETE /health -> %d (want 405; 404 acceptable)" % status]
    return True, ["unsupported method rejected with %d" % status]


CASES = {
    "error.no_auth": ("无鉴权", "缺 token 的请求被拒为 401", _no_auth),
    "error.bad_auth": ("错误鉴权", "伪造 token 被拒为 401", _bad_auth),
    "error.oversized_body": (
        "超大 body", "5MB 请求体被优雅处理，无 5xx/OOM", _oversized_body),
    "error.invalid_json": (
        "畸形 JSON", "非法 JSON 请求体返回 400", _invalid_json),
    "error.unknown_endpoint": (
        "未知端点", "不存在的端点返回 404", _unknown_endpoint),
    "error.method_not_allowed": (
        "不允许的方法", "DELETE /health 返回 405（或 404）", _method_not_allowed),
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
