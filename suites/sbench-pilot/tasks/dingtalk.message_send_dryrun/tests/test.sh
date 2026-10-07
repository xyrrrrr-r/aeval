#!/bin/sh
# dingtalk.message_send_dryrun 消息发送干运行 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_dingtalk.py dingtalk.message_send_dryrun
