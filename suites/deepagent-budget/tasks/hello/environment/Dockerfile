# ubuntu:24.04 pinned by its multi-arch manifest digest; the e2b backend
# builds for arm64, where this resolves to the arm64 image.
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# The sandbox must ship node: the declared control stack
# (control_stack: deepagent-facade) runs the OpenAI-compatible facade inside
# this container, and aeval's bootstrap health-gates it before the run starts.
# Ubuntu 24.04's nodejs is 18.x — the facade needs fetch/AbortSignal (>=18)
# and nothing newer; the sandbox image itself is digest-pinned per run.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Baseline seed (suite.yaml: baselines.workspace_ready): the ready file
# proves the seeded copy reached the sandbox before the agent starts.
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
