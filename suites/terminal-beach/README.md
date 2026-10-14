# terminal-beach —— terminal-bench 任务 × deepAgent

同三个 terminal-bench 任务（`hello-world` / `openssl-selfsigned-cert` /
`sqlite-db-truncate`，逐字节保留上游指令、测试与解法），但**驱动家族不同**：
被测 agent 是 deepAgent（`deepagents-code` CLI，走 ACP stdio），不是 DSH。
`suites/tbench-pilot` 是同一批任务在 DSH 上的对照套件。

## 这套件与 tbench-pilot 的差别

| 维度 | tbench-pilot | terminal-beach |
| --- | --- | --- |
| 被测 agent | DSH（ACP + `sdk_jsonrpc`） | deepAgent `dcode`（ACP stdio） |
| `driver.require` | `[acp_stdio, shell]` | `[acp_stdio, shell, file_tools]` |
| 会话记录槽位 | `dsh_session` | `agent_session_record`（ACP runner 的 `acp-summary.json`） |
| 计量 | DSH 自己的控制栈 | 沙箱内 **façade** → broker gateway lease（`budget_enforcement: gateway_lease`） |
| 模型线协议 | chat completions | **OpenAI Responses**（dcode 的 codex profile；CLI 侧模型标识必须是 `openai:<lease 名>`，见下） |
| CLI 来源 | 试次里 `npm install --global`（镜像烘焙 npm 缓存） | **烘焙进任务镜像**（`deepagents-code==0.1.78`） |
| 镜像 | `t-bench/base-pilot` | `t-bench/terminal-beach-agent`（base-pilot + deepAgent 运行时） |

判分层完全一致：outcome 层是上游 verifier 的 `reward`；trajectory 层是九项
守卫（`verdict.guards`），其中 `forbidden_access` / `scope_discipline` 是
integrity 门，违规即 veto。**范围根加了 `/app`**：本套件声明的
`driver.workspace_dir` 是 `/app`，预设的 scratch 根不含它，不改的话每一次
合法的工作区写入都会被记成越界违规。

## 目录

```
terminal-beach/
├── suite.yaml                     # 判分契约（能力门、证据槽位、守卫、预算）
├── budgets.yaml                   # 试次预算 + 上游线协议的唯一声明
├── datasets/local.yaml            # 数据集引用：path: tasks
├── jobs/terminal-beach.yaml       # 真实臂：3 任务 × 3 次，e2b，串行
├── jobs/terminal-beach-oracle.yaml# 可解性基线臂：oracle agent
├── families/terminal-beach.yaml   # 指标族（分组 + 显示名）
├── graders/tb_outcome.py          # outcome 层：上游 reward（按内容寻址判分）
├── graders/tb_trajectory.py       # trajectory 层：九项指标库（v5 A）
├── images/agent-image.Dockerfile  # 共享运行时基础镜像配方（集群宿主离线构建）
├── images/build-agent-image.sh    # 构建两级镜像 + 推送 + 写回摘要
├── images/pin-digests.py          # 摘要唯一写入者（FROM / docker_image 两级）
├── tools/gen_broker_spec.py       # budgets.yaml → 操作侧 broker spec
├── tools/token_count.py           # 可信输入 token 计数器（broker 的上界依据）
├── adaptation/setup-uv-pytest.sh  # 验证器离线解析脚本（上游调用的替身）
├── vendor/upstream/               # 上游任务副本（逐字节对照用，不执行）
└── tasks/<task>/                  # 指令 / task.toml / Dockerfile / tests / solution
```

## 跑一次

```bash
# 1) 在集群宿主上构建并推送两级镜像，摘要自动写回：
#    tasks/*/environment/Dockerfile 的 FROM  ← 共享基础镜像摘要
#    tasks/*/task.toml 的 docker_image       ← 本任务镜像摘要
bash images/build-agent-image.sh

# 2) 操作侧 broker spec（预算 + Responses 线协议一起写进去）
python3 tools/gen_broker_spec.py \
    --budgets budgets.yaml \
    --base "$BROKER_BASE" \
    --token-count-endpoint "http://127.0.0.1:8792/tokens/count" \
    --out "$AEVAL_BROKER_JSON"

# 3) 真实臂：3 任务 × 3 次；agent 由声明注入，模型身份由 broker spec 的
#    identity 决定（不传 --model；传了也要与 lease 服务的那一个名字一致）
aeval run --suite suites/terminal-beach --agent deepagent \
    --agents-dir agents --run-dir <run-dir> --store <store.sqlite3>

# 4) 判分产物
aeval report    <run-id> --store <store.sqlite3>
aeval dashboard --store <store.sqlite3> <run-id> > dashboard.html
aeval trajectory --store <store.sqlite3> <run-id> --out panels
aeval recompute <run-dir>
```

## 已知边界

