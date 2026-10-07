#!/bin/sh
# task_center.hmac_execute HMAC 执行 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_task_center.py task_center.hmac_execute
