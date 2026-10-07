#!/bin/sh
# ddl.message_order 消息顺序 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.message_order
