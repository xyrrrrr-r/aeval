#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""task_center —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
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

# task_center 类 11 用例：任务中心——定时/一次性/即时创建、查询/
# 取消/禁用/启用/删除全生命周期、next_run 排程、漏跑恢复。0.5.0
# 需求移出 HMAC 执行（task_center.hmac_execute）用例。


def _create_task(kind, extra=None):
    body = {"type": kind, "name": "sbench-%s" % kind}
    body.update(extra or {})
    status, text = api("POST", "/tasks", body)
    if status not in (200, 201):
        return None, ["POST /tasks (%s) -> %d (want 2xx)" % (kind, status)]
    return (jget(text, "id") or jget(text, "task_id") or jget(text, "data", "id")), []


def _cron_create():
    task_id, errors = _create_task("cron", {"cron": "0 3 * * *"})
    if errors:
        return False, errors
    if not task_id:
        return False, ["cron task create response carries no id"]
    return True, ["cron task created: %s" % task_id]


def _oneshot_create():
    task_id, errors = _create_task("oneshot", {"run_at": "2030-01-01T00:00:00Z"})
    if errors:
        return False, errors
    if not task_id:
        return False, ["oneshot task create response carries no id"]
    return True, ["oneshot task created: %s" % task_id]


def _immediate_create():
    task_id, errors = _create_task("immediate")
    if errors:
        return False, errors
    if not task_id:
        return False, ["immediate task create response carries no id"]
    status, text = api("GET", "/tasks/" + task_id)
    if status == 200:
        state = str(jget(text, "status") or jget(text, "data", "status") or "")
        if state in ("pending", "waiting", "created"):
            return False, ["immediate task stuck in %r" % state]
    return True, ["immediate task created and left the pending state"]


def _query():
    status, text = api("GET", "/tasks")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /tasks -> %d (want 200)" % status]
    if jget(text, "tasks") is None and jget(text, "items") is None \
            and not text.strip().startswith("["):
        return False, ["task list payload not a list: %r" % text[:120]]
    return True, ["task list endpoint answers with a list"]


def _cancel():
    task_id, errors = _create_task("cron", {"cron": "0 4 * * *"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, _ = api("POST", "/tasks/%s/cancel" % task_id, {})
    if status not in (200, 202):
        return False, ["cancel -> %d (want 200/202)" % status]
    return True, ["task cancelled"]


def _disable():
    task_id, errors = _create_task("cron", {"cron": "0 5 * * *"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, _ = api("POST", "/tasks/%s/disable" % task_id, {})
    if status not in (200, 202):
        return False, ["disable -> %d (want 200/202)" % status]
    return True, ["task disabled"]


def _enable():
    task_id, errors = _create_task("cron", {"cron": "0 5 * * *"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    api("POST", "/tasks/%s/disable" % task_id, {})
    status, _ = api("POST", "/tasks/%s/enable" % task_id, {})
    if status not in (200, 202):
        return False, ["enable -> %d (want 200/202)" % status]
    return True, ["task re-enabled after disable"]


def _delete():
    task_id, errors = _create_task("oneshot", {"run_at": "2030-01-01T00:00:00Z"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, _ = api("DELETE", "/tasks/" + task_id)
    if status not in (200, 202, 204):
        return False, ["delete -> %d (want 2xx)" % status]
    status_after, _ = api("GET", "/tasks/" + task_id)
    if status_after == 200:
        return False, ["task still readable after delete"]
    return True, ["task deleted and no longer readable"]


def _lifecycle():
    task_id, errors = _create_task("cron", {"cron": "0 6 * * *"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    for action in ("disable", "enable", "cancel"):
        status, _ = api("POST", "/tasks/%s/%s" % (task_id, action), {})
        if status not in (200, 202):
            return False, ["lifecycle %s -> %d (want 2xx)" % (action, status)]
    status, _ = api("DELETE", "/tasks/" + task_id)
    if status not in (200, 202, 204):
        return False, ["lifecycle delete -> %d (want 2xx)" % status]
    return True, ["create -> disable -> enable -> cancel -> delete all 2xx"]


def _next_run():
    task_id, errors = _create_task("cron", {"cron": "0 3 * * *"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, text = api("GET", "/tasks/" + task_id)
    if status != 200:
        return False, ["GET task -> %d (want 200)" % status]
    next_run = jget(text, "next_run_at") or jget(text, "next_run") \
        or jget(text, "data", "next_run_at")
    if not next_run:
        return False, ["cron task carries no next_run_at: %r" % text[:120]]
    import datetime
    try:
        moment = datetime.datetime.fromisoformat(str(next_run).replace("Z", "+00:00"))
    except ValueError:
        return False, ["next_run_at not ISO: %r" % next_run]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    if moment <= datetime.datetime.now(datetime.timezone.utc):
        return False, ["next_run_at %r is in the past" % next_run]
    return True, ["next_run_at computed and in the future"]


def _missed_run():
    task_id, errors = _create_task(
        "oneshot", {"run_at": "2020-01-01T00:00:00Z"})
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, text = api("GET", "/tasks/" + task_id)
    if status != 200:
        return False, ["GET missed task -> %d (want 200)" % status]
    state = str(jget(text, "status") or jget(text, "data", "status") or "")
    if state in ("pending", "waiting", "scheduled"):
        return False, ["past-due oneshot still %r — no missed-run handling" % state]
    return True, ["past-due oneshot handled (status %r)" % state]


CASES = {
    "task_center.cron_create": (
        "定时任务创建", "cron 表达式任务可创建", _cron_create),
    "task_center.oneshot_create": (
        "一次性任务创建", "指定时刻的一次性任务可创建", _oneshot_create),
    "task_center.immediate_create": (
        "即时任务创建", "即时任务创建后立即离开等待态", _immediate_create),
    "task_center.query": ("查询", "GET /tasks 返回任务列表", _query),
    "task_center.cancel": ("取消", "任务可取消", _cancel),
    "task_center.disable": ("禁用", "任务可禁用", _disable),
    "task_center.enable": ("启用", "禁用后可重新启用", _enable),
    "task_center.delete": ("删除", "任务删除后不可读", _delete),
    "task_center.lifecycle": (
        "全生命周期", "建/禁/启/消/删链路全 2xx", _lifecycle),
    "task_center.next_run": (
        "下次执行排程", "cron 任务计算出未来的 next_run_at", _next_run),
    "task_center.missed_run": (
        "漏跑恢复", "过期一次性任务被处理而非无限等待", _missed_run),
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
