#!/bin/sh
# ddl.id_uniqueness 主键唯一性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.id_uniqueness
