#!/bin/sh
# Harbor verifier test: the agent must have written exactly "hello"
# into /workspace/result (no trailing newline, no extra whitespace).
set -eu

expected="hello"
actual="$(cat /workspace/result)"

if [ "$actual" != "$expected" ]; then
    echo "result mismatch: expected '$expected', got '$actual'" >&2
    exit 1
fi
