#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""artifact —— 服务自查用例的执行脚本：直接探测被测引擎
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

# artifact 类 5 用例：产出物——API 可达、字段完整性、env 正确性、列表、
# 持久化。


def _api_reachable():
    status, text = api("GET", "/artifacts")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    return True, ["artifact listing endpoint reachable"]


def _field_completeness():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    rows = jget(text, "artifacts") or jget(text, "items") \
        or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no artifact rows to inspect (seed one task run first)"]
    row = rows[0] if isinstance(rows[0], dict) else {}
    missing = [f for f in ("id", "type", "created_at") if f not in row]
    if missing:
        return False, ["artifact row missing fields %r: %r" % (missing, row)]
    return True, ["artifact rows carry id/type/created_at"]


def _env_correctness():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    rows = jget(text, "artifacts") or jget(text, "items") \
        or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no artifact rows to inspect"]
    row = rows[0] if isinstance(rows[0], dict) else {}
    env = row.get("env") or row.get("environment")
    if env is None:
        return False, ["artifact carries no env field: %r" % row]
    return True, ["artifact records its originating env (%r)" % str(env)[:60]]


def _listing():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    if jget(text, "artifacts") is None and jget(text, "items") is None \
            and not text.strip().startswith("["):
        return False, ["artifact payload not a list: %r" % text[:120]]
    return True, ["artifact listing is list-shaped"]


def _persistence():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    rows = jget(text, "artifacts") or jget(text, "items") \
        or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no artifact rows to re-read"]
    row = rows[0]
    artifact_id = row.get("id") if isinstance(row, dict) else row
    if not artifact_id:
        return False, ["artifact row carries no id"]
    status2, text2 = api("GET", "/artifacts/" + str(artifact_id))
    if status2 != 200:
        return False, ["GET /artifacts/<id> -> %d (want 200)" % status2]
    digest_a = row.get("sha256") or row.get("digest") if isinstance(row, dict) else None
    digest_b = jget(text2, "sha256") or jget(text2, "digest")
    if digest_a and digest_b and digest_a != digest_b:
        return False, ["artifact digest changed between reads"]
    return True, ["artifact re-readable with stable content"]


CASES = {
    "artifact.api_reachable": (
        "API 可达", "GET /artifacts 返回 200", _api_reachable),
    "artifact.field_completeness": (
        "字段完整性", "产出物行携带 id/type/created_at", _field_completeness),
    "artifact.env_correctness": (
        "env 正确性", "产出物记录其来源环境", _env_correctness),
    "artifact.listing": ("列表", "产出物列表为列表结构", _listing),
    "artifact.persistence": (
        "持久化", "产出物可重读且内容稳定", _persistence),
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
