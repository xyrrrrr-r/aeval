#!/bin/sh
# report.markdown_render Markdown 渲染 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.markdown_render
