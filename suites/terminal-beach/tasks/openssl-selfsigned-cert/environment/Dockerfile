# terminal-beach 任务镜像：FROM 共享的 deepAgent 基础镜像（与其它任务同一个摘要），
# 再叠加本任务自己的 provisioning（upstream 的 Dockerfile 步骤，逐字保留）。
#
# 为什么任务镜像必须预构建并钉摘要：e2b 后端在 [environment] 设了 docker_image 时
# **直接使用该镜像**（harbor/environments/definition.py:should_use_prebuilt_docker_image
# 在未 --force-build 时恒为真），本目录的 Dockerfile 不会被执行。所以任务输入
# （例如 sqlite 的 trunc.db）必须在构建期烘进镜像，否则沙箱里就是"文件不存在"。
# 构建与钉摘要：images/build-agent-image.sh（该脚本会把下面的 FROM 摘要改成共享
# 基础镜像的最新摘要，并把本任务镜像摘要写回 task.toml 的 docker_image）。

FROM harbor:443/t-bench/terminal-beach-agent@sha256:a9ac0653d866574473a0be8f677729fd92160244040b3a02de110bfd610892e3
# Mapped from docker-compose.yaml
ENV TEST_DIR=/tests

# Restore the working directory the migration dropped
WORKDIR /app
