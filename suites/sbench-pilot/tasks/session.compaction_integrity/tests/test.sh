#!/bin/sh
# session.compaction_integrity 压缩完整性 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_session.py session.compaction_integrity
