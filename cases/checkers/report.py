# report 类 7 用例：报告系统——SSR 端点、Markdown 渲染、任务完成
# 自动生成、报告 schema、列表、下载、租户隔离。0.5.0 需求移出
# HMAC 鉴权（report.hmac_auth）用例。


def _any_report_id():
    status, text = api("GET", "/reports")
    if status != 200:
        return None, ["GET /reports -> %d (want 200)" % status]
    rows = jget(text, "reports") or jget(text, "items") or jget(text, "data") or []
    if not isinstance(rows, list) or not rows:
        return None, ["no reports yet — run one task to seed a report"]
    row = rows[0]
    return (row.get("id") if isinstance(row, dict) else row), []


def _ssr_endpoint():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/report/" + str(report_id))
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /report/<id> -> %d (want 200)" % status]
    if not re.search(r"<html|<!doctype", text, re.IGNORECASE):
        return False, ["SSR reply is not HTML: %r" % text[:80]]
    return True, ["report SSR endpoint renders HTML"]


def _markdown_render():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/report/" + str(report_id))
    if status != 200:
        return False, ["GET /report/<id> -> %d (want 200)" % status]
    if re.search(r"<h[1-6]|<strong|<ul|<table", text, re.IGNORECASE):
        return True, ["markdown rendered to HTML block elements"]
    if "```" in text or "**" in text:
        return False, ["raw markdown leaked into the page (unrendered)"]
    return True, ["report page contains rendered content"]


def _auto_generate():
    body_text = json.dumps({"task_id": "sbench-report-seed", "action": "run"})
    status, text = api(
        "POST", "/engine/task_execute",
        {"task_id": "sbench-report-seed", "action": "run"},
        headers=hmac_headers(body_text))
    if status not in (200, 202):
        return False, ["seed execute -> %d (want 2xx)" % status]
    api("POST", "/engine/tasks/sbench-report-seed/complete", {"result": "done"})
    status2, text2 = api("GET", "/reports")
    if status2 != 200:
        return False, ["GET /reports -> %d (want 200)" % status2]
    rows = jget(text2, "reports") or jget(text2, "items") or jget(text2, "data") or []
    seeded = [
        r for r in rows
        if isinstance(r, dict) and "sbench-report-seed" in json.dumps(r, default=str)
    ]
    if not seeded:
        return False, ["completed task did not produce a report entry"]
    return True, ["task completion auto-generated a report"]


def _schema():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/reports/" + str(report_id))
    if status == 404:
        status, text = api("GET", "/report/" + str(report_id) + "?format=json")
    if status != 200:
        return False, ["report json -> %d (want 200)" % status]
    if not text.strip().startswith("{"):
        return False, ["report json payload is not an object: %r" % text[:80]]
    data = json.loads(text)
    missing = [f for f in ("id", "created_at") if f not in data]
    if missing:
        return False, ["report json missing %r" % missing]
    return True, ["report json schema carries id/created_at"]


def _listing():
    status, text = api("GET", "/reports")
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["GET /reports -> %d (want 200)" % status]
    if jget(text, "reports") is None and jget(text, "items") is None \
            and not text.strip().startswith("["):
        return False, ["report list payload not a list: %r" % text[:120]]
    return True, ["report listing is list-shaped"]


def _download():
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/reports/%s/download" % report_id)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status != 200:
        return False, ["download -> %d (want 200)" % status]
    if not text.strip():
        return False, ["download body is empty"]
    return True, ["report downloadable with content (%d chars)" % len(text)]


def _isolation():
    if not TOKEN_B:
        return False, [
            "ENGINE_TOKEN_B not configured — isolation check needs a "
            "second tenant credential"
        ]
    report_id, errors = _any_report_id()
    if errors:
        return False, errors
    status, text = api("GET", "/reports/" + str(report_id), token=TOKEN_B)
    if status < 0:
        return False, ["engine unreachable: " + text]
    if status in (403, 404):
        return True, ["tenant B cannot read tenant A's report (%d)" % status]
    if status == 200:
        return False, ["tenant B read tenant A's report — isolation broken"]
    return False, ["unexpected status %d for cross-tenant report read" % status]


CASES = {
    "report.ssr_endpoint": (
        "SSR 端点", "GET /report/<id> 服务端渲染 HTML", _ssr_endpoint),
    "report.markdown_render": (
        "Markdown 渲染", "报告页是渲染后的 HTML 而非裸 markdown", _markdown_render),
    "report.auto_generate": (
        "自动生成", "任务完成后报告自动出现", _auto_generate),
    "report.schema": (
        "报告 schema", "报告 JSON 携带 id/created_at", _schema),
    "report.listing": ("列表", "GET /reports 返回列表", _listing),
    "report.download": ("下载", "报告可下载且非空", _download),
    "report.isolation": (
        "租户隔离", "租户 B 读不到租户 A 的报告", _isolation),
}
