#!/bin/sh
# tools.web_fetch web_fetch — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tools.py tools.web_fetch
