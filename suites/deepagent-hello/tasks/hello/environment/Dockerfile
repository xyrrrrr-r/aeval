# ubuntu:24.04 pinned by its multi-arch manifest digest; the e2b backend
# builds for arm64, where this resolves to the arm64 image.
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# The sandbox must ship node: this adapter's declared control stack
# (control_stack: deepagent-facade) runs the OpenAI-compatible facade inside
# this container, and aeval's bootstrap health-gates it before the run starts.
# Ubuntu 24.04's nodejs is 18.x — the facade needs fetch/AbortSignal (>=18)
# and nothing newer; the sandbox image itself is digest-pinned per run.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The agent CLI is baked in at build time: every suite here runs with
# network_mode = "no-network", so a uvx distribution would need PyPI at agent
# setup. The version is pinned to the adapter's _DEEPAGENTS_CODE_VERSION —
# tests/integration/test_deepagent_budget_suite.py keeps the two equal.
#
# Install is offline on purpose: the lab host has no PyPI egress, so the
# closure ships in the build context (wheelhouse/) and is prepared on a machine
# that does — see tools/fetch_deepagent_wheelhouse.py. pip still builds the one
# sdist-only dependency from source inside its isolated build env, whose
# setuptools/wheel come from the same wheelhouse.
COPY wheelhouse /opt/wheelhouse
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && python3 -m venv /opt/deepagents \
    && /opt/deepagents/bin/pip install --no-index --find-links /opt/wheelhouse \
        deepagents-code==0.1.78 \
    && ln -sf /opt/deepagents/bin/dcode /usr/local/bin/dcode \
    && rm -rf /opt/wheelhouse /var/lib/apt/lists/*

# Baseline seed (suite.yaml: baselines.workspace_ready): the ready file
# proves the seeded copy reached the sandbox before the agent starts.
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
