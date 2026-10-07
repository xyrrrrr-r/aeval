#!/bin/sh
# report.ssr_endpoint SSR 端点 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.ssr_endpoint
