# 快速上手：从安装到一份评估报告

> 目标：**半小时内**从零跑出第一份评估报告。本文只讲"怎么做"；
> 每节末尾指向深入文档。字段级参考见 [写一个评测套件](writing-a-suite.md)、
> 接入 agent 见 [接入一个新的 agent](adding-an-agent.md)、
> 报告怎么读见 [指标语义与判分规则](metric-semantics.md)。

## 0. 心智模型

```
aeval       声明 → 组合 → 判分 → 封存        （不亲自跑东西）
Harbor      真跑：建沙箱 / 跑 agent / 跑 verifier
控制插件     计量与证据归属                    （只有 DSH 家族需要）
```

三个概念，一一对应：

| 概念 | 是什么 | 放在哪 |
| --- | --- | --- |
| **suite** | 评什么：任务、判分器、预算 | 一个目录，`suite.yaml` 是入口 |
| **agent** | 被评谁：一条声明 + 一个适配器类 | `agents/<id>.yaml` + Python 包 |
| **run** | 一次评测的产物 | 一个 run 目录 + 一个 SQLite store |

**一条报告 = 一个套件**。跨套件聚合会被拒绝（`exit 3`）——不同实验不合并成一个分数。

---

## 1. 安装

### 1.1 aeval

```bash
pip install aeval-harbor          # 发行名 aeval-harbor；CLI 与 import 名都是 aeval
aeval --help                      # 验证
```

需要 **Python ≥ 3.12**。`harbor` 会作为依赖一起装上。

### 1.2 dsh-eval-control（**只在评 DSH agent 时需要**）

两条路都行，aeval 都找得到（不需要额外设路径）：

```bash
# A. 你自己项目里装（aeval 会查 <当前目录>/node_modules）
npm install dsh-eval-control        # Node ^22.19 或 >=24
ls node_modules/dsh-eval-control/dist/*.js | wc -l    # 应为 18

# B. 装成 DSH 插件（aeval 会查 <dsh home>/profiles/*/node_modules）
dsh plugin --profile web add dsh-eval-control
```

它随包分发**已编译的 `dist/`**，不需要你本地构建。附带四个命令：
`aeval-model-broker`、`aeval-dsh-session-reader`、`aeval-dsh-session-stub`、`aeval-dsh-control-selfcheck`。

> 装成 DSH 插件本身**不等于**能出评测报告：插件只让 DSH 可控（standalone 模式
> 什么都不挂），套件、试次、证据门、封存、报告都是 aeval 的事。见插件仓库
> README 的「装了插件之后：真正跑一次评测」。
>
> 装在别处时不必设环境变量：在操作者的 broker spec 里把 `controlRoot` 指向该包
> 即可，`brokerJs`/`controlDist`/会话读取器都由它推导。评别的 agent 则不需要它：
> 控制栈由 agent 声明的 `control_stack: dsh` 触发，框架不会把 DSH 的控制栈塞进
> 别的 agent。

### 1.3 先确认判分链路（零外部依赖）

不需要 docker / 模型 key：

```bash
git clone https://github.com/xyrrrrr-r/aeval && cd aeval
uv run python examples/offline-chain/run_offline_chain.py --no-open
```

它会产出 4 份报告 + dashboard + 轨迹面板。**能出报告说明判分链路是通的**，
之后真跑失败就只可能是环境/接线问题，排查面小很多。

---

## 2. 定制你自己的 suite

### 2.1 目录

```
suites/my-suite/
├── suite.yaml                  # 入口：套件事实
├── tasks/<task>/               # 每个任务一个目录
│   ├── task.toml               #   网络、verifier、元数据
│   ├── instruction.md          #   给 agent 的任务书
│   ├── environment/Dockerfile  #   镜像（FROM 必须 @sha256 固定）
│   └── tests/test.sh           #   Harbor verifier：写 reward.txt
├── graders/my_grader.py        # 判分器（版本化）
├── datasets/local.yaml         # 数据集指向
└── jobs/my-suite.yaml          # 试次次数、并发、环境后端
```

### 2.2 最少要写的五处

```yaml
# suite.yaml
schema_version: 2
id: my-suite
version: 0.1.0
extends: _base/harbor.base.yaml     # 时钟/基线/指标种类等环境级基座
harbor: { job: jobs/my-suite.yaml }
driver:
  require: [acp_stdio, shell]       # 大门槛：这个套件接受哪类 agent
  session_record: dsh_session       # 会话证据槽位
observables:
  - { name: result, type: string, source: "file:/workspace/result" }
verdict:
  graders:
    default: { impl: "graders/my_grader.py@v1", layer: outcome }
provenance: { source: authored-internally, license: MIT }
```

