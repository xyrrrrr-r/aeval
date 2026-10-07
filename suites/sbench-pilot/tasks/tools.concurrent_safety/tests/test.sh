#!/bin/sh
# tools.concurrent_safety 并发安全 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tools.py tools.concurrent_safety
