#!/bin/bash
# pilot arm: 3 tasks x 3 attempts with the real DSH agent and the real
# upstream provider (api.deepseek.com), graded by the outcome layer
# (upstream verifier reward) and the trajectory layer (framework rubric
# with the Terminal-Bench integrity gates).
#
# Run on the cluster host, after the oracle arm is green.
set -euo pipefail

SUITE="${SUITE:-/root/e2e/lab-suite-tbench}"
RUN_NAME="${RUN_NAME:-tbench-m0}"
export PATH=/usr/local/bin:/root/.local/bin:$PATH
cd /root/src/aeval
export E2B_API_KEY=$(python3 -c "import json;print(json.load(open('/root/.e2b/config.json'))['teamApiKey'])")
export E2B_API_URL=http://localhost:3000
export E2B_DOMAIN=sandbox.localhost:8443
export SSL_CERT_FILE=/root/e2e/tls/ca.crt
# The trusted input-token counter. The broker ties every request to it
# when limits.maxTokens is set, and the online chain's offline stub
# counter (a fixed 64 tokens) made every real call fail closed with
# AEVAL_TOKEN_BOUND_VIOLATED. This one answers with the byte length of the
# exact request body, which cannot undercount.
TOKEN_COUNT_PORT="${TOKEN_COUNT_PORT:-8792}"
pgrep -f "token_count.py --host 127.0.0.1 --port $TOKEN_COUNT_PORT" >/dev/null || {
    setsid nohup /root/src/aeval/.venv/bin/python /root/e2e/token_count.py --host 127.0.0.1 --port "$TOKEN_COUNT_PORT" \
        > /root/e2e/token-count.log 2>&1 < /dev/null &
    sleep 1
}
# The pilot budget (maxSteps 60 / maxTokens 2M) and the auxiliary-call
# policy (compaction allowed + accounted) are generated from the suite's
# budgets.yaml by tools/gen_broker_spec.py.
export AEVAL_BROKER_JSON=/root/e2e/broker-spec-tbench.json
/root/src/aeval/.venv/bin/python /root/e2e/gen_broker_spec.py \
    --budgets /root/e2e/lab-suite-tbench/budgets.yaml \
    --base /root/e2e/broker-spec-real.json \
    --token-count-endpoint "http://127.0.0.1:$TOKEN_COUNT_PORT/tokens/count" \
    --out "$AEVAL_BROKER_JSON"
export DEEPSEEK_API_KEY=$(cat /root/e2e/keys/deepseek.key)
unset E2B_DEBUG E2B_HTTP_SSL
pgrep -f stub_upstream.py >/dev/null || {
    setsid nohup python3 /root/e2e/gateway/stub_upstream.py \
        > /root/e2e/gateway/stub.log 2>&1 < /dev/null &
    sleep 2
}

rm -rf "/root/e2e/runs/$RUN_NAME" "/root/e2e/runs/$RUN_NAME.sqlite3"
mkdir -p /root/e2e/runs
echo "########## aeval PILOT (3 tasks x 3 attempts, real provider) ##########"
date -u
.venv/bin/python -m aeval.cli run \
  --suite "$SUITE" \
  --run-dir "/root/e2e/runs/$RUN_NAME" \
  --store "/root/e2e/runs/$RUN_NAME.sqlite3" \
  --harbor-cli /root/e2e/harbor-cli.sh \
  --force-build > "/root/e2e/$RUN_NAME.log" 2>&1
echo "AEVAL_RUN_EXIT=$?"
date -u
tail -60 "/root/e2e/$RUN_NAME.log"
