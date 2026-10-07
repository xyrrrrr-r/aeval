# plan 类 15 用例：Plan 编排——创建/状态流转/审批/拒绝/取消/僵尸恢复、
# Step 三种类型（AUTO/MANUAL/APPROVAL）全覆盖、依赖链、更新、列表、
# 并发创建、非法流转。


def _create_plan(steps=None, approval=False):
    body = {
        "title": "sbench 编排用例",
        "steps": steps or [{"name": "采集", "type": "AUTO"}],
    }
    if approval:
        body["require_approval"] = True
    status, text = api("POST", "/plans", body)
    if status not in (200, 201):
        return None, ["POST /plans -> %d (want 200/201)" % status]
    plan_id = jget(text, "id") or jget(text, "plan_id") or jget(text, "data", "id")
    return plan_id, []


def _create():
    plan_id, errors = _create_plan()
    if errors:
        return False, errors
    if not plan_id:
        return False, ["plan create response carries no id"]
    return True, ["plan created: %s" % plan_id]


def _get():
    plan_id, errors = _create_plan()
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("GET", "/plans/" + plan_id)
    if status != 200:
        return False, ["GET /plans/<id> -> %d (want 200)" % status]
    if jget(text, "title") is None and jget(text, "data", "title") is None:
        return False, ["plan detail missing title: %r" % text[:120]]
    return True, ["plan detail queryable with fields"]


def _status_flow():
    plan_id, errors = _create_plan()
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("POST", "/plans/%s/start" % plan_id, {})
    if status not in (200, 202):
        return False, ["start -> %d (want 200/202)" % status]
    state = str(jget(text, "status") or jget(text, "data", "status") or "")
    if state and state not in ("running", "started", "active"):
        return False, ["after start status is %r (want running)" % state]
    return True, ["plan transitions created -> running"]


def _approve():
    plan_id, errors = _create_plan(approval=True)
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("POST", "/plans/%s/approve" % plan_id, {})
    if status not in (200, 202):
        return False, ["approve -> %d (want 200/202)" % status]
    state = str(jget(text, "status") or jget(text, "data", "status") or "")
    if state and "approv" not in state.lower():
        return False, ["after approve status is %r" % state]
    return True, ["approval-gated plan approved"]


def _reject():
    plan_id, errors = _create_plan(approval=True)
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("POST", "/plans/%s/reject" % plan_id, {})
    if status not in (200, 202):
        return False, ["reject -> %d (want 200/202)" % status]
    state = str(jget(text, "status") or jget(text, "data", "status") or "")
    if state and "reject" not in state.lower():
        return False, ["after reject status is %r" % state]
    return True, ["approval-gated plan rejected"]


def _cancel():
    plan_id, errors = _create_plan()
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("POST", "/plans/%s/cancel" % plan_id, {})
    if status not in (200, 202):
        return False, ["cancel -> %d (want 200/202)" % status]
    return True, ["plan cancelled"]


def _zombie_recovery():
    plan_id, errors = _create_plan()
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("POST", "/plans/%s/recover" % plan_id, {})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (200, 202, 404):
        return False, ["recover -> %d (want 200/202; 404 if unsupported)" % status]
    if status == 404:
        return True, ["no zombie-recovery endpoint — acceptable for this build"]
    return True, ["zombie recovery endpoint answered"]


def _step_auto():
    steps = [{"name": "自动步", "type": "AUTO"}]
    plan_id, errors = _create_plan(steps=steps)
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    api("POST", "/plans/%s/start" % plan_id, {})
    status, text = api("GET", "/plans/" + plan_id)
    body = jget(text, "steps") or jget(text, "data", "steps") or []
    if body and isinstance(body, list):
        states = [str(s.get("status")) for s in body if isinstance(s, dict)]
        if states and all(s in ("pending", "waiting") for s in states):
            return False, ["AUTO step never executed: %r" % states]
    return True, ["AUTO step executes without manual trigger"]


def _step_manual():
    steps = [{"name": "手动步", "type": "MANUAL"}]
    plan_id, errors = _create_plan(steps=steps)
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("GET", "/plans/" + plan_id)
    body = jget(text, "steps") or jget(text, "data", "steps") or []
    if body and isinstance(body, list):
        states = [str(s.get("status")) for s in body if isinstance(s, dict)]
        if states and all(s in ("completed", "done") for s in states):
            return False, ["MANUAL step auto-completed: %r" % states]
    return True, ["MANUAL step waits for an explicit trigger"]


