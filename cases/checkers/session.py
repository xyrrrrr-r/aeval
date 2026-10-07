# session 类 8 用例：会话管理——自动创建、多轮复用、并发隔离、跨租户
# 隔离、10 轮上下文保持（compaction 压力）、查询与关闭。

import threading


def _new_session(message="你好"):
    status, text = api("POST", "/chat", {"message": message})
    if status != 200:
        return None, ["POST /chat -> %d (want 200)" % status]
    session_id = jget(text, "session_id") or jget(text, "data", "session_id")
    return session_id, []


def _auto_create():
    session_id, errors = _new_session("自动创建会话")
    if errors:
        return False, errors
    if not session_id:
        return False, ["reply carries no session_id — session not auto-created"]
    return True, ["session auto-created: %s" % session_id]


def _multi_turn_reuse():
    session_id, errors = _new_session("复用会话：记住数字 7")
    if errors or not session_id:
        return False, errors or ["no session id"]
    for turn in range(3):
        status, text = api(
            "POST", "/chat",
            {"message": "第 %d 轮" % (turn + 1), "session_id": session_id})
        if status != 200:
            return False, ["turn %d -> %d (want 200)" % (turn + 1, status)]
    status, text = api("GET", "/sessions/" + session_id)
    if status != 200:
        return False, ["GET /sessions/<id> -> %d after reuse (want 200)" % status]
    return True, ["session reused over 3 turns and still queryable"]


def _concurrent_isolation():
    ids, errors = {}, []
    results = {}

    def one(label, fact):
        status, text = api("POST", "/chat", {"message": "记住：%s" % fact})
        results[label] = (status, jget(text, "session_id"), text)

    threads = [
        threading.Thread(target=one, args=("a", "苹果是红色的")),
        threading.Thread(target=one, args=("b", "海水是咸的")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for label, (status, session_id, _text) in results.items():
        if status != 200 or not session_id:
            errors.append("%s session not created (status %d)" % (label, status))
    if errors:
        return False, errors
    status, text = api(
        "POST", "/chat",
        {"message": "我刚才说了什么水果？", "session_id": results["a"][1]})
    reply = str(jget(text, "reply") or jget(text, "content") or "")
    if "苹果" not in reply:
        return False, ["session a lost its own fact: %r" % reply[:120]]
    return True, ["concurrent sessions keep their facts separate"]


def _cross_tenant_isolation():
    if not TOKEN_B:
        return False, [
            "ENGINE_TOKEN_B not configured — cross-tenant check needs a "
            "second tenant credential"
        ]
    status, text = api("POST", "/chat", {"message": "商家A的机密是 TENANT-SECRET-A9"})
    if status != 200:
        return False, ["tenant A chat -> %d (want 200)" % status]
    session_id = jget(text, "session_id")
    probe = {"message": "商家A的机密是什么？"}
    if session_id:
        probe["session_id"] = session_id
    status_b, text_b = api("POST", "/chat", probe, token=TOKEN_B)
    if status_b < 0:
        return False, ["engine unreachable: " + text_b]
    reply_b = str(jget(text_b, "reply") or jget(text_b, "content") or text_b)
    if "TENANT-SECRET-A9" in reply_b:
        return False, ["tenant B saw tenant A's secret — isolation broken"]
    return True, ["tenant B cannot read tenant A's secret (status %d)" % status_b]


def _ten_round_context():
    session_id, errors = _new_session("十轮上下文测试开始")
    if errors or not session_id:
        return False, errors or ["no session id"]
    for turn in range(1, 11):
        status, text = api(
            "POST", "/chat",
            {"message": "第 %d 轮，请记住数字 %d000" % (turn, turn),
             "session_id": session_id})
        if status != 200:
            return False, ["round %d -> %d (want 200)" % (turn, status)]
    status, text = api(
        "POST", "/chat",
        {"message": "我前面说过的数字有哪些？随便报两个。",
         "session_id": session_id})
    if status != 200:
        return False, ["recall turn -> %d (want 200)" % status]
    reply = str(jget(text, "reply") or jget(text, "content") or "")
    hits = [str(n) + "000" for n in range(1, 11) if (str(n) + "000") in reply]
    if len(hits) < 2:
        return False, [
            "10-round context kept but recall failed (hits %r): %r"
            % (hits, reply[:120])
        ]
    return True, ["context survives 10 rounds (compaction pressure), recalled %r" % hits]


def _compaction_integrity():
    session_id, errors = _new_session("长会话开始：早期事实是灯塔42")
    if errors or not session_id:
        return False, errors or ["no session id"]
    for turn in range(1, 16):
        status, _ = api(
            "POST", "/chat",
            {"message": "填充轮次 %d" % turn, "session_id": session_id})
        if status != 200:
            return False, ["fill turn %d -> %d" % (turn, status)]
    status, text = api(
        "POST", "/chat",
        {"message": "早期事实是什么？", "session_id": session_id})
    reply = str(jget(text, "reply") or jget(text, "content") or "")
    if "灯塔" not in reply and "42" not in reply:
        return False, ["early fact lost after compaction: %r" % reply[:120]]
    return True, ["early fact still recallable after 15 filler turns"]


def _query():
    status, text = api("GET", "/sessions")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /sessions -> %d (want 200)" % status]
    if jget(text, "sessions") is None and jget(text, "items") is None and not text.strip().startswith("["):
        return False, ["session list payload not a list: %r" % text[:120]]
    return True, ["session list endpoint answers with a list"]


def _close():
    session_id, errors = _new_session("关闭测试")
    if errors or not session_id:
        return False, errors or ["no session id"]
    status, _ = api("DELETE", "/sessions/" + session_id)
    if status < 0:
        return False, ["delete unreachable"]
    if status not in (200, 202, 204):
        return False, ["DELETE /sessions/<id> -> %d (want 2xx)" % status]
    status_after, _ = api("GET", "/sessions/" + session_id)
    if status_after == 200:
        return False, ["session still readable after close"]
    return True, ["session closed and no longer readable (%d)" % status_after]


CASES = {
    "session.auto_create": (
        "自动创建", "首轮对话响应携带新 session_id", _auto_create),
    "session.multi_turn_reuse": (
        "多轮复用", "同一会话 3 轮复用后仍可查询", _multi_turn_reuse),
    "session.concurrent_isolation": (
        "并发隔离", "并发会话各自的事实不串扰", _concurrent_isolation),
    "session.cross_tenant_isolation": (
        "跨租户隔离", "商家 B 读不到商家 A 会话里的机密", _cross_tenant_isolation),
    "session.ten_round_context": (
        "10 轮上下文保持", "compaction 压力下十轮事实仍可召回", _ten_round_context),
    "session.compaction_integrity": (
        "压缩完整性", "长会话压缩后早期事实不丢失", _compaction_integrity),
    "session.query": ("会话查询", "GET /sessions 返回会话列表", _query),
    "session.close": ("会话关闭", "DELETE 后会话不可再读", _close),
}
