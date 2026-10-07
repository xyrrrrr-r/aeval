#!/bin/sh
# dingtalk.worksheet_write 工作表写入 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_dingtalk.py dingtalk.worksheet_write
