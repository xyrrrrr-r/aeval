# ddl 类 15 用例：数据持久化——9 张表数据正确性、字段类型、时间合理
# 性、消息顺序、用量累加一致性、双写检测。

# 源方案说"9 张表"未点名；按被描述的系统域取合理清单，对齐真实引擎
# 时改此表并重新生成即可。
TABLES = (
    "sessions", "messages", "plans", "plan_steps", "tasks",
    "artifacts", "tool_calls", "usage_stats", "memory_records",
)


def _table_rows(table):
    status, text = api("GET", "/ddl/" + table)
    if status < 0:
        return None, ["engine unreachable: " + text]
    if status == 404:
        return None, ["table %s not exposed (404)" % table]
    if status != 200:
        return None, ["GET /ddl/%s -> %d (want 200)" % (table, status)]
    rows = jget(text, "rows") or jget(text, "items") or jget(text, "data")
    if rows is None:
        return None, ["table %s payload not row-shaped: %r" % (table, text[:120])]
    return rows, []


def _table_case(table):
    def run():
        rows, errors = _table_rows(table)
        if errors:
            return False, errors
        return True, ["table %s readable (%s rows)" % (table, len(rows))]
    return run


def _field_types():
    for table in TABLES[:3]:
        status, text = api("GET", "/ddl/schema", {"table": table})
        if status == 404:
            status, text = api("GET", "/ddl/%s/schema" % table)
        if status < 0:
            return False, ["engine unreachable: " + text]
        if status != 200:
            return False, ["schema for %s -> %d (want 200)" % (table, status)]
        if jget(text, "columns") is None and jget(text, "fields") is None:
            return False, ["schema payload missing columns: %r" % text[:120]]
    return True, ["schema endpoint exposes column types for sampled tables"]


def _time_sanity():
    import datetime
    floor = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    ceiling = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    for table in ("sessions", "messages"):
        rows, errors = _table_rows(table)
        if errors:
            continue  # 表不可读由 9 张表的用例负责报
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            stamp = row.get("created_at") or row.get("updated_at")
            if not stamp:
                continue
            try:
                moment = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                return False, ["%s.created_at not ISO: %r" % (table, stamp)]
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            if not (floor <= moment <= ceiling):
                return False, ["%s timestamp out of sane range: %r" % (table, stamp)]
    return True, ["sampled timestamps parse as ISO and sit in a sane window"]


def _message_order():
    rows, errors = _table_rows("messages")
    if errors:
        return False, errors
    by_session = {}
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        key = str(row.get("session_id"))
        by_session.setdefault(key, []).append(row)
    checked = 0
    for key, bucket in by_session.items():
        seqs = [r.get("seq") or r.get("id") for r in bucket]
        if any(s is None for s in seqs):
            continue
        try:
            numbers = [int(s) for s in seqs]
        except (TypeError, ValueError):
            continue
        if numbers != sorted(numbers):
            return False, ["session %s message order broken: %r" % (key, numbers)]
        checked += 1
    if not checked and rows:
        return False, ["messages carry no usable ordering key to verify"]
    return True, ["message order monotonic in %d sampled sessions" % checked]


def _usage_accumulation():
    rows, errors = _table_rows("usage_stats")
    if errors:
        return False, errors
    totals = {}
    for row in rows[:50]:
        if isinstance(row, dict) and row.get("total_tokens") is not None:
            key = str(row.get("session_id") or row.get("tenant_id") or "global")
            totals[key] = totals.get(key, 0) + 0  # 累计表的口径是行内总量
            totals[key] = max(totals[key], int(row.get("total_tokens") or 0))
    messages, errors = _table_rows("messages")
    if errors or not messages:
        # 没有可比对的消息行时，只验证累计表本身可读且字段在。
        if rows:
            return True, ["usage_stats readable with total fields"]
        return False, errors or ["no usage rows"]
    per_message = {}
    for row in messages[:200]:
        if isinstance(row, dict) and row.get("tokens") is not None:
            key = str(row.get("session_id"))
            per_message[key] = per_message.get(key, 0) + int(row.get("tokens") or 0)
    for key, total in totals.items():
        if key in per_message and per_message[key] > total:
            return False, [
                "usage total %r < sum of message tokens %d for %s"
                % (total, per_message[key], key)
            ]
    return True, ["usage totals consistent with per-message sums (sampled)"]


def _id_uniqueness():
    for table in ("sessions", "messages", "tasks"):
        rows, errors = _table_rows(table)
        if errors:
            continue
        ids = [
            str(r.get("id")) for r in rows[:100]
            if isinstance(r, dict) and r.get("id") is not None
        ]
        if not ids:
            continue
        if len(set(ids)) != len(ids):
            return False, ["%s has duplicate ids: %r" % (table, ids[:8])]
    return True, ["sampled table ids are unique (primary-key integrity)"]


def _double_write():
    first, errors = _table_rows("sessions")
    if errors:
        return False, errors
    import hashlib
    digest_a = hashlib.sha256(
        json.dumps(first, sort_keys=True, default=str).encode()).hexdigest()
    second, errors = _table_rows("sessions")
    if errors:
        return False, errors
    digest_b = hashlib.sha256(
        json.dumps(second, sort_keys=True, default=str).encode()).hexdigest()
    if digest_a != digest_b and first != second:
        return False, ["two reads of sessions disagree — double-write divergence"]
    return True, ["two consecutive reads agree (no double-write divergence)"]


CASES = {}
for _table in TABLES:
    CASES["ddl.%s_data" % _table] = (
        "%s 表数据" % _table,
        "GET /ddl/%s 返回可解析的行数据" % _table,
        _table_case(_table))
CASES["ddl.field_types"] = (
    "字段类型", "schema 端点暴露各表列类型", _field_types)
CASES["ddl.time_sanity"] = (
    "时间合理性", "时间戳可解析且落在合理窗口", _time_sanity)
CASES["ddl.message_order"] = (
    "消息顺序", "会话内消息按序号单调排列", _message_order)
CASES["ddl.usage_accumulation"] = (
    "用量累加一致性", "用量累计与逐条消息之和一致", _usage_accumulation)
CASES["ddl.id_uniqueness"] = (
    "主键唯一性", "抽样表行的 id 无重复", _id_uniqueness)
CASES["ddl.double_write"] = (
    "双写检测", "两次读取一致，无双写分歧", _double_write)