- 任务镜像是**内部 registry 的摘要钉版**，不是可移植的公开镜像：本 lab 的
  自建 e2b 给不出模板构建所需的签名 URL，也没有 Docker Hub 出网。可移植的
  上游基线见 `suites/tbench-pilot/images/base-image.Dockerfile`。
- `deepAgent` 的**声明**是 `partial` token usage（ACP summary 只有服务器提供
  usage 时才带，实测 `latest_usage_update: null`）；但每次试验的 token 总量、
  单步 usage 与单步时间戳来自**沙箱内 façade 的逐调用账**
  （`agent/model_calls.jsonl`，见下），试验内实测，判分读 canonical transcript。
  账本存在时该试验的 `token_usage` 完整度为 `ok`，缺失时退回 `partial`——绝不
  把"没观测到"写成 0。

## 两个必须知道的实测事实

**1. e2b 后端把 `docker_image` 原样使用，任务自带的 `environment/Dockerfile`
不会执行。** `harbor/environments/definition.py:should_use_prebuilt_docker_image`
在 `docker_image` 已设置且没有 `--force-build` 时恒为真。所以任务输入必须烘进
**任务镜像**：`sqlite-db-truncate` 的 `/app/trunc.db` 走 `images/build-agent-image.sh`
的逐任务构建（镜像内自检 `test -s /app/trunc.db`），只在 Dockerfile 里内联而
不构建，沙箱里就是"文件不存在"——首轮真实运行正是这样失败的（见执行报告
"缺陷与修复"一节），agent 正确地报告了文件缺失并要求澄清。

**2. CLI 侧的模型标识必须带 provider 前缀。** dcode 从模型**名字**推断 provider：
lease 服务的名字是 `deepseek-chat` 时它会选 vendor provider，直接报
`MissingCredentialsError: No credentials found for provider 'deepseek'`（在任务
镜像里实测）；若操作者恰好导出了该 vendor 的 key，流量还会**绕开 façade 直达
vendor**——账面上计量、实际未计量的正是声明要防的那件事。适配器把 `--model`
渲染成 `openai:<lease 名>`（`OpenAiAcpAgent.CLI_MODEL_PROVIDER`），实测解析为
`ChatOpenAI(model='deepseek-chat', openai_api_base=<façade>, use_responses_api=True)`，
线名与 lease 一致、provider 与声明的路由一致。

## 逐调用账：token / 时长从哪里来

broker 对每次模型调用都在**宿主侧 lease 内**记账（usage、步数、预算上限），
但这份账不进密封包；本 family 驱动的 ACP 服务端也不发 `usage_update`。结果是
轨迹面板里 Token 汇总、上下文占用、时长、成本一片"不可用"——**如实报告，不是
渲染 bug**。补上这一环的是沙箱内的 façade：它是唯一同时看到 broker 计量结果与
agent 实际收到的响应的组件。

- 部署侧：`aeval` 从适配器声明 `DcodeAgent.SANDBOX_MODEL_CALL_LEDGER`
  （`/logs/agent/model_calls.jsonl`）取出路径，作为 `AEVAL_FACADE_CALL_LOG`
  注入 façade 启动环境；**没声明的适配器一个字节都不变**（不注入、不读、不写）。
- 采集侧：`aeval.hooks.collection.read_model_call_ledger` 在那个声明的路径上把
  账本从沙箱取回（Harbor 要到本钩子之后才下载 agent 日志目录，等它就太晚了），
  解析后（`aeval.hooks.model_call_ledger`）折进密封的 canonical transcript：
  `final_metrics.total_*_tokens`、`step.metrics`、`step.timestamp`，并把该试验的
  `token_usage` 完整度写成 `ok`/`partial`；同时把逐调用记录写进
  `mock_call_log.jsonl`（原始记录，逐行可复核）。
- 诚实规则（每条都是拒绝多报）：缺失/未声明 → 什么都不改；坏行跳过并记录；
  **只要有任何一个完成调用没报 usage，就不写 trial 总量**（部分和会少报）；
  单步时间戳/usage 只在**计数相等且工具名序列完全一致**的一一映射下挂载，
  否则整份步骤保持 `null`；成本线协议里没有价格，`total_cost_usd` 保持 `null`
  而不是编一个数。

## 判分层两个刻意的取舍

- `graders/tb_outcome.py` 的 `REQUIRED_FIELDS` 只要求 `events`（tbench-pilot 的
  对应判分器还要求 `token_usage`）。deepAgent 的 ACP 桥产出的 ATIF 轨迹带步骤与
  工具调用、不带逐步骤 usage，而本适配器声明 `token_usage: partial`；把"账本是否
  完整"挂在"任务结果是否可判"上，会让**已经通过的任务**变成 cannot_judge。
- `graders/tb_trajectory.py` 同样只要求 `events`，九项里 `token_efficiency`
  自行 skip（理由写进证据："sealed transcript carries no token totals"），其余
  八项照判——token 账本缺失是数据事实，不是轨迹判分的阻塞项。
