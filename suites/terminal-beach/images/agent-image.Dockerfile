# terminal-beach 的 agent 基础镜像 —— 在集群宿主上构建，推到内部 registry。
#
# 为什么不是"上游 ubuntu + 联网装依赖"：本 lab 的自建 e2b 给不出模板构建所需的
# 签名 URL，且任务声明的 egress 只有 broker 主机；因此镜像必须**离线**构建、
# 且把 deepAgent 运行时与验证器工具链都烘焙进去。
#
# 镜像 = 两份已验证镜像的并集，两者都按摘要钉死：
#
#   * harbor:443/t-bench/agentlab-deepagent —— deepAgent 运行时：
#       python3.12（dcode 要求 ≥3.12）、node 24（façade 要求 ≥18）、
#       deepagents-code 0.1.78 + deepagents 0.7.19、uv、
#       Harbor ACP 运行器预置（/opt/harbor-acp-tools/uv、/opt/harbor-acp-venv、
#       /opt/harbor-acp-python），apt 源已清空使 ACP 自举的 apt 步骤成为空操作。
#   * harbor:443/t-bench/base-pilot —— terminal-bench 验证器工具链：
#       /usr/local/bin/uv、/opt/wheels（pytest 闭包，钉 9.1.1）、
#       /opt/verifier/pyproject.toml（离线解析工程）。
#
# base-pilot 自带的 node24 / npm 缓存对 deepAgent 无用（dcode 是 Python 包，
# 不再在试次里 npm 安装），但保留它们不影响正确性，且让镜像与 pilot 基线同源。
#
# 构建（在集群宿主上）：
#     bash images/build-agent-image.sh
# 然后把打印出来的 `harbor:443/...@sha256:...` 写回每个 task 的
# task.toml（docker_image）与 environment/Dockerfile（FROM）。

# deepAgent 运行时镜像（已验证；来源见 .lab/lab-deepagent.Dockerfile）。
FROM harbor:443/t-bench/agentlab-deepagent@sha256:89fa77f8df603cd9fb42560efb0b9a7a7ca298cc024d7651bc506b90fc4ddb20

# terminal-bench 验证器工具链：从已验证的 pilot 基础镜像里逐路径搬过来，
# 不重新解析、不联网。
COPY --from=harbor:443/t-bench/base-pilot@sha256:df100e56e0919321627d16df47edfda3438c04eb40e463c0c0e98c8dac18e3f1 /opt/wheels /opt/wheels
COPY --from=harbor:443/t-bench/base-pilot@sha256:df100e56e0919321627d16df47edfda3438c04eb40e463c0c0e98c8dac18e3f1 /opt/verifier /opt/verifier
COPY --from=harbor:443/t-bench/base-pilot@sha256:df100e56e0919321627d16df47edfda3438c04eb40e463c0c0e98c8dac18e3f1 /usr/local/bin/uv /usr/local/bin/uv

# 让这个镜像里的每一次 uv 调用都离线：静默联网会把一个密封试次挂住，而不是
# 大声失败。
ENV UV_OFFLINE=1

# 被 vendored 的任务 Dockerfile 都以 WORKDIR /app 结尾、测试硬编码 /app
# （hello-world 说的是"当前目录"），因此 agent 与 verifier 都从 /app 起跑。
WORKDIR /app

# 构建期自检：跑一遍验证器在试次里会走的**同一条**路径（同一个 pyproject、
# 同一个 wheelhouse）。在这里过不去就该在这里失败，而不是在密封试次里。
RUN cp /opt/verifier/pyproject.toml /tmp/pyproject.toml \
 && cd /tmp \
 && uv sync --offline \
 && uv run pytest --version \
 && cd / \
 && rm -rf /tmp/.venv /tmp/pyproject.toml /tmp/uv.lock

# 基座 baseline 断言的 seed（suite.yaml: baselines.ready）。
RUN mkdir -p /workspace \
 && printf 'true' > /workspace/ready
