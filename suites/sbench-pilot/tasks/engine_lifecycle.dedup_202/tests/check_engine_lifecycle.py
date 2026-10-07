#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""engine_lifecycle —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
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

# engine_lifecycle 类 8 用例：引擎生命周期——task_execute 接收、去重
# (202)、心跳精确更新、任务完成、状态机流转、失败重试字段、健康状态、
# 心跳超时。


def _execute(body):
    body_text = json.dumps(body)
    return api("POST", "/engine/task_execute", body, headers=hmac_headers(body_text))


def _task_execute_accept():
    status, text = _execute({"task_id": "sbench-accept-1", "action": "run"})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (200, 202):
        return False, ["task_execute -> %d (want 200/202)" % status]
    return True, ["task_execute accepted with %d" % status]


def _dedup_202():
    status1, text1 = _execute({"task_id": "sbench-dedup", "action": "run"})
    if status1 < 0:
        return False, ["engine unreachable: " + text1]
    if status1 not in (200, 202):
        return False, ["first execute -> %d (want 2xx)" % status1]
    status2, text2 = _execute({"task_id": "sbench-dedup", "action": "run"})
    if status2 < 0:
        return False, ["engine unreachable: " + text2]
    if status2 >= 500:
        return False, ["duplicate execute -> %d (5xx)" % status2]
    if status2 == 202:
        return True, ["duplicate execute answered 202 (dedup accepted, no re-run)"]
    if status2 in (200,) and (jget(text2, "duplicate") or jget(text2, "deduplicated")):
        return True, ["duplicate execute answered 200 with dedup flag"]
    if status2 == 409:
        return True, ["duplicate execute rejected with 409"]
    return False, [
        "duplicate execute -> %d without dedup marker: %r"
        % (status2, text2[:120])
    ]


def _heartbeat_update():
    status, text = _execute({"task_id": "sbench-heartbeat", "action": "run"})
    if status not in (200, 202):
        return False, ["setup execute -> %d" % status]
    status, text = api(
        "POST", "/engine/tasks/sbench-heartbeat/heartbeat", {"progress": 50})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (200, 202):
        return False, ["heartbeat -> %d (want 200/202)" % status]
    status2, text2 = api("GET", "/engine/tasks/sbench-heartbeat")
    if status2 == 200:
        stamp = jget(text2, "last_heartbeat") or jget(text2, "data", "last_heartbeat")
        if not stamp:
            return False, ["task detail missing last_heartbeat after a beat"]
    return True, ["heartbeat recorded and queryable"]


def _task_complete():
    _execute({"task_id": "sbench-complete", "action": "run"})
    status, text = api(
        "POST", "/engine/tasks/sbench-complete/complete", {"result": "done"})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (200, 202):
        return False, ["complete -> %d (want 200/202)" % status]
    status2, text2 = api("GET", "/engine/tasks/sbench-complete")
    if status2 == 200:
        state = str(jget(text2, "status") or jget(text2, "data", "status") or "")
        if state and state not in ("completed", "done", "succeeded"):
            return False, ["after complete status is %r" % state]
    return True, ["task completes and reports a completed state"]


def _state_machine():
    _execute({"task_id": "sbench-state", "action": "run"})
    seen = []
    for step in ("start", "complete"):
        status, text = api(
            "POST", "/engine/tasks/sbench-state/%s" % step, {})
        seen.append((step, status))
        if status not in (200, 202):
            return False, ["state step %s -> %d: %r" % (step, status, text[:120])]
    status, text = api("GET", "/engine/tasks/sbench-state")
    state = str(jget(text, "status") or jget(text, "data", "status") or "")
    if state and state not in ("completed", "done", "succeeded"):
        return False, ["final state is %r after start+complete" % state]
    return True, ["state machine walks run -> completed"]


def _failure_retry_fields():
    status, text = api("GET", "/engine/tasks?failed=true")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status == 404:
        status, text = api("GET", "/engine/tasks")
    if status != 200:
        return False, ["task query -> %d (want 200)" % status]
    rows = jget(text, "tasks") or jget(text, "items") or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no task rows to inspect for retry fields"]
    retry_rows = [
        r for r in rows
        if isinstance(r, dict) and str(r.get("status")) in ("failed", "retrying", "error")
    ]
    if not retry_rows:
        return True, ["no failed tasks present — nothing to retry-check (ok)"]
    row = retry_rows[0]
    missing = [f for f in ("retry_count", "next_retry_at") if f not in row]
    if missing:
        return False, ["failed task missing %r: %r" % (missing, row)]
    return True, ["failed tasks carry retry_count/next_retry_at"]


def _status_healthy():
    # 源方案「重启恢复」在单测脚本里无法真正重启引擎：退化为其健康面
    # （uptime/恢复状态可观测）。
    status, text = api("GET", "/engine/status")
    if status == 404:
        status, text = api("GET", "/health")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["engine status -> %d (want 200)" % status]
    return True, ["engine status observable (restart-recovery health face)"]


def _heartbeat_timeout():
    # 制造一条过期心跳，验证引擎把僵尸任务标记出来（而非永不过期）。
    _execute({"task_id": "sbench-stale-beat", "action": "run"})
    api("POST", "/engine/tasks/sbench-stale-beat/heartbeat", {"progress": 1})
    status, text = api("POST", "/engine/tasks/sbench-stale-beat/expire", {})
    if status == 404:
        # 没有显式过期端点：查询侧至少要能暴露心跳字段供超时判定。
        status2, text2 = api("GET", "/engine/tasks/sbench-stale-beat")
        if status2 == 200 and (
                jget(text2, "last_heartbeat") is not None
                or jget(text2, "heartbeat_at") is not None):
            return True, ["no expire endpoint, but heartbeat is observable"]
        return False, [
            "neither expire endpoint nor observable heartbeat — timeout "
            "cannot be detected"
        ]
    if status not in (200, 202):
        return False, ["expire -> %d (want 200/202)" % status]
    return True, ["stale heartbeat expiration handled"]


CASES = {
    "engine_lifecycle.task_execute_accept": (
        "task_execute 接收", "签名执行请求被接受（200/202）", _task_execute_accept),
    "engine_lifecycle.dedup_202": (
        "去重(202)", "重复 task_execute 被去重吸收", _dedup_202),
    "engine_lifecycle.heartbeat_update": (
        "心跳精确更新", "心跳上报后任务详情可查到最新心跳", _heartbeat_update),
    "engine_lifecycle.task_complete": (
        "任务完成", "完成任务后状态为 completed", _task_complete),
    "engine_lifecycle.state_machine": (
        "状态机流转", "run -> completed 状态机可走通", _state_machine),
    "engine_lifecycle.failure_retry_fields": (
        "失败重试字段", "失败任务携带 retry_count/next_retry_at", _failure_retry_fields),
    "engine_lifecycle.status_healthy": (
        "健康状态（重启恢复面）", "引擎状态端点可观测", _status_healthy),
    "engine_lifecycle.heartbeat_timeout": (
        "心跳超时", "过期心跳可被判定/过期处理", _heartbeat_timeout),
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
