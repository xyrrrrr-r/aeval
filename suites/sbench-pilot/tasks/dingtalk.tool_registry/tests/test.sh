#!/bin/sh
# dingtalk.tool_registry 工具注册 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_dingtalk.py dingtalk.tool_registry
