# aeval

**确定性、证据可密封的 agent 轨迹评测框架。**

Deterministic, evidence-sealed agent trajectory evaluation built on Harbor.
（experimental · v0.1.0 · Apache-2.0）

[![判分链路](docs/images/architecture.png)](docs/diagrams/eval-chain.html)

> 交互版架构图（含引导视图 / 明暗主题 / 导出）：[`docs/diagrams/eval-chain.html`](docs/diagrams/eval-chain.html)

aeval 把执行委托给 Harbor（PyPI `harbor[e2b]==0.23.0`），自己专注一件事：**让"分数"可信**。
requirements 门先行、再进 grader，阈值折叠、违规 veto 终裁——不做加权平均；每份采集产物
逐一 sha256 校验并绑定运行时锁，密封 bundle 可被 `recompute` 独立复算；可靠性用 pass^k
度量，而不是"跑一次过了"。

## 安装

```bash
pip install aeval-harbor    # 发行名 aeval-harbor（PyPI 上 aeval 已被停更包占用）；CLI 与 import 名仍是 aeval
```

Python ≥ 3.12 · 变更见 [CHANGELOG](CHANGELOG.md) · 语义化版本（0.x 实验期：minor 版本
可能含破坏性变更，均在 CHANGELOG 显式列出）。想先试试？直接跑下一节的离线演示，
从源码运行、无需安装。

## 30 秒体验（离线，零外部依赖）

不需要 docker / e2b / 模型 API key。仓库自带一条完整的离线判分链路演示——
从"采集完成"那一点切入，之后**每一环都是生产代码，没有一处 mock**：

```bash
git clone https://gitcode.com/open_kunpeng_agentic_infra/aeval.git && cd aeval
uv run python examples/offline-chain/run_offline_chain.py   # 跑完自动在浏览器打开 dashboard
```

