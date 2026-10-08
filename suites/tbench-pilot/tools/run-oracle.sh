#!/bin/bash
# Oracle arm of the terminal-bench-core pilot.
#
# Purpose: prove the three vendored tasks are solvable and that their
# UPSTREAM tests verify green inside the adapted images, BEFORE any model
# output is graded.
#
# This arm runs Harbor DIRECTLY, without the aeval plugin. That is
# deliberate, not a shortcut: aeval's verification gate requires a
# "trusted control binding" (the DSH control stack's lease handshake),
# and the oracle agent never starts that stack — running it through the
# aeval plugin fails closed with
# `EvidenceIntegrityError: trial has no trusted control binding`. The
# oracle arm is a solvability pre-flight, not a graded run: it must NOT
# write into the pilot store. Harbor reads the reward the upstream
# `tests/test.sh` publishes at /logs/verifier/reward.txt, which is
# exactly the fact this arm needs to establish (3/3 reward = 1).
#
# Run on the cluster host.
set -euo pipefail

SUITE="${SUITE:-/root/e2e/lab-suite-tbench-oracle}"
RUN_NAME="${RUN_NAME:-tbench-oracle}"
RUN_DIR="/root/e2e/runs/$RUN_NAME"
export PATH=/usr/local/bin:/root/.local/bin:$PATH
export E2B_API_KEY=$(python3 -c "import json;print(json.load(open('/root/.e2b/config.json'))['teamApiKey'])")
export E2B_API_URL=http://localhost:3000
export E2B_DOMAIN=sandbox.localhost:8443
export SSL_CERT_FILE=/root/e2e/tls/ca.crt
unset E2B_DEBUG E2B_HTTP_SSL

rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"

echo "########## terminal-bench-core pilot: ORACLE ARM (harbor, no aeval plugin) ##########"
date -u
# Compose the same job aeval would, then hand it to Harbor unchanged.
(
  cd /root/src/aeval
  .venv/bin/python - "$SUITE" "$RUN_DIR" <<'PY'
import json, sys
from pathlib import Path

from aeval.suite_loader.loader import load_suite
from aeval.suite_loader.composition import compose_harbor_job

suite, run_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
job = compose_harbor_job(load_suite(suite))
job.environment.force_build = True
job.jobs_dir = run_dir / "harbor"
path = run_dir / "harbor-job.json"
path.write_text(json.dumps(job.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
print(f"composed {path} ({job.job_name}: {len(job.tasks)} tasks)")
PY
)

/root/e2e/harbor-cli.sh run --config "$RUN_DIR/harbor-job.json" \
    > "/root/e2e/$RUN_NAME.log" 2>&1 || true
echo "HARBOR_EXIT=$?"
date -u
tail -40 "/root/e2e/$RUN_NAME.log"
