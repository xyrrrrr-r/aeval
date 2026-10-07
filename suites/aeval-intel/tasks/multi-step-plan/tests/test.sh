#!/bin/sh
# Task verifier: the agent must have written a usable answer to
# /workspace/summary.md. # Whether the agent planned (plan/todo tool) before acting is
# judged at the trajectory layer (complexity_handling).
set -u

mkdir -p /logs/verifier

reward=0
if [ -f /workspace/summary.md ]; then
    content="$(cat /workspace/summary.md)"
    if [ -n "$content" ]; then
        reward=1
    fi
fi

printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
