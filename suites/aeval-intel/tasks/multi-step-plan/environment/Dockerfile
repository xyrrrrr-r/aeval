# ubuntu:24.04 pinned by its multi-arch manifest digest (same pin as the
# e2e-hello seed image).
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# Baseline seed (suite base: baselines.ready): proves the seeded copy
# reached the sandbox before the agent starts.
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
    && mkdir -p /data \
    && printf 'id,name\n1,a\n2,b\n' > /data/a.csv \
    && printf 'id,city\n1,bj\n2,sh\n3,gz\n' > /data/b.csv
