# error 类 6 用例：异常处理——401 鉴权（无/错 token）、超大 body 防
# OOM、畸形 JSON 400、未知端点 404、不允许的方法 405。0.4.0 需求收
# 窄：HMAC 三用例（缺失/错误/重放）不再单独成例（task_center 的
# HMAC 执行检查仍在）。

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
    "error.oversized_body": (
        "超大 body", "5MB 请求体被优雅处理，无 5xx/OOM", _oversized_body),
    "error.invalid_json": (
        "畸形 JSON", "非法 JSON 请求体返回 400", _invalid_json),
    "error.unknown_endpoint": (
        "未知端点", "不存在的端点返回 404", _unknown_endpoint),
    "error.method_not_allowed": (
        "不允许的方法", "DELETE /health 返回 405（或 404）", _method_not_allowed),
}
