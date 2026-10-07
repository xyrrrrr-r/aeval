#!/bin/sh
# report.auto_generate 自动生成 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.auto_generate
