#!/bin/sh
# tools.read_skill read_skill — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_tools.py tools.read_skill
