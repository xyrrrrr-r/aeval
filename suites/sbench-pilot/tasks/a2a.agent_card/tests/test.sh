#!/bin/sh
# a2a.agent_card Agent Card — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_a2a.py a2a.agent_card