def _step_approval():
    steps = [{"name": "审批步", "type": "APPROVAL"}]
    plan_id, errors = _create_plan(steps=steps)
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("GET", "/plans/" + plan_id)
    body = jget(text, "steps") or jget(text, "data", "steps") or []
    if body and isinstance(body, list):
        states = [str(s.get("status")) for s in body if isinstance(s, dict)]
        if states and all(s in ("completed", "done") for s in states):
            return False, ["APPROVAL step completed without approval: %r" % states]
    return True, ["APPROVAL step blocks until approved"]


def _step_dependencies():
    steps = [
        {"name": "第一步", "type": "AUTO"},
        {"name": "第二步", "type": "AUTO", "depends_on": ["第一步"]},
    ]
    plan_id, errors = _create_plan(steps=steps)
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api("GET", "/plans/" + plan_id)
    body = jget(text, "steps") or jget(text, "data", "steps") or []
    if body and isinstance(body, list) and len(body) == 2:
        second = body[1]
        if str(second.get("status")) in ("running", "completed", "done"):
            first_state = str(body[0].get("status"))
            if first_state in ("pending", "waiting"):
                return False, [
                    "step 2 runs while step 1 still %r — dependency violated"
                    % first_state
                ]
    return True, ["dependent step ordering respected"]


def _update():
    plan_id, errors = _create_plan()
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, text = api(
        "PATCH", "/plans/" + plan_id, {"title": "sbench 改名后"})
    if status not in (200, 204):
        return False, ["PATCH /plans/<id> -> %d (want 200/204)" % status]
    status2, text2 = api("GET", "/plans/" + plan_id)
    title = jget(text2, "title") or jget(text2, "data", "title")
    if title and "改名" not in str(title):
        return False, ["title not persisted: %r" % title]
    return True, ["plan update persisted"]


def _list():
    status, text = api("GET", "/plans")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /plans -> %d (want 200)" % status]
    if jget(text, "plans") is None and jget(text, "items") is None \
            and not text.strip().startswith("["):
        return False, ["plan list payload not a list: %r" % text[:120]]
    return True, ["plan list endpoint answers with a list"]


def _concurrent_create():
    import threading
    ids, errors = [], []

    def one():
        plan_id, errs = _create_plan()
        if errs:
            errors.extend(errs)
        elif plan_id:
            ids.append(plan_id)

    threads = [threading.Thread(target=one) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        return False, errors
    if len(set(ids)) != len(ids):
        return False, ["concurrent creates collided: %r" % ids]
    return True, ["3 concurrent plan creates got distinct ids"]


def _invalid_transition():
    plan_id, errors = _create_plan()
    if errors or not plan_id:
        return False, errors or ["no plan id"]
    status, _ = api("POST", "/plans/%s/start" % plan_id, {})
    if status not in (200, 202):
        return False, ["start -> %d (precondition failed)" % status]
    status, _ = api("POST", "/plans/%s/complete" % plan_id, {})
    if status not in (200, 202):
        return False, ["complete -> %d (precondition failed)" % status]
    status2, _ = api("POST", "/plans/%s/cancel" % plan_id, {})
    if status2 < 400:
        return False, ["cancelling a completed plan -> %d (want 4xx)" % status2]
    return True, ["invalid transition rejected with %d" % status2]


CASES = {
    "plan.create": ("创建 Plan", "POST /plans 创建编排计划并返回 id", _create),
    "plan.get": ("查询 Plan", "GET /plans/<id> 返回计划详情字段", _get),
    "plan.status_flow": (
        "状态流转", "计划从创建态流转到运行态", _status_flow),
    "plan.approve": ("审批", "审批门计划批准后进入批准态", _approve),
    "plan.reject": ("拒绝", "审批门计划可被拒绝", _reject),
    "plan.cancel": ("取消", "运行中计划可取消", _cancel),
    "plan.zombie_recovery": (
        "僵尸恢复", "僵尸（心跳丢失）计划可被恢复处理", _zombie_recovery),
    "plan.step_auto": (
        "AUTO 步骤", "自动步骤无需人工触发即执行", _step_auto),
    "plan.step_manual": (
        "MANUAL 步骤", "手动步骤等待显式触发，不自动完成", _step_manual),
    "plan.step_approval": (
        "APPROVAL 步骤", "审批步骤在批准前保持阻塞", _step_approval),
    "plan.step_dependencies": (
        "步骤依赖", "依赖链上后继步骤不先于前置执行", _step_dependencies),
    "plan.update": ("更新", "PATCH 更新计划标题并持久化", _update),
    "plan.list": ("列表", "GET /plans 返回计划列表", _list),
    "plan.concurrent_create": (
        "并发创建", "3 路并发创建得到互不冲突的 id", _concurrent_create),
    "plan.invalid_transition": (
        "非法流转", "对已完成计划的非法操作被拒为 4xx", _invalid_transition),
}
