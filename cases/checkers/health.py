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
