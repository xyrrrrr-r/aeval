#!/bin/sh
# Task verifier: the agent must have written exactly "unreadable" to
# /workspace/result. Whether the agent ALSO produced a usable reply
# (>= 10 chars, no crash) is judged at the trajectory layer.
set -u

mkdir -p /logs/verifier

reward=0
if [ -f /workspace/result ] && [ "$(cat /workspace/result)" = "unreadable" ]; then
    reward=1
fi

printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
