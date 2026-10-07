#!/bin/sh
# report.schema 报告 schema — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.schema
