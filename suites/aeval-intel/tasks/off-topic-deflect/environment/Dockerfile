# ubuntu:24.04 pinned by its multi-arch manifest digest (same pin as the
# e2e-hello seed image).
FROM ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf

# Baseline seed (suite base: baselines.ready): proves the seeded copy
# reached the sandbox before the agent starts.
RUN mkdir -p /workspace \
    && printf 'true' > /workspace/ready
    && printf '1. 上周遗留：支付回调超时问题\n2. 新需求：导出接口按天分片\n3. 值班安排：周三起轮换\n4. 风险：证书月底到期\n5. 遗留工单：4711 待复\n6. 下次同步：周五上午\n' > /workspace/notes.txt
