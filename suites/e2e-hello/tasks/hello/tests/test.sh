#!/bin/sh
# Harbor verifier: the agent must have written exactly "hello" into
# /workspace/result. Harbor requires a reward file from every verifier —
# a script that only exits non-zero makes the trial crash with
# RewardFileNotFoundError (found on the real e2b chain), so the reward is
# always written and the script always exits 0.
set -u

mkdir -p /logs/verifier

reward=0
if [ -f /workspace/result ] && [ "$(cat /workspace/result)" = "hello" ]; then
    reward=1
fi

printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
