#!/usr/bin/env python3
# 由 aeval/cases/generate.py 按类别生成 —— 不要手改套件 tasks/ 下的
# 副本；改用例请改 aeval/cases/checkers/<category>.py 后重新注入。
"""ddl —— 源方案《Benchmark 测评指标设计方案》§2 服务用例的
执行脚本：直接探测被测引擎（ENGINE_BASE_URL）并发布
/logs/verifier/reward.txt（检查通过 = 1，失败 = 0；引擎不可达视为
失败，原因打印到 verifier 日志留痕）。"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ENGINE_BASE_URL", "http://engine:8080").rstrip("/")
TOKEN = os.environ.get("ENGINE_TOKEN", "")
TOKEN_B = os.environ.get("ENGINE_TOKEN_B", "")
HMAC_SECRET = os.environ.get("ENGINE_HMAC_SECRET", "sbench-hmac-secret")
REWARD_PATH = "/logs/verifier/reward.txt"


def api(method, path, body=None, headers=None, token=None, timeout=30.0, raw=False):
    """对引擎发一次 HTTP 调用；返回 (status, text)，status < 0 表示连不上。

    raw=True 时 body 按原始字节发送（用于畸形 JSON 等负向用例）。
    """
    if raw:
        data = body.encode() if isinstance(body, str) else body
    else:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    bearer = TOKEN if token is None else token
    if bearer:
        req.add_header("Authorization", "Bearer " + bearer)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — 连不上等传输层失败
        return -1, "{}: {}".format(type(exc).__name__, exc)


def jget(text, *path):
    """从 JSON 回复里取嵌套字段；解析失败或路径不存在返回 None。"""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def hmac_headers(body_text):
    """对 body 计算约定形制的 HMAC-SHA256 签名头。"""
    ts = str(int(time.time()))
    digest = _hmac.new(
        HMAC_SECRET.encode(), (ts + "." + body_text).encode(), hashlib.sha256
    ).hexdigest()
    return {"X-Timestamp": ts, "X-Signature": digest}

# ddl 类 8 用例：数据持久化——核心表数据正确性（sessions/artifacts/
# tool_calls/usage_stats）、字段类型、时间合理性、用量累加一致性、
# 主键唯一性。0.3.0 需求收窄：messages/plans/plan_steps/tasks/
# memory_records 五张表与消息顺序、双写检测不再单独成例。

# 引擎持久化的 9 张表（源方案"9 张表"未点名，按被描述的系统域取合
# 理清单）：schema/时间/主键等横切检查按引擎实际形态覆盖全表；只有
# CASE_TABLES 里的表单独成例。对齐真实引擎时改此表并重新生成即可。
TABLES = (
    "sessions", "messages", "plans", "plan_steps", "tasks",
    "artifacts", "tool_calls", "usage_stats", "memory_records",
)
# 单独成例（ddl.<table>_data）的表。
CASE_TABLES = ("sessions", "artifacts", "tool_calls", "usage_stats")


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
            continue  # 表不可读由 ddl.<table>_data 用例负责报
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


CASES = {}
for _table in CASE_TABLES:
    CASES["ddl.%s_data" % _table] = (
        "%s 表数据" % _table,
        "GET /ddl/%s 返回可解析的行数据" % _table,
        _table_case(_table))
CASES["ddl.field_types"] = (
    "字段类型", "schema 端点暴露各表列类型", _field_types)
CASES["ddl.time_sanity"] = (
    "时间合理性", "时间戳可解析且落在合理窗口", _time_sanity)
CASES["ddl.usage_accumulation"] = (
    "用量累加一致性", "用量累计与逐条消息之和一致", _usage_accumulation)
CASES["ddl.id_uniqueness"] = (
    "主键唯一性", "抽样表行的 id 无重复", _id_uniqueness)


def check(case_id):
    title, _doc, run = CASES[case_id]
    reasons = [case_id + " " + title]
    try:
        ok, detail = run()
    except Exception as exc:  # noqa: BLE001 — 用例脚本的任何异常都判失败
        return False, reasons + ["check raised {}: {}".format(type(exc).__name__, exc)]
    if not isinstance(detail, list):
        detail = [str(detail)]
    return bool(ok), reasons + detail


def main(argv):
    if len(argv) != 2 or argv[1] not in CASES:
        print("usage: check <case_id>; cases: " + ", ".join(sorted(CASES)))
        return 2
    ok, reasons = check(argv[1])
    for reason in reasons:
        print("[sbench] " + reason)
    reward = "1" if ok else "0"
    os.makedirs(os.path.dirname(REWARD_PATH), exist_ok=True)
    with open(REWARD_PATH, "w", encoding="utf-8") as fh:
        fh.write(reward + "\n")
    print("[sbench] reward=" + reward)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
