#!/bin/sh
# tools.sub_agent sub_agent — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tools.py tools.sub_agent
