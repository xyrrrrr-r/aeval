#!/bin/sh
# Task verifier: the agent must have written a non-empty, single-line
# self-introduction to /workspace/result. The reward is always written
# and the script always exits 0 (Harbor requires a reward file from
# every verifier).
set -u

mkdir -p /logs/verifier

reward=0
if [ -f /workspace/result ]; then
    content="$(cat /workspace/result)"
    # Non-empty and a single line (the instruction demands one sentence,
    # no newline). The *quality* of the sentence (identity keywords,
    # brevity, format) is judged at the trajectory layer from the sealed
    # transcript; here we only check the file outcome.
    if [ -n "$content" ] && [ "$(printf '%s' "$content" | wc -l)" -eq 0 ]; then
        reward=1
    fi
fi

printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
