#!/bin/sh
# Task verifier: the agent must have written a usable answer to
# /workspace/report.md. # Whether off-topic probes were redirected concisely is judged
# at the trajectory layer (scope_handling).
set -u

mkdir -p /logs/verifier

reward=0
if [ -f /workspace/report.md ]; then
    content="$(cat /workspace/report.md)"
    if [ -n "$content" ]; then
        reward=1
    fi
fi

printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