```python
# graders/my_grader.py —— 判分器必须自报身份，且与套件声明一致
GRADER_ID = "my-grader"; GRADER_VERSION = "v1"; LAYER = "outcome"
REQUIRED_FIELDS = ["events", "token_usage"]     # 轨迹不完整就不判（宁可 cannot_judge）

async def grade(record):    # 输入是密封过的 TrialRecord，只读
    ...
```

```sh
# tasks/<task>/tests/test.sh —— 必须写 reward.txt 并 exit 0
mkdir -p /logs/verifier
printf '%s\n' "$reward" > /logs/verifier/reward.txt
exit 0
```

> `reward.txt` 千万别省：只 `exit 1` 会让 trial 以 `RewardFileNotFoundError` 崩掉，
> 而不是记 0 分。

### 2.3 校验（都不需要跑 agent、不需要拉数据集）

```bash
aeval list    --suites-dir suites              # 发现套件
aeval probe   --suite suites/my-suite          # 校验声明本身
aeval explain suites/my-suite                  # 看组合后的完整事实与来源链
aeval check   --suite suites/my-suite --agent dsh   # 这个配对能不能跑
```

---

## 3. 接入你的 agent

### 3.1 一条命令拿骨架

```bash
aeval new-agent --id myagent --mode installed_cli \
    --provides "shell,file_tools" --budget none \
    --agents-dir agents --package-dir src
# → src/myagent/agent.py  +  agents/myagent.yaml
#   并打印 import path（按模块实际落点推导，例如 myagent.agent:MyagentAgent）
```

> 需要 `agents/_base/agent.base.yaml` 已在位（声明基座）。装出来的 aeval 旁边没有
> `agents/`，用随包分发的起点一次写齐：
>
> ```bash
> aeval agents --init --agents-dir agents
> # → agents/_base/agent.base.yaml + agents/_runtime/images.yaml + agents/dsh.yaml
> ```
>
> 已有文件会被拒绝覆盖，要覆盖加 `--force`。想只补基座，也可以从 aeval 仓库
> `cp -r <aeval>/agents/_base agents/_base`。
>
> 生成器打印的 import path 就是它在声明里写下的那一个。校验时把源码根放进
> `PYTHONPATH`（上例是 `PYTHONPATH=src`），否则 `aeval agents` 会报 `No module named ...`。

### 3.2 实现三件事

1. `install()` —— 把 CLI 装进沙箱，并**断言版本等于运行时锁**（不等就报 infra_invalid，而不是默默换一个 agent 判分）；
2. `run()` —— 拉起来、把任务交给它；
3. `read_trial_session()` —— 把原生会话读成规范轨迹。**骨架里这个是 `NotImplementedError`**，
   所以 `conformance` 会如实报"跳过"，不会假装通过。

### 3.3 三处必须诚实声明的事实

| 声明 | 含义 | 不诚实的后果 |
| --- | --- | --- |
| `budget_enforcement` | 你的账能不能被计量 | 声明了硬预算却记不了账 → `aeval run` 直接拒绝，除非 `--accept-unmetered` 并记录在案 |
| `provides` / `driver.require` | 能力配对 | 不匹配就在建沙箱前拒绝，而不是跑到一半炸 |
| `transcript.source` + `capabilities` | 证据从哪来、能证明什么 | 证据门不认，试次退出有效分母 |

### 3.4 校验并跑通

```bash
aeval agents                                   # 列声明，并逐个与适配器类对账
aeval conformance --agent agents/myagent.yaml --suite suites/my-suite
aeval run --suite suites/my-suite --agent myagent \
          --run-dir out/my-run --store out/my-run.sqlite3
```

---

## 4. 准备操作者输入：broker spec

真实评测里，**所有模型流量都经宿主 broker**（预算封顶 + 证据归属）。操作者要准备**一份** spec，
用环境变量指过去：

```bash
export AEVAL_BROKER_JSON=/etc/aeval/broker-spec.json
```

```jsonc
{
  "controlRoot": "/path/to/node_modules/dsh-eval-control",   // 或直接给 brokerJs
  "controlDist": "/path/to/node_modules/dsh-eval-control/dist",
  "listenPort": 8447,                 // 必须固定：配置摘要要在 broker 启动前就存在
  "listenHost": "127.0.0.1",
  "upstream": {
    "provider": "deepseek", "model": "deepseek-chat",
    "baseUrl": "https://api.deepseek.com/v1",
    "apiKeyEnv": "DEEPSEEK_API_KEY"   // 变量的名字，密钥本身永不进配置文件
  },
  "identity": { "provider": "deepseek", "model": "deepseek-chat" },
  "limits":   { "maxSteps": 60, "maxTokens": 2000000 },
  "maxOutputTokens": 16384,
  "tokenCount": { "endpoint": "http://127.0.0.1:8792/tokens/count", "margin": 8 }
}
```

三个真会绊倒人的点：

