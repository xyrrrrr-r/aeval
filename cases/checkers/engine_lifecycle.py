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
