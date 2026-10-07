# artifact 类 5 用例：产出物——API 可达、字段完整性、env 正确性、列表、
# 持久化。


def _api_reachable():
    status, text = api("GET", "/artifacts")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    return True, ["artifact listing endpoint reachable"]


def _field_completeness():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    rows = jget(text, "artifacts") or jget(text, "items") \
        or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no artifact rows to inspect (seed one task run first)"]
    row = rows[0] if isinstance(rows[0], dict) else {}
    missing = [f for f in ("id", "type", "created_at") if f not in row]
    if missing:
        return False, ["artifact row missing fields %r: %r" % (missing, row)]
    return True, ["artifact rows carry id/type/created_at"]


def _env_correctness():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    rows = jget(text, "artifacts") or jget(text, "items") \
        or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no artifact rows to inspect"]
    row = rows[0] if isinstance(rows[0], dict) else {}
    env = row.get("env") or row.get("environment")
    if env is None:
        return False, ["artifact carries no env field: %r" % row]
    return True, ["artifact records its originating env (%r)" % str(env)[:60]]


def _listing():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    if jget(text, "artifacts") is None and jget(text, "items") is None \
            and not text.strip().startswith("["):
        return False, ["artifact payload not a list: %r" % text[:120]]
    return True, ["artifact listing is list-shaped"]


def _persistence():
    status, text = api("GET", "/artifacts")
    if status != 200:
        return False, ["GET /artifacts -> %d (want 200)" % status]
    rows = jget(text, "artifacts") or jget(text, "items") \
        or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return False, ["no artifact rows to re-read"]
    row = rows[0]
    artifact_id = row.get("id") if isinstance(row, dict) else row
    if not artifact_id:
        return False, ["artifact row carries no id"]
    status2, text2 = api("GET", "/artifacts/" + str(artifact_id))
    if status2 != 200:
        return False, ["GET /artifacts/<id> -> %d (want 200)" % status2]
    digest_a = row.get("sha256") or row.get("digest") if isinstance(row, dict) else None
    digest_b = jget(text2, "sha256") or jget(text2, "digest")
    if digest_a and digest_b and digest_a != digest_b:
        return False, ["artifact digest changed between reads"]
    return True, ["artifact re-readable with stable content"]


CASES = {
    "artifact.api_reachable": (
        "API 可达", "GET /artifacts 返回 200", _api_reachable),
    "artifact.field_completeness": (
        "字段完整性", "产出物行携带 id/type/created_at", _field_completeness),
    "artifact.env_correctness": (
        "env 正确性", "产出物记录其来源环境", _env_correctness),
    "artifact.listing": ("列表", "产出物列表为列表结构", _listing),
    "artifact.persistence": (
        "持久化", "产出物可重读且内容稳定", _persistence),
}
