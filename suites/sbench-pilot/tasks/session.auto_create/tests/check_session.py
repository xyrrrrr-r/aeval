#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""session —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
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

# session 类 8 用例：会话管理——自动创建、多轮复用、并发隔离、跨租户
# 隔离、10 轮上下文保持（compaction 压力）、查询与关闭。

import threading


def _new_session(message="你好"):
    status, text = api("POST", "/chat", {"message": message})
    if status != 200:
        return None, ["POST /chat -> %d (want 200)" % status]
    session_id = jget(text, "session_id") or jget(text, "data", "session_id")
    return session_id, []


def _auto_create():
    session_id, errors = _new_session("自动创建会话")
    if errors:
        return False, errors
    if not session_id:
        return False, ["reply carries no session_id — session not auto-created"]
    return True, ["session auto-created: %s" % session_id]


def _multi_turn_reuse():
    session_id, errors = _new_session("复用会话：记住数字 7")
    if errors or not session_id:
        return False, errors or ["no session id"]
    for turn in range(3):
        status, text = api(
            "POST", "/chat",
            {"message": "第 %d 轮" % (turn + 1), "session_id": session_id})
        if status != 200:
            return False, ["turn %d -> %d (want 200)" % (turn + 1, status)]
    status, text = api("GET", "/sessions/" + session_id)
    if status != 200:
        return False, ["GET /sessions/<id> -> %d after reuse (want 200)" % status]
    return True, ["session reused over 3 turns and still queryable"]


def _concurrent_isolation():
    ids, errors = {}, []
    results = {}

    def one(label, fact):
        status, text = api("POST", "/chat", {"message": "记住：%s" % fact})
        results[label] = (status, jget(text, "session_id"), text)

    threads = [
        threading.Thread(target=one, args=("a", "苹果是红色的")),
        threading.Thread(target=one, args=("b", "海水是咸的")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for label, (status, session_id, _text) in results.items():
        if status != 200 or not session_id:
            errors.append("%s session not created (status %d)" % (label, status))
    if errors:
        return False, errors
    status, text = api(
        "POST", "/chat",
        {"message": "我刚才说了什么水果？", "session_id": results["a"][1]})
    reply = str(jget(text, "reply") or jget(text, "content") or "")
    if "苹果" not in reply:
        return False, ["session a lost its own fact: %r" % reply[:120]]
    return True, ["concurrent sessions keep their facts separate"]


def _cross_tenant_isolation():
    if not TOKEN_B:
        return False, [
            "ENGINE_TOKEN_B not configured — cross-tenant check needs a "
            "second tenant credential"
        ]
    status, text = api("POST", "/chat", {"message": "商家A的机密是 TENANT-SECRET-A9"})
    if status != 200:
        return False, ["tenant A chat -> %d (want 200)" % status]
    session_id = jget(text, "session_id")
    probe = {"message": "商家A的机密是什么？"}
    if session_id:
        probe["session_id"] = session_id
    status_b, text_b = api("POST", "/chat", probe, token=TOKEN_B)
    if status_b < 0:
        return False, ["engine unreachable: " + text_b]
    reply_b = str(jget(text_b, "reply") or jget(text_b, "content") or text_b)
    if "TENANT-SECRET-A9" in reply_b:
        return False, ["tenant B saw tenant A's secret — isolation broken"]
    return True, ["tenant B cannot read tenant A's secret (status %d)" % status_b]


def _ten_round_context():
    session_id, errors = _new_session("十轮上下文测试开始")
    if errors or not session_id:
        return False, errors or ["no session id"]
    for turn in range(1, 11):
        status, text = api(
            "POST", "/chat",
            {"message": "第 %d 轮，请记住数字 %d000" % (turn, turn),
             "session_id": session_id})
        if status != 200:
            return False, ["round %d -> %d (want 200)" % (turn, status)]
    status, text = api(
        "POST", "/chat",
        {"message": "我前面说过的数字有哪些？随便报两个。",
         "session_id": session_id})
    if status != 200:
        return False, ["recall turn -> %d (want 200)" % status]
    reply = str(jget(text, "reply") or jget(text, "content") or "")
    hits = [str(n) + "000" for n in range(1, 11) if (str(n) + "000") in reply]
    if len(hits) < 2:
        return False, [
            "10-round context kept but recall failed (hits %r): %r"
            % (hits, reply[:120])
        ]
    return True, ["context survives 10 rounds (compaction pressure), recalled %r" % hits]


def _compaction_integrity():
    session_id, errors = _new_session("长会话开始：早期事实是灯塔42")
    if errors or not session_id:
        return False, errors or ["no session id"]
    for turn in range(1, 16):
        status, _ = api(
            "POST", "/chat",
            {"message": "填充轮次 %d" % turn, "session_id": session_id})
        if status != 200:
            return False, ["fill turn %d -> %d" % (turn, status)]
    status, text = api(
        "POST", "/chat",
        {"message": "早期事实是什么？", "session_id": session_id})
    reply = str(jget(text, "reply") or jget(text, "content") or "")
    if "灯塔" not in reply and "42" not in reply:
        return False, ["early fact lost after compaction: %r" % reply[:120]]
    return True, ["early fact still recallable after 15 filler turns"]


def _query():
    status, text = api("GET", "/sessions")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /sessions -> %d (want 200)" % status]
    if jget(text, "sessions") is None and jget(text, "items") is None and not text.strip().startswith("["):
        return False, ["session list payload not a list: %r" % text[:120]]
    return True, ["session list endpoint answers with a list"]


def _close():
    session_id, errors = _new_session("关闭测试")
    if errors or not session_id:
        return False, errors or ["no session id"]
    status, _ = api("DELETE", "/sessions/" + session_id)
    if status < 0:
        return False, ["delete unreachable"]
    if status not in (200, 202, 204):
        return False, ["DELETE /sessions/<id> -> %d (want 2xx)" % status]
    status_after, _ = api("GET", "/sessions/" + session_id)
    if status_after == 200:
        return False, ["session still readable after close"]
    return True, ["session closed and no longer readable (%d)" % status_after]


CASES = {
    "session.auto_create": (
        "自动创建", "首轮对话响应携带新 session_id", _auto_create),
    "session.multi_turn_reuse": (
        "多轮复用", "同一会话 3 轮复用后仍可查询", _multi_turn_reuse),
    "session.concurrent_isolation": (
        "并发隔离", "并发会话各自的事实不串扰", _concurrent_isolation),
    "session.cross_tenant_isolation": (
        "跨租户隔离", "商家 B 读不到商家 A 会话里的机密", _cross_tenant_isolation),
    "session.ten_round_context": (
        "10 轮上下文保持", "compaction 压力下十轮事实仍可召回", _ten_round_context),
    "session.compaction_integrity": (
        "压缩完整性", "长会话压缩后早期事实不丢失", _compaction_integrity),
    "session.query": ("会话查询", "GET /sessions 返回会话列表", _query),
    "session.close": ("会话关闭", "DELETE 后会话不可再读", _close),
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
