#!/bin/sh
# health.sam_memory_diagnosis 记忆系统连通性诊断 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_health.py health.sam_memory_diagnosis
