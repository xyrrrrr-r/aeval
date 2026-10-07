#!/bin/sh
# ddl.messages_data messages 表数据 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.messages_data
