#!/bin/sh
# dingtalk.worksheet_read 工作表读取 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_dingtalk.py dingtalk.worksheet_read
