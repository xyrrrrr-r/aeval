#!/bin/sh
# ddl.double_write 双写检测 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.double_write
