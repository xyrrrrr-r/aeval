#!/bin/sh
# report.download 下载 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.download
