#!/bin/sh
# tools.get_current_time get_current_time — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tools.py tools.get_current_time
