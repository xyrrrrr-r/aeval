# error 类 9 用例：异常处理——401 鉴权、HMAC 签名（缺失/错误/重放）、
# 超大 body 防 OOM、畸形 JSON 400、未知端点 404、不允许的方法 405。


def _no_auth():
    status, text = api("GET", "/plans", token="")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 401:
        return False, ["no-token request -> %d (want 401)" % status]
    return True, ["missing token rejected with 401"]


def _bad_auth():
    status, text = api("GET", "/plans", token="definitely-not-a-token")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 401:
        return False, ["garbage token -> %d (want 401)" % status]
    return True, ["invalid token rejected with 401"]


def _hmac_missing():
    status, text = api("POST", "/engine/task_execute", {"task_id": "sbench-hmac-missing"})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (401, 403):
        return False, ["task_execute without HMAC -> %d (want 401/403)" % status]
    return True, ["missing HMAC signature rejected with %d" % status]


def _hmac_wrong():
    body_text = json.dumps({"task_id": "sbench-hmac-wrong"})
    status, text = api(
        "POST", "/engine/task_execute", {"task_id": "sbench-hmac-wrong"},
        headers={"X-Timestamp": str(int(time.time())),
                 "X-Signature": "deadbeef" * 8})
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status not in (401, 403):
        return False, ["wrong HMAC signature -> %d (want 401/403)" % status]
    return True, ["wrong HMAC signature rejected with %d" % status]


def _hmac_replay():
    body_text = json.dumps({"task_id": "sbench-hmac-replay"})
    headers = hmac_headers(body_text)
    first_status, first_text = api(
        "POST", "/engine/task_execute", {"task_id": "sbench-hmac-replay"},
        headers=headers)
    if first_status < 0:
        return False, ["engine unreachable: " + first_text]
    if first_status in (401, 403):
        # 部署没有配 ENGINE_HMAC_SECRET 时第一发就被拒——签名机制在，
        # 但无法验证重放语义，记为失败并留原因。
        return False, [
            "first signed request already rejected (%d) — HMAC secret "
            "mismatch between checker and engine?" % first_status
        ]
    replay_status, replay_text = api(
        "POST", "/engine/task_execute", {"task_id": "sbench-hmac-replay"},
        headers=headers)  # 同一签名+时间戳原样重放
    if replay_status in (401, 403):
        return True, ["replayed signature+timestamp rejected with %d" % replay_status]
    if replay_status in (200, 202):
        # 幂等去重也算防重放（第二次不产生副作用）。
        if jget(replay_text, "duplicate") or jget(replay_text, "deduplicated"):
            return True, ["replay absorbed as dedup (no side effect)"]
    return False, [
        "replayed request accepted with %d — replay protection missing"
        % replay_status
    ]


def _oversized_body():
    status, text = api(
        "POST", "/chat", "y" * (5 * 1024 * 1024), timeout=90.0, raw=True)
    if status < 0:
        return False, ["engine unreachable or timed out: " + text]
    if status >= 500:
        return False, ["5MB body -> %d (5xx: OOM risk)" % status]
    return True, ["5MB body handled with %d (no 5xx)" % status]


def _invalid_json():
    status, text = api("POST", "/chat", "{definitely-not-json", raw=True)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 400:
        return False, ["malformed JSON -> %d (want 400)" % status]
    return True, ["malformed JSON rejected with 400"]


def _unknown_endpoint():
    status, text = api("GET", "/no-such-endpoint-sbench")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 404:
        return False, ["unknown endpoint -> %d (want 404)" % status]
    return True, ["unknown endpoint returns 404"]


def _method_not_allowed():
    status, text = api("DELETE", "/health")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 405 and status != 404:
        return False, ["DELETE /health -> %d (want 405; 404 acceptable)" % status]
    return True, ["unsupported method rejected with %d" % status]


CASES = {
    "error.no_auth": ("无鉴权", "缺 token 的请求被拒为 401", _no_auth),
    "error.bad_auth": ("错误鉴权", "伪造 token 被拒为 401", _bad_auth),
    "error.hmac_missing": (
        "HMAC 缺失", "受保护端点缺 HMAC 签名被拒为 401/403", _hmac_missing),
    "error.hmac_wrong": (
        "HMAC 错误", "错误签名被拒为 401/403", _hmac_wrong),
    "error.hmac_replay": (
        "HMAC 重放", "同一签名+时间戳重放被拒或幂等吸收", _hmac_replay),
    "error.oversized_body": (
        "超大 body", "5MB 请求体被优雅处理，无 5xx/OOM", _oversized_body),
    "error.invalid_json": (
        "畸形 JSON", "非法 JSON 请求体返回 400", _invalid_json),
    "error.unknown_endpoint": (
        "未知端点", "不存在的端点返回 404", _unknown_endpoint),
    "error.method_not_allowed": (
        "不允许的方法", "DELETE /health 返回 405（或 404）", _method_not_allowed),
}
