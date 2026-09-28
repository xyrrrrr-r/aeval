#!/bin/bash
# Offline replacement for terminal-bench's setup-uv-pytest.sh.
#
# Upstream installs curl, then uv from astral.sh, then resolves pytest
# from the network. The sealed pilot sandbox has no verification-time
# egress, so the task images bake:
#
#   * uv                  at /usr/local/bin/uv
#   * a pinned wheelhouse at /opt/wheels
#   * the verifier project at /opt/verifier/pyproject.toml (the same
#     file the image's build-time self-check uses, so the path proven at
#     build time is exactly the path taken here)
#   * UV_OFFLINE=1, so any network attempt fails loudly instead of
#     hanging a sealed trial
#
# Only this infrastructure script is adapted. tests/run-uv-pytest.sh —
# the invocation that actually runs the task's tests — is untouched, so
# the tests execute exactly as upstream (`uv run pytest <file> -rA`).
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
    echo "setup-uv-pytest.sh: uv is missing from the image" >&2
    exit 1
fi

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile before running this script." >&2
    exit 1
fi

# The task Dockerfile sets ENV TEST_DIR=/tests, but the e2b exec path the
# verifier uses does not carry the image environment (observed on the
# oracle arm: `uv run pytest $TEST_DIR/test_outputs.py` expanded to
# `/test_outputs.py`). This script is SOURCED by the task's test.sh, so an
# export here reaches run-uv-pytest.sh.
export TEST_DIR="${TEST_DIR:-/tests}"

cp /opt/verifier/pyproject.toml ./pyproject.toml
# Resolve pytest's pinned closure from the baked wheelhouse; a cache or
# wheelhouse miss is a loud failure rather than a silent network fetch.
uv sync --offline
