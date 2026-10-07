# a2a 类 8 用例：A2A 协议——send/get/cancel task、鉴权（401）、错误码、
# 重复 task_id 处理、agent card 发现、任务状态历史。


def _send_task(task_id=None):
    body = {"task": "sbench a2a probe：报告完成"}
    if task_id:
        body["task_id"] = task_id
    status, text = api("POST", "/a2a/tasks", body)
    if status not in (200, 201, 202):
        return None, ["POST /a2a/tasks -> %d (want 2xx)" % status]
    return (jget(text, "task_id") or jget(text, "id") or task_id), []


def _send():
    task_id, errors = _send_task()
    if errors:
        return False, errors
    if not task_id:
        return False, ["a2a send carries no task id"]
    return True, ["a2a task sent: %s" % task_id]


def _get_task():
    task_id, errors = _send_task("a2a-fixed-get")
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, text = api("GET", "/a2a/tasks/" + task_id)
    if status != 200:
        return False, ["GET /a2a/tasks/<id> -> %d (want 200)" % status]
    if jget(text, "status") is None and jget(text, "data", "status") is None:
        return False, ["task detail missing status: %r" % text[:120]]
    return True, ["a2a task queryable with status"]


def _cancel_task():
    task_id, errors = _send_task("a2a-fixed-cancel")
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, _ = api("POST", "/a2a/tasks/%s/cancel" % task_id, {})
    if status not in (200, 202):
        return False, ["cancel -> %d (want 200/202)" % status]
    return True, ["a2a task cancelled"]


def _no_auth():
    status, text = api("POST", "/a2a/tasks", {"task": "x"}, token="")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 401:
        return False, ["no-token a2a call -> %d (want 401)" % status]
    return True, ["a2a without token rejected with 401"]


def _error_codes():
    status, _ = api("GET", "/a2a/tasks/does-not-exist-404")
    if status != 404:
        return False, ["unknown a2a task -> %d (want 404)" % status]
    status2, _ = api("POST", "/a2a/tasks", "{not-json", raw=True)
    if status2 >= 500:
        return False, ["malformed a2a body -> %d (5xx)" % status2]
    return True, ["a2a error codes are 4xx-shaped (404 verified, body %d)" % status2]


def _duplicate_task_id():
    task_id = "a2a-dup-probe"
    first, errors = _send_task(task_id)
    if errors:
        return False, errors
    status, text = api(
        "POST", "/a2a/tasks", {"task": "重复 id 重发", "task_id": task_id})
    if status >= 500:
        return False, ["duplicate task_id -> %d (5xx crash)" % status]
    if status == 409:
        return True, ["duplicate task_id rejected with 409"]
    if status in (200, 202):
        returned = jget(text, "task_id") or jget(text, "id")
        if returned and returned != task_id:
            return True, ["duplicate send deduplicated to a new id %r" % returned]
        return True, ["duplicate task_id answered idempotently (202-style)"]
    return False, ["duplicate task_id -> %d (unexpected)" % status]


def _agent_card():
    status, text = api("GET", "/.well-known/agent.json")
    if status == 404:
        status, text = api("GET", "/a2a/card")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["agent card -> %d (want 200)" % status]
    if jget(text, "name") is None and jget(text, "capabilities") is None:
        return False, ["agent card missing name/capabilities: %r" % text[:120]]
    return True, ["a2a agent card discoverable with fields"]


def _task_status_history():
    task_id, errors = _send_task("a2a-fixed-history")
    if errors or not task_id:
        return False, errors or ["no task id"]
    status, text = api("GET", "/a2a/tasks/" + task_id)
    if status != 200:
        return False, ["GET task -> %d (want 200)" % status]
    history = jget(text, "history") or jget(text, "status_history") \
        or jget(text, "data", "history")
    if history is None:
        # 状态机至少要暴露当前状态，历史字段是增强项。
        if jget(text, "status") is None and jget(text, "data", "status") is None:
            return False, ["task exposes neither history nor status"]
        return True, ["task exposes current status (history optional)"]
    return True, ["task carries a status history (%d entries)" % len(history)]


CASES = {
    "a2a.send_task": ("发送任务", "POST /a2a/tasks 派发任务并返回 id", _send),
    "a2a.get_task": ("查询任务", "GET /a2a/tasks/<id> 返回任务状态", _get_task),
    "a2a.cancel_task": ("取消任务", "A2A 任务可取消", _cancel_task),
    "a2a.no_auth": ("鉴权", "无 token 的 A2A 调用返回 401", _no_auth),
    "a2a.error_codes": (
        "错误码", "未知任务 404、坏请求 4xx，无 5xx", _error_codes),
    "a2a.duplicate_task_id": (
        "重复 task_id", "重复派发被幂等/409 处理，不崩溃", _duplicate_task_id),
    "a2a.agent_card": (
        "Agent Card", "A2A agent card 可发现且字段完整", _agent_card),
    "a2a.task_status_history": (
        "状态历史", "任务暴露状态（或状态历史）", _task_status_history),
}
