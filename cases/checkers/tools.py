# tools 类 6 用例：工具系统——注册列表、get_current_time、web_fetch、
# read_skill、sub_agent、并发安全。

import threading


def _registry_list():
    status, text = api("GET", "/tools")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /tools -> %d (want 200)" % status]
    names = set()
    for entry in (jget(text, "tools") or jget(text, "items") or []):
        if isinstance(entry, dict):
            names.add(str(entry.get("name") or entry.get("id")))
        else:
            names.add(str(entry))
    expected = {"get_current_time", "web_fetch", "read_skill", "sub_agent"}
    missing = expected - names
    if missing:
        return False, ["tool registry missing %r (has %r)" % (sorted(missing), sorted(names)[:12])]
    return True, ["registry lists all expected tools: %r" % sorted(expected)]


def _get_current_time():
    status, text = api(
        "POST", "/tools/call", {"name": "get_current_time", "arguments": {}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call get_current_time -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not re.search(r"\d{4}", body):
        return False, ["no plausible year in time output: %r" % body[:120]]
    return True, ["get_current_time returns a plausible timestamp: %r" % body[:60]]


def _web_fetch():
    # 抓取引擎自身的 /health：无外部网络依赖的连通性证明。
    status, text = api(
        "POST", "/tools/call",
        {"name": "web_fetch",
         "arguments": {"url": BASE + "/health"}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call web_fetch -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["web_fetch returned empty content"]
    return True, ["web_fetch fetched %s/health (%d chars)" % (BASE, len(body))]


def _read_skill():
    status, text = api(
        "POST", "/tools/call", {"name": "read_skill", "arguments": {}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call read_skill -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["read_skill returned empty content"]
    return True, ["read_skill answers with skill content (%d chars)" % len(body)]


def _sub_agent():
    status, text = api(
        "POST", "/tools/call",
        {"name": "sub_agent",
         "arguments": {"task": "报告 1+1 的结果"}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["call sub_agent -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["sub_agent returned empty result"]
    return True, ["sub_agent answers with a result (%d chars)" % len(body)]


def _concurrent_safety():
    results = []

    def one(index):
        status, text = api(
            "POST", "/tools/call",
            {"name": "get_current_time", "arguments": {"label": index}})
        results.append((index, status))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    bad = [i for i, status in results if status != 200]
    if bad:
        return False, ["concurrent tool calls failed for indices %r" % bad]
    return True, ["5 concurrent tool calls all returned 200"]


CASES = {
    "tools.registry_list": (
        "工具注册列表", "GET /tools 列出预期注册的工具", _registry_list),
    "tools.get_current_time": (
        "get_current_time", "时间工具返回可信时间戳", _get_current_time),
    "tools.web_fetch": ("web_fetch", "抓取工具可用（引擎自身端点）", _web_fetch),
    "tools.read_skill": ("read_skill", "技能读取工具返回内容", _read_skill),
    "tools.sub_agent": ("sub_agent", "子代理工具返回执行结果", _sub_agent),
    "tools.concurrent_safety": (
        "并发安全", "5 路并发工具调用全部成功", _concurrent_safety),
}
