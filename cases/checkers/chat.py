# chat 类 10 用例：核心对话——流式/非流式、空消息、超长输入、特殊字符、
# SSE 事件格式、unicode、多轮上下文、并发。

import threading


def _basic():
    status, text = api("POST", "/chat", {"message": "你好，请回复：收到"})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["POST /chat -> %d (want 200)" % status]
    reply = jget(text, "reply") or jget(text, "content") or jget(text, "message")
    if not reply:
        return False, ["no reply field: %r" % text[:120]]
    return True, ["chat round trip ok, reply %d chars" % len(str(reply))]


def _non_streaming():
    status, text = api("POST", "/chat", {"message": "非流式", "stream": False})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["POST /chat (stream=false) -> %d (want 200)" % status]
    if "data:" in text[:64]:
        return False, ["non-streaming reply looks like SSE: %r" % text[:64]]
    return True, ["non-streaming reply is a plain JSON document"]


def _streaming():
    status, text = api("POST", "/chat", {"message": "流式", "stream": True})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["POST /chat (stream=true) -> %d (want 200)" % status]
    if "data:" not in text:
        return False, ["streaming reply carries no data: events: %r" % text[:120]]
    return True, ["streaming reply carries %d data: lines" % text.count("data:")]


def _sse_event_format():
    status, text = api("POST", "/chat", {"message": "SSE 格式", "stream": True})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["stream request -> %d (want 200)" % status]
    bad = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if not re.match(r"^(event|data|id|retry)\s*:", line):
            bad.append("line %d: %r" % (lineno, line[:60]))
    if bad:
        return False, ["malformed SSE lines: " + "; ".join(bad[:3])]
    if not re.search(r"\[DONE\]|message_end|event:\s*done", text, re.IGNORECASE):
        return False, ["SSE stream missing a termination marker"]
    return True, ["every SSE line is event/data/id/retry, stream terminates"]


def _empty_message():
    status, _text = api("POST", "/chat", {"message": ""})
    if status < 0:
        return False, ["engine unreachable: " + _text]
    if status >= 500:
        return False, ["empty message -> %d (5xx: server error, want 4xx)" % status]
    if status >= 400:
        return True, ["empty message rejected with %d" % status]
    return False, ["empty message accepted with %d (want 4xx)" % status]


def _oversized_input():
    status, _text = api(
        "POST", "/chat", {"message": "x" * (1024 * 1024)}, timeout=60.0)
    if status < 0:
        return False, ["engine unreachable or timed out: " + _text]
    if status >= 500:
        return False, ["1MB message -> %d (5xx: OOM/crash risk)" % status]
    return True, ["1MB message handled with status %d (no 5xx)" % status]


def _special_chars():
    payload = {
        "message": '<script>alert("xss")</script> & <b>bold</b> "quotes" \\ /%00;',
    }
    status, text = api("POST", "/chat", payload)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status >= 500:
        return False, ["special chars -> %d (5xx)" % status]
    reply = jget(text, "reply") or jget(text, "content") or ""
    if not reply:
        return False, ["no reply for special-char message"]
    return True, ["special-char message answered with %d" % status]


def _unicode_content():
    status, text = api("POST", "/chat", {"message": "中文测试 🎉 かな"})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["unicode message -> %d (want 200)" % status]
    reply = jget(text, "reply") or jget(text, "content") or ""
    if not reply:
        return False, ["no reply for unicode message"]
    return True, ["unicode message answered, reply %d chars" % len(str(reply))]


def _multi_turn_context():
    status, text = api("POST", "/chat", {"message": "我的暗号是蓝鲸77"})
    if status != 200:
        return False, ["first turn -> %d (want 200)" % status]
    session_id = jget(text, "session_id") or jget(text, "data", "session_id")
    second = {"message": "我的暗号是什么？"}
    if session_id:
        second["session_id"] = session_id
    status2, text2 = api("POST", "/chat", second)
    if status2 != 200:
        return False, ["second turn -> %d (want 200)" % status2]
    reply = str(jget(text2, "reply") or jget(text2, "content") or "")
    if "蓝鲸" not in reply and "77" not in reply:
        return False, ["second turn does not recall the fact: %r" % reply[:120]]
    return True, ["multi-turn context recalled within the session"]


def _concurrent_requests():
    results = []

    def one(index):
        status, text = api("POST", "/chat", {"message": "并发测试 %d" % index})
        results.append((index, status, text))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    bad = [i for i, status, _ in results if status != 200]
    if bad:
        return False, ["concurrent requests failed for indices %r" % bad]
    replies = [
        str(jget(text, "reply") or jget(text, "content") or "") for _, _, text in results
    ]
    if len(set(replies)) < 2:
        return False, ["concurrent replies look cross-contaminated"]
    return True, ["5 concurrent chat requests all returned 200 with distinct replies"]


CASES = {
    "chat.basic": ("基础对话", "POST /chat 完成一轮问答，回复非空", _basic),
    "chat.non_streaming": (
        "非流式对话", "stream=false 时返回普通 JSON 文档", _non_streaming),
    "chat.streaming": (
        "流式对话", "stream=true 时返回 SSE data: 事件流", _streaming),
    "chat.sse_event_format": (
        "SSE 事件格式", "流式回复每行都是 event/data/id/retry 字段且带终止标记",
        _sse_event_format),
    "chat.empty_message": (
        "空消息", "空 message 被拒为 4xx 而不是 5xx 崩溃", _empty_message),
    "chat.oversized_input": (
        "超长输入", "1MB 消息被优雅处理（413 或截断），无 5xx/OOM",
        _oversized_input),
    "chat.special_chars": (
        "特殊字符", "脚本/引号/控制字符消息得到正常回复，无 5xx", _special_chars),
    "chat.unicode_content": (
        "Unicode 内容", "中文/emoji 消息往返正常", _unicode_content),
    "chat.multi_turn_context": (
        "多轮上下文", "同会话第二轮能召回第一轮给出的事实", _multi_turn_context),
    "chat.concurrent_requests": (
        "并发请求", "5 路并发对话全部 200 且互不串扰", _concurrent_requests),
}
