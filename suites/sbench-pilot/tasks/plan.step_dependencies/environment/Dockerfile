# ubuntu:24.04 pinned by its multi-arch manifest digest (same pin as the
# e2e-hello seed image). The engine under test is NOT in this image: the
# case script reaches it over the network as ENGINE_BASE_URL.
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# Baseline seed (suite base: baselines.ready): proves the seeded copy
# reached the sandbox before the agent starts.
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