1. **`maxTokens` 必须配可信的 `tokenCount`**。硬预算的可信度等于输入上界的可信度：
   没有可信计数源时 broker 会**直接拒绝**硬预算；而指向一个会**少算**的计数器更糟——
   每次请求都会在派发后的自查里被判 `AEVAL_TOKEN_BOUND_VIOLATED`（这个坑真实发生过）。
   参考实现可直接抄 `suites/tbench-pilot/tools/token_count.py`（用请求体的字节长度作上界：
   一个 token 至少覆盖一个字节，所以永不少算）。
2. **`listenPort` 固定，且每个并发槽位一份 spec**（端口要写进摘要，不能等监听到了才知道）。
3. **密钥只以变量名出现**（`apiKeyEnv`），配置文件里不放密钥。

---

## 5. 跑一轮

```bash
aeval run --suite suites/my-suite \
          --run-dir out/my-run \
          --store   out/my-run.sqlite3 \
          --agent   myagent \
          --harbor-cli harbor \
          --sandbox-image <ref>@sha256:<digest> --sandbox-platform arm64
```

`aeval run` 会：校验 → 组合 Harbor job → **委托 Harbor 执行**（自动带上
`--plugin aeval.hooks:AevalPlugin`，你不用手写）→ 回收产物 → 判分 → 封存。

- **run_id = `"run-" + <run 目录名>`**，本例是 `run-my-run`；也记在
  `run 目录/run_manifest.json` 与 `aeval_run_summary.json` 里。
- 先用一次单任务的小套件或 `--profile` 小配置把接线验证一遍，再放大；
  接线类故障（控制栈上传、租约、会话读取）在真跑里排查代价高得多。

---

## 6. 出报告

```bash
S=out/my-run.sqlite3

aeval report     run-my-run --store $S --k 3        # Markdown 报告
aeval dashboard  run-my-run --store $S --k 3 > dashboard.html
aeval trajectory run-my-run --store $S --out panels/  # 每个 task 一个面板
aeval recompute  out/my-run                          # 接 run 目录（不是 run_id），独立复核封存
```

- **`k` 由命令行给**（`--k 3`），套件里那个 `k` 只影响 `aeval explain` 的渲染，不改报告数字。
- **`recompute` 是复核入口**：不信任渲染器，自己重算一遍封存。
  要检查一份报告有没有被改过，看它。

产物（都在 run 目录里）：

| 文件 | 内容 |
| --- | --- |
| `aeval_run_summary.json` | run 级摘要：run_id、配置哈希、套件版本、排除项 |
| `run_manifest.json` | 身份：agent/plugin/harbor 版本、`runtime_lock_digest`、`seal_digest` |
| `runtime_lock.json` | 运行时锁：被测 CLI 的 tag/commit/integrity、控制插件文件清单 |
| `attestation.json` | 封存清单（逐文件 sha256） |
| `harbor/*/result.json` | **上游 verifier** 口径：试次、异常、reward |
| `dashboard.html` · `panels/*.html` | 自包含 HTML，可直接分享 |

**两条口径不要混用**：上游 verifier 的 reward 和 aeval 的终判是两回事。
aeval 会把"轨迹质量"叠在 outcome 之上（阈值折叠、反作弊 veto），
所以完全可能 reward=1 却终判 fail。这是设计，不是 bug——详见[指标语义](metric-semantics.md)。

---

## 7. 排错速查

| 现象 | 多半是 |
| --- | --- |
| `no _base/agent.base.yaml` | 声明基座不在位，见「接入你的 agent」 |
| `conformance` 报 transcript 检查"跳过" | 你还没实现 `read_trial_session()`（骨架故意如此） |
| `pairing refused` / 没有 runtime 行 | 运行时镜像表里没有你这个 (套件, runtime) 配对：在 `agents/_runtime/images.yaml` 加一行，或显式传 `--sandbox-image` |
| `carries no controlDist and no controlRoot with a built dist/` | broker spec 既没给 `controlDist`，`controlRoot` 也没有编译好的 `dist/`。给 `controlRoot`（指向含 `dist/broker_main.js` 的包）即可，`controlDist` 会默认成 `<controlRoot>/dist` |
| `AEVAL_TOKEN_BOUND_VIOLATED` | 计数器在少算，见「准备操作者输入」第 1 点 |
| trial 崩在 `RewardFileNotFoundError` | verifier 没写 `reward.txt`，见「定制你自己的 suite」 |
| `aeval report` 报跨套件 | run_id 属于不同套件；一份报告只对一个套件 |
| 报告里证据覆盖 0/N | 采集产物没进"证据目录"，检查 collect 声明与采集清单 |

## 8. 下一步

- 字段全量参考：[写一个评测套件](writing-a-suite.md)
- 适配器细节与三条红线：[接入一个新的 agent](adding-an-agent.md)
- 状态词与数字的确切含义：[指标语义与判分规则](metric-semantics.md)
- 判分链路交互式架构图：`docs/diagrams/eval-chain.html`
