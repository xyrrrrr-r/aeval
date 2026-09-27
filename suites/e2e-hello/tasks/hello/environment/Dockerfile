# ubuntu:24.04 pinned by its multi-arch manifest digest; the e2b backend
# builds for arm64, where this resolves to the arm64 image.
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# Baseline seed (suite.yaml: baselines.workspace_ready): the ready file
# proves the seeded copy reached the sandbox before the agent starts.
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
