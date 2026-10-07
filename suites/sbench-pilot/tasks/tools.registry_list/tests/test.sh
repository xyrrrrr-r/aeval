#!/bin/sh
# tools.registry_list 工具注册列表 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tools.py tools.registry_list
