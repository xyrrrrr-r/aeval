#!/bin/sh
# report.hmac_auth HMAC 鉴权 — injected by aeval/cases/generate.py.
set -u
mkdir -p /logs/verifier
exec python3 /tests/check_report.py report.hmac_auth
