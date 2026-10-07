#!/bin/sh
# Task verifier: the agent must have written a usable answer to
# /workspace/result. # Whether the agent asked instead of guessing is judged at the
# trajectory layer (clarification).
set -u

mkdir -p /logs/verifier

reward=0
if [ -f /workspace/result ]; then
    content="$(cat /workspace/result)"
    if [ -n "$content" ]; then
        reward=1
    fi
fi

printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
