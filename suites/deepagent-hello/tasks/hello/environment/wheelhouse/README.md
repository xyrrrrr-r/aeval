# wheelhouse

离线安装源：deepagent 任务镜像在构建期从这里装 `deepagents-code==<钉版>`。
本 lab 主机**无法访问 PyPI**（`registry.npmjs.org` 可达、内网 docker registry
可达），而沙箱的出网白名单只放行 broker 主机（façade → broker），所以镜像必须
在构建期就把 CLI 装好，而构建期又要能在**不出网**的前提下拿到全部依赖。

## 生成（在有 PyPI 的机器上，如开发机）

    python3 tools/fetch_deepagent_wheelhouse.py \
        --dest suites/deepagent-budget/tasks/hello/environment/wheelhouse \
        --also suites/deepagent-hello/tasks/hello/environment/wheelhouse

脚本用 uv（即 `uvx` 同一解析器）按镜像平台（lab 为 aarch64）解析出完整钉版
闭包，再逐包取 wheel；无 wheel 的包取 sdist，由镜像构建期的 pip 在隔离环境里
就地构建（当前只有 `forbiddenfruit`，纯 Python，`FFRUIT_EXTENSION` 默认关闭，
因此镜像不需要编译器）。`pins.txt` 记录本次解析结果，便于审计。

## 为什么不是 `uvx`

`uvx deepagents-code==…` 会在每次 agent setup 时向 PyPI 取包 —— 在无网沙箱里
必然失败。镜像内预装 + Harbor 的 `local` 分发入口把这条网络依赖彻底去掉，
版本钉在适配器常量与镜像两处，由 `tests/integration/test_deepagent_budget_suite.py`
的一致性用例守住。

## 注意

`*.whl` 与 `*.tar.gz` 不入库（见 `.gitignore`）：它们是可重建的实验室产物，
大小约 73 MiB。全新 clone 若没有执行上面的生成脚本，镜像构建会在
`pip install --no-index` 处报 “no matching distribution” —— 这是有意的显式失败。
