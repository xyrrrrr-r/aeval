# task_center 类 12 用例：任务中心——定时/一次性/即时创建、HMAC 执行
# 回调、查询/取消/禁用/启用/删除全生命周期、next_run 排程、漏跑恢复。


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


def _hmac_execute():
    body_text = json.dumps({"task_id": "sbench-hmac-execute", "action": "run"})
    status, text = api(
        "POST", "/engine/task_execute",
        {"task_id": "sbench-hmac-execute", "action": "run"},
        headers=hmac_headers(body_text))
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status in (401, 403):
        return False, [
            "signed execute rejected (%d) — ENGINE_HMAC_SECRET mismatch?" % status
        ]
    if status not in (200, 202):
        return False, ["signed execute -> %d (want 200/202)" % status]
    return True, ["HMAC-signed execution accepted with %d" % status]


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
    "task_center.hmac_execute": (
        "HMAC 执行", "HMAC 签名的执行回调被接受", _hmac_execute),
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