> 没有 [uv](https://docs.astral.sh/uv/)：先 `curl -LsSf https://astral.sh/uv/install.sh | sh`
> （Python ≥ 3.12 它也能代管）；或传统方式 `python -m venv .venv && .venv/bin/pip install -e .`
> 再 `.venv/bin/python examples/offline-chain/run_offline_chain.py`。
> 加 `--no-open` 跳过自动打开浏览器（SSH / CI 环境打不开时也会退回打印路径）。

一次产出 4 个 run 的报告（Markdown）+ 自包含 dashboard（HTML）+ 轨迹面板，
其中会话套件 run 包含 24 任务 × 3 试次 = 72 条判定记录（pass@3 0.9998、pass^3 0.8032、
红线告警、维度达标表、token/时长/工具调用统计）。演示刻意覆盖了判分标准的每条路径：

| 路径 | 演示用例 |
|---|---|
| 干净通过 | hello-world 前两试 |
| 质量阈值折叠判 fail | hello-world 第三试（冗长/低遵循，reward=1 也翻车） |
| **反作弊 veto** | sqlite-db-truncate：agent 偷看验证器，reward=1 但 ForbiddenAccess ⇒ 终判 fail |
| 真实失败 | openssl-selfsigned-cert（证书 CN 写错，reward=0） |
| 基础设施故障排除 | 显式排除记录，不进有效分母 |

![dashboard](docs/images/dashboard-aeval-intel.png)

*`aeval dashboard` 产出的是单个自包含 HTML，可直接分享给别人。*

## 为什么用 aeval

- **确定性判分，不是 LLM 拍脑袋**——requirements 门（input_complete / agent_finished /
  integration_valid / render_valid / judge_finished / artifact_schema_ok）先行，grader
  版本化加载（`graders/outcome.py@v7`），outcome 与轨迹分层，阈值折叠，veto 终裁。
- **防篡改证据链**——采集清单绑定运行时锁，逐产物 sha256；`recompute` 对密封 bundle
  独立复算；`rejudge` 在前置条件不满足时**拒绝执行**而不是悄悄重判（fail loud）。
- **pass^k 可靠性**——k 次尝试次次通过才算数，报告同时给出 pass@k 与 pass^k。
- **可复现**——虚拟时钟（`clock: virtual_offset`）、环境基线探针（baselines）、
  observables 声明式采集。
- **agent 中立**——`new-agent` 脚手架 + `conformance` 一致性测试；已带 DSH、deepagent、
  OpenAI-ACP 三个适配器。接入新 agent 先过一致性测试，再进评测。
- **评测框架自己先被评测**——`aeval selftest` 故障注入：每类该拦的失败都必须被拦下。

## CLI 一览

| 命令 | 作用 |
|---|---|
| `aeval list` | 列出发现的套件 |
| `aeval check` | (套件, agent) 配对可行性——只读，什么都不建 |
| `aeval run` | 校验 + 组合 Harbor job 并**委托** Harbor 执行 |
| `aeval explain` | 渲染套件的只读组合视图（产物，不是输入） |
| `aeval probe` / `job` | 验证 agent 声明 / 为声明的 agent 组合 job |
| `aeval agents` / `new-agent` / `conformance` | 列出适配器 / 脚手架新 agent / 一致性验证 |
| `aeval import` / `export` | harbor-task 格式套件双向转换 |
| `aeval report` / `dashboard` / `trajectory` | Markdown 报告 / 自包含 HTML 面板 / 单任务轨迹面板 |
| `aeval recompute` | 独立复算密封 bundle（篡改即报错） |
| `aeval rejudge` | 受保护的重判入口：前置不满足即拒绝 |
| `aeval selftest manifest` / `isolation` | 故障注入自检（必须全部拦截） |

## 套件（suite）

一个套件 = 一个目录：`suite.yaml` + datasets + jobs + graders + tasks。核心是判分契约：

```yaml
schema_version: 2
id: refund-policy
version: 1.4.0
baselines:                                    # 环境基线探针：跑前先验证环境
  - { id: orders_seeded, probe: "db:SELECT count(*) FROM orders", equals: 120 }
clock: { mode: virtual_offset, epoch: "2026-09-16T00:00:00+08:00" }   # 可复现时间
observables:                                  # 声明式采集
  - { name: order_status, type: string, source: "db:orders.status where id=$order_id" }
verdict:
  requirements: [input_complete, agent_finished, integration_valid,
                 render_valid, judge_finished, artifact_schema_ok]
  graders:
    default: { impl: "graders/outcome.py@v7", layer: outcome }   # grader 版本化
metrics:
  - { id: reliability, kind: pass_pow_k, k: 5 }                  # pass^k
provenance: { source: authored-internally, license: MIT }
```

仓库自带套件（`aeval list`）：

| 套件 | 内容 |
|---|---|
| `tbench-pilot` | terminal-bench 试点（含镜像摘要锁定与来源说明） |
| `aeval-intel` | 会话智能 10 任务 + 跨会话记忆 14 任务（含红线注入：回显、密钥泄露、跨租户越权等） |
| `sbench-pilot` | 服务自查 123 任务全量（outcome-only 契约） |
| `deepagent-hello` / `deepagent-budget` / `e2e-hello` | deepagent 与端到端冒烟 |
| `_base` | 共享基座（继承源） |

服务类用例来自共享用例库 [`cases/`](cases/README.md)：12 个服务类别，套件在自己的
`cases.yaml` 里声明要注入哪些——用例事实与判分契约解耦。

## 接入新 agent

```bash
aeval new-agent my-agent          # 生成适配器骨架 + 声明（reader 故意 NotImplementedError）
# …实现你的 transcript reader…
aeval conformance my-agent        # 一致性验证：跳过的检查如实报告为 skipped，绝不谎报 passed
aeval check --suite <suite> --agent my-agent
```

## 成熟度（诚实说明）

- ✅ **离线判分链路**：任何机器可复现（见 30 秒体验），判分/密封/存储/报告全部是生产代码。
- ✅ **静态组合与验证**：`check` / `probe` / `job` / `explain` / `conformance` / `selftest`。
- 🚧 **真实沙箱全链路**：目标是 Linux 主机 + 自托管 ARM e2b 沙箱；控制面、会话读取、
  ATIF 映射已实现，采集/评分/封存的生产接线仍在推进——逐层验收步骤见
  [HARBOR_DSH_E2E_LINUX.md](HARBOR_DSH_E2E_LINUX.md)。
- 版本号 0.1.0，接口可能变化；`harbor`/`e2b` 依赖有精确锁定（原因见 [pyproject.toml](pyproject.toml) 注释）。

## 仓库结构

```
src/aeval/
├── cli.py            # 16 个顶层命令 + selftest 故障注入组
├── suite_loader/     # 套件加载 · 继承 · 组合 · 校验 · 转换
├── verdict/          # requirements 门 · grader 加载 · 轨迹指标 · 折叠 · veto
├── hooks/            # 采集器 · 证据门（verify_evidence_bundle）
├── bundle/           # 密封 · attestation · recompute
├── store/            # TrialStore（SQLite）· 产物管理
├── metrics/          # pass^k · report · dashboard · 轨迹面板
├── agents/           # 适配器契约 · conformance · dsh/deepagent/ACP/testing
└── control/          # 控制面引导 · broker · flavors
suites/               # 评测套件（见上表）
cases/                # 共享服务用例库
control/              # aeval-control：agent 中立的宿主控制面（Node）
examples/offline-chain/  # 30 秒离线演示
```

## 相关项目

- **[dsh-eval-control](https://gitcode.com/open_kunpeng_agentic_infra/dsh-eval-control)**——DSH 形态的宿主侧控制插件（实验变量注入、
  网关租约预算、fork 血统、bundle descriptor）。`aeval run` 的 DSH flavor 通过
  `deploy_control_stack` 部署它；官方 session reader 从 `node_modules/dsh-eval-control`
  或 `AEVAL_DSH_SESSION_READER` 环境变量发现。
- **`control/`（aeval-control）**——从 dsh-eval-control 抽出的 agent 中立控制面
  （host broker + gateway lease），各 agent 控制栈从这里组合部署产物。

## 开发

```bash
uv sync
.venv/bin/pytest                 # 单元 + 集成（含 hypothesis 性质测试）
.venv/bin/aeval selftest manifest   # 故障注入自检：必须全部拦截
.venv/bin/aeval selftest isolation
```

集成测试里的 DSH bridge 用例需要 PATH 上有 Node（`^22.19 || >=24`，
dsh-eval-control 的运行时）；缺失时这些用例会以 `node runtime not found` 失败。

## 发布

```bash
uv build && uvx twine check dist/*   # hatchling 锁 1.27.x——1.28+ 产出 PyPI 尚不识别的 Metadata 2.5，见 pyproject 注释
uv publish dist/*                    # 需要 PyPI token（如 UV_PUBLISH_TOKEN 环境变量）
```

## License

Apache-2.0，见 [LICENSE](LICENSE)。
