# Adapted pilot base image for the terminal-bench-core suite.
#
# This is deliberately ONE base for all three pilot tasks, replacing the
# two upstream bases (python-3-13, ubuntu-24-04). Why, measured on this
# cluster:
#
#   * ghcr.io is not usable at image-build scale here — the Docker
#     daemon's configured proxy (127.0.0.1:7897) never listened, and
#     once revived the host's direct egress measured ~45 KB/s to ghcr
#     and ~5-100 KB/s to pypi/ubuntu/nodejs, so a ~250 MB upstream base
#     is a multi-hour pull;
#   * the internal registry already carries an adapted Ubuntu image
#     (`e2b-orchestration/ubuntu@sha256:5aabce…`) with python3.10,
#     curl and openssl — everything the three pilot tasks need;
#   * Node.js, uv and a pinned wheelhouse are staged into the build
#     context from the cluster host, so this image builds with NO
#     network access at all.
#
# What the image provides (a superset of what the pilot tasks need):
#   * python3 (3.10) — the tasks' tests and the migrated verifier;
#   * openssl, curl — required by the openssl task / general tooling;
#   * Node.js v24.20.0 — the DSH CLI is installed with
#     `npm install --global` at trial time (offline: the cluster host's
#     npm cache is baked in at /root/.npm, see images/npmrc);
#   * uv + a pinned, local wheelhouse (/opt/wheels) + /opt/verifier/
#     pyproject.toml — the verifier resolves pytest fully offline.
#     Index resolution was NOT enough: with only a warm uv cache,
#     `uv add --offline pytest` still failed on this image's Python 3.10
#     (tomli/exceptiongroup are pulled in for <3.11 and uv needs index
#     metadata for them). Pinning the closure in one pyproject that
#     resolves from a local wheelhouse removes the network from the
#     verifier path entirely.
#   * /workspace/ready — the seed asserted by suite.yaml's baseline.
#
# The task's own Dockerfile steps (ENV/COPY/RUN) are carried over from
# the migration output verbatim; only the FROM line changes.
#
# Build with images/build-bases.sh (it stages the context first).

ARG UPSTREAM
FROM ${UPSTREAM}

# The version the DSH adapter's official lock pins; build-bases.py reads it
# from aeval and passes it here, so the warmed cache cannot silently drift
# from what a trial installs.
ARG DSH_VERSION

# Node.js runtime for the DSH CLI, staged from the cluster host.
COPY node/ /usr/local/
# uv binary, staged from the cluster host (no astral.sh fetch).
COPY uv /usr/local/bin/uv
# The pinned wheelhouse (pytest and its closure) and the verifier
# project the setup script installs from it.
COPY wheels/ /opt/wheels/
COPY verifier/ /opt/verifier/
# Keep every uv invocation in this image offline: a silent network fetch
# would hang a sealed trial instead of failing loudly.
ENV UV_OFFLINE=1

# The DSH CLI is installed by the agent at trial time (`npm install
# --global`). This cluster's npm registry path is throttled to a few KB/s
# (measured ~3 KB/s to registry.npmjs.org), which blew Harbor's 900 s
# agent-setup bound on the first pilot attempt. The cluster host's npm
# cache is complete for the pinned CLI (a fully offline install of it was
# verified on the host), so it is baked here together with an .npmrc that
# sets `prefer-offline`: the trial install then resolves from /root/.npm
# with no network.
COPY npm-cache/ /root/.npm/
COPY npmrc /root/.npmrc

RUN node --version \
    && npm --version \
    && uv --version \
    && python3 --version

# Prove the EXACT offline path the task verifier will take (same
# pyproject, same wheelhouse). A build that cannot complete it must fail
# HERE, not inside a sealed trial.
RUN mkdir -p /tmp/uvselfcheck \
    && cd /tmp/uvselfcheck \
    && cp /opt/verifier/pyproject.toml . \
    && uv sync --offline \
    && uv run pytest --version \
    && cd / \
    && rm -rf /tmp/uvselfcheck

# Prove the EXACT offline path the trial-time install will take: same
# cache, same package, same pinned version, `--offline` (no network at
# all). A build whose cache is incomplete must fail HERE, not inside a
# sealed trial after burning its setup budget.
RUN test -n "${DSH_VERSION}" \
    && mkdir -p /tmp/dshwarm \
    && npm install --global --prefix /tmp/dshwarm \
        --cache /root/.npm --offline --no-audit --no-fund \
        "@deepseek-ai/dsh@${DSH_VERSION}" \
    && test -x /tmp/dshwarm/bin/dsh \
    && rm -rf /tmp/dshwarm

# Readiness seed asserted by the suite baseline (see suite.yaml).
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
