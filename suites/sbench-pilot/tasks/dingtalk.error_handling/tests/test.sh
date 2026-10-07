#!/bin/sh
# dingtalk.error_handling 错误处理 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_dingtalk.py dingtalk.error_handling
