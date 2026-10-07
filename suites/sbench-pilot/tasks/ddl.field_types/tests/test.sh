#!/bin/sh
# ddl.field_types 字段类型 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_ddl.py ddl.field_types
