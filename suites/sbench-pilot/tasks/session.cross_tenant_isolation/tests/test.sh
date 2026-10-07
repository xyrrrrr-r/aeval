#!/bin/sh
# session.cross_tenant_isolation 跨租户隔离 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.cross_tenant_isolation
