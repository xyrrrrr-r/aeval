# dingtalk 类 5 用例：钉钉集成——工具注册、工作表读写（干运行）、消息
# 发送（干运行）、错误处理。干运行 = 不真正外发，部署侧以
# DINGTALK_DRY_RUN 标记。


def _tool_registry():
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
    dingtalk = {n for n in names if "dingtalk" in n.lower() or "worksheet" in n.lower()}
    if not dingtalk:
        return False, ["no dingtalk-ish tools registered: %r" % sorted(names)[:16]]
    return True, ["dingtalk tools registered: %r" % sorted(dingtalk)]


def _worksheet_read():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.worksheet.read",
         "arguments": {"worksheet_id": "sbench-sheet", "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["worksheet read (dry run) -> %d (want 200)" % status]
    body = str(jget(text, "result") or jget(text, "output") or text)
    if not body.strip():
        return False, ["worksheet read returned empty content"]
    return True, ["worksheet read (dry run) answered with content"]


def _worksheet_write():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.worksheet.write",
         "arguments": {"worksheet_id": "sbench-sheet", "values": {"k": "v"},
                       "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["worksheet write (dry run) -> %d (want 200)" % status]
    body = json.loads(text) if text.strip().startswith("{") else {}
    if body.get("dry_run") is False:
        return False, ["write executed for real despite dry_run=true"]
    return True, ["worksheet write honored dry_run"]


def _message_send_dryrun():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.send_message",
         "arguments": {"chat_id": "sbench-chat", "content": "干运行消息",
                       "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["send message (dry run) -> %d (want 200)" % status]
    body = json.loads(text) if text.strip().startswith("{") else {}
    if body.get("dry_run") is False or body.get("sent") is True:
        return False, ["message actually sent despite dry run"]
    return True, ["message send stayed a dry run"]


def _error_handling():
    status, text = api(
        "POST", "/tools/call",
        {"name": "dingtalk.worksheet.read",
         "arguments": {"worksheet_id": "", "dry_run": True}})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status >= 500:
        return False, ["bad worksheet id -> %d (5xx)" % status]
    return True, ["dingtalk bad input handled with %d (no 5xx)" % status]


CASES = {
    "dingtalk.tool_registry": (
        "工具注册", "钉钉族工具出现在注册表中", _tool_registry),
    "dingtalk.worksheet_read": (
        "工作表读取", "干运行下工作表可读", _worksheet_read),
    "dingtalk.worksheet_write": (
        "工作表写入", "干运行下写操作不落盘", _worksheet_write),
    "dingtalk.message_send_dryrun": (
        "消息发送干运行", "干运行下消息不真正外发", _message_send_dryrun),
    "dingtalk.error_handling": (
        "错误处理", "非法工作表 id 得到 4xx 而非 5xx", _error_handling),
}
