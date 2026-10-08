# 写一个评测套件（suite）

本文面向从 PyPI 安装 `aeval-harbor`、准备写下第一个评测套件的开发者。你只需要会 Python 和
YAML；执行由 Harbor 承担，你不需要先学会 Harbor。本文只讲一件事：**怎么写出一个 `aeval`
能加载、能校验、能判分的套件。**

命令一律写成 `aeval ...`。在源码仓库里工作时，等价写法是 `.venv/bin/aeval ...`。

## 1. 这套件是什么

一个套件就是一个目录：里面装着 `suite.yaml`（判分契约）、`datasets/`（数据集引用）、
`jobs/`（Harbor job 文件）、`graders/`（版本化判分器）和 `tasks/`（任务）。它不是"跑一次评测的
脚本"，而是**评分契约**——哪些环境事实必须先成立（`baselines`）、采什么证据
（`observables`）、本套件要求哪些前置事实必须成立、否则该试次不可判（`verdict.requirements`）、由哪些层判分
（`verdict.graders`）、什么样的失败直接终裁（`veto`）、可靠性与成本怎么度量（`metrics`）、
用例从哪来（`provenance`）。Harbor 负责建沙箱、跑 agent、收奖励；`aeval` 负责让这个分数可信、
可复算。

## 2. 目录结构

出厂的最小真实套件是 `suites/e2e-hello`：一条任务，加载、组合、判分所需的文件都真实落位。

```text
suites/
├── _base/
│   └── harbor.base.yaml          # 共享约定基座：clock / ready 观测点与基线 / 固定六项
│                                 # requirements / 指标种类 / 数据集路径约定
└── e2e-hello/
    ├── suite.yaml                # 判分契约；extends 基座，只写本套件特有的事实
    ├── README.md                 # 套件自己的说明
    ├── datasets/
    │   └── local.yaml            # Harbor 数据集引用：path: tasks
    ├── jobs/
    │   └── e2e-hello.yaml        # Harbor job：job_name / n_attempts / 并发 / 后端
    ├── graders/
    │   └── hello_outcome.py      # 版本化判分器：GRADER_ID / GRADER_VERSION / LAYER
    └── tasks/
        └── hello/                # 一个任务 = 一个目录；目录名就是 task_id
            ├── task.toml         # Harbor 原生任务：环境、网络、collect 声明、provenance
            ├── instruction.md    # 给 agent 的指令（Harbor 读它）
            ├── environment/
            │   └── Dockerfile    # 镜像（digest 钉死）+ 种下 /workspace/ready
            └── tests/
                └── test.sh       # Harbor 校验器：写 /logs/verifier/reward.txt
```

三条归属规则，后面每一节都会用到：

- `suite.yaml`、`graders/`、`jobs/`、`datasets/`、`tasks/` 里的事实由**套件**负责；
- `_base/` 下的文件是共享约定；任何以 `_` 开头的路径段都会被套件发现跳过，所以真实套件不能
  放进 `_` 前缀目录；
- Harbor 原生文件（`tasks/*/task.toml`、`jobs/*.yaml`）里的环境、镜像、尝试次数、并发、
  重试、超时、egress 等事实归 **Harbor**，`suite.yaml` 不得复述（见第 6 节）。

## 3. 最小可跑套件

### 3.1 先写一份基座（一次即可）

基座放在 suites 根下的 `_base/`。它只放"每个套件都要写、且写法必须一致"的约定，因此
**不含**身份（`id`/`version`/`harbor.job`）、不含 `provenance`、不含带 `veto: true` 的判分器。

```yaml
# suites/_base/harbor.base.yaml
schema_version: 2

clock:
  mode: real

# 每个任务镜像都要在 /workspace/ready 写入 "true"（见任务 Dockerfile）。
# 它同时是"镜像/种子确实就位"的基线：没有它，套件不会开始跑。
observables:
  - { name: ready, type: string, source: "file:/workspace/ready" }
baselines:
  - id: ready
    probe: "observable:ready"
    equals: "true"

verdict:
  # 固定六项：判定"这一次试次是否可判"的口径，跨套件必须一致。
  requirements: [input_complete, agent_finished, integration_valid, render_valid, judge_finished, artifact_schema_ok]
  graders: {}

metrics:
  # 只约定 kind；k 可省略。这个块只被 aeval explain 渲染，不影响判分或报告。
  - { id: reliability, kind: pass_pow_k }

harbor:
  # 套件目录内的数据集引用约定；具体选哪个 job 由套件自己声明。
  dataset: datasets/local.yaml
```

### 3.2 再写自己的 `suite.yaml`

```yaml
# suites/hello-suite/suite.yaml
schema_version: 2
id: hello-suite
version: 0.1.0

# 时钟、ready 观测点与基线、六项 requirements、指标种类、数据集路径都来自基座；
# 下面只写本套件特有的事实。
extends: _base/harbor.base.yaml

harbor:
  # job 选择是套件身份的一部分，永不由基座提供。
  job: jobs/hello-suite.yaml

driver:
  # 本套件的任务真正需要 agent 提供的两项能力：ACP 通道 + 能在沙箱里跑命令。
  # 这是"是否接受某个 agent"的显式门槛：能力对不上，配对在跑之前就被拒。
  require: [acp_stdio, shell]
  # 会话记录槽位：本套件按 dsh_session 采集证据；换 agent 家族要连同这里一起改。
  session_record: dsh_session

observables:
  # 目前只有 file: 源可用；db:/screenshot:/dom: 会明确失败，不会伪造取值。
  - { name: result, type: string, source: "file:/workspace/result" }

verdict:
  graders:
    default: { impl: "graders/hello_outcome.py@v1", layer: outcome }

metrics:
  # 同一个 id 覆盖基座的 kind 声明。这个块只被 aeval explain 渲染，不参与判分或
  # 报告：报告里的 pass^k 由 aeval report --k 显式给，编辑这里不会改变报告数字。
  - { id: reliability, kind: pass_pow_k, k: 5 }

# 套件必须自己声明来源与许可（不给默认值，也不静默继承基座的）。
provenance: { source: authored-internally, license: MIT }
```

### 3.3 字段逐条解释

| 字段 | 谁声明 | 为什么存在 | 不写会怎样 |
| --- | --- | --- | --- |
| `schema_version` | 套件 | 覆盖层格式版本 | 加载器只接受 `2`；缺或错直接拒绝 |
| `id` | 套件，**永不继承** | 套件身份，`aeval list` 与选择靠它 | 缺则拒绝；重复身份由 `aeval list` 拒绝（`probe` / `explain` / `check` / `job` / `run` 不查重） |
| `version` | 套件，**永不继承** | 准入规则、任务集或判分口径变了就进位 | 缺则拒绝；不进位会让两次配置不同的运行被当成同一次 |
| `extends` | 套件 | 把跨套件必须一致的约定交给基座 | 缺了就得自己写 `clock`/`observables`/`baselines`/`requirements`/`dataset` |
| `harbor.dataset` | **继承**基座（`datasets/local.yaml`） | 指向套件目录内的数据集引用 | 套件和基座都没写则拒绝 |
| `harbor.job` | 套件，**永不继承** | 决定"跑哪一个 job" | 缺则拒绝（身份必须落在套件自己的文件里） |
| `driver.require` | 套件 | 本套件对 agent 的能力门槛 | 可省略（默认空，等于不设门槛） |
| `driver.session_record` | 套件（默认 `dsh_session`） | 本套件采集会话记录的槽位 | 可省略取默认；换 agent 家族必须显式改，否则配对失败 |
| `observables` | `result` 由套件声明，`ready` **继承** | 声明式证据来源 | 一个都没有则拒绝 |
| `baselines` | **继承**（`ready` 断言 `/workspace/ready == "true"`） | 证明环境/种子真的就位 | 一个都没有则拒绝 |
| `verdict.requirements` | **继承**（固定六项） | **门**：这里没列到的位不参与判定，列到而没成立的位 ⇒ 该试次 `cannot_judge`（见[指标语义](metric-semantics.md)） | 空则拒绝 |
| `verdict.graders` | 套件 | 判分层与版本 | 一个都没有则拒绝 |
| `metrics` | `kind` **继承**，`k` 由套件声明 | 声明要报告哪些指标口径 | 可省略（默认空列表）；这个块只被 `aeval explain` 渲染，判分与 `aeval report` 都不读它（pass^k 的 `k` 由 `aeval report --k` 给） |
| `provenance` | 套件 | 数据来源与许可 | **默认不继承**；自己写或写 `provenance: inherit`，否则拒绝 |

### 3.4 它引用的文件也要在

`suite.yaml` 只是契约；组合阶段会真的去读它引用的文件，缺一个就报错。

```yaml
# suites/hello-suite/datasets/local.yaml
path: tasks
```

```yaml
# suites/hello-suite/jobs/hello-suite.yaml
job_name: hello-suite
n_attempts: 5            # 试次次数；报告里 pass^k 的 k 由 aeval report --k 指定
n_concurrent_trials: 1   # 并发是 Harbor 侧事实，写在这里
agents:
  - name: nop            # 占位：真实评测时用 --agent 覆盖成已声明的适配器
environment:
  type: e2b              # 后端类型是 Harbor 侧选择，按你的运行环境改
```

`tasks/<task_id>/` 是 Harbor 原生任务目录：必须有 `environment/` 和 `task.toml`，目录名就是
task_id。判分器模块要自证身份——`GRADER_ID` 与 `GRADER_VERSION` 必须是非空字符串，入口
`grade` 必须是协程函数，模块自己声明了 `LAYER` 时还要与套件里的 `layer` 一致。`impl` 里
`@v1` 的版本后缀会被解析出来，但加载器不会拿它与模块的 `GRADER_VERSION` 比对，把两边写成
同一个版本只是约定：

```python
# suites/hello-suite/graders/hello_outcome.py（节选）
GRADER_ID = "hello-outcome"
GRADER_VERSION = "v1"
LAYER = "outcome"
REQUIRED_FIELDS = ["events", "token_usage"]
```

## 4. 字段参考

下面是套件可以声明的主要键（`image` 的说明见本节末尾）。`harbor` 是唯一一个"指向 Harbor 原生
文件"的键，其余都是 `aeval` 自己的事实。

| 键 | 必填 | 类型 | 含义 | 说明或默认 |
| --- | --- | --- | --- | --- |
| `schema_version` | 是 | int | 覆盖层 schema 版本 | 只能是 `2` |
| `id` | 是 | str | 套件身份 | 永不继承；同一 suites 根内不得重复（查重只发生在 `aeval list`） |
| `version` | 是 | str | 套件版本 | 永不继承；任务集或准入规则变更须进位 |
| `extends` | 否 | str 或 list[str] | 继承的基座文件 | 按 suites 根解析；禁止 `..`；基座不得名为 `suite.yaml`；左→右后者胜；禁止环、深度上限 4 |
| `harbor` | 是 | mapping | 指向 Harbor 原生声明 | 至少要有 `dataset` 与 `job`（可分别来自基座与套件） |
| `harbor.job` | 是 | str | 本套件的 Harbor job 文件 | 永不继承；必须是套件目录内的相对路径 |
| `harbor.dataset` | 是 | str | 数据集引用 | 基座默认 `datasets/local.yaml`；本地数据集目录里放各任务目录 |
| `driver.require` | 否 | list[str] | 本套件要求 agent 提供的能力 | 默认空；与适配器声明的能力取交集，缺一即拒绝配对；与基座并集去重 |
| `driver.session_record` | 否 | str | 会话记录槽位 | 默认 `dsh_session`；必须与所选适配器的会话记录槽位一致 |
| `driver.stage_tests_before_collect` | 否 | bool | 在 agent 结束后、证据采集之前把任务的 `tests/` 暂存进沙箱 | 默认 `false`；套件的 collect 命令要跑任务自带校验器并读它发布的 reward 时打开（Harbor 默认到验证阶段才上传 `tests/`，那时采集已经结束） |
| `driver.workspace_dir` | 否 | str | agent（以及控制插件铸的会话）在沙箱里的工作目录 | 默认 `/workspace`；必须等于任务校验器假定的 cwd（通常就是 Dockerfile 的 `WORKDIR`） |
| `driver.control_options` | 否 | mapping | 交给沙箱内控制栈、按 flavor 名分命名空间的选项 | 默认空；框架不解释这些键，由注册的 flavor 自己校验，如 `dsh: {permission_mode: danger-full-access}` |
| `clock` | 是 | mapping | 时钟模式 | `mode` 为 `real` 或 `virtual_offset`（后者可带 `epoch`） |
| `observables` | 是（≥1） | list | 声明式观测点 | 每项 `name` / `type`（`string`/`number`/`boolean`/`json`）/ `source`；`source` 目前只有 `file:<沙箱内路径>` 可用 |
| `baselines` | 是（≥1） | list | 环境基线断言 | 每项 `id`，并**恰好**给一个 `probe` 或 `assert`；`probe: "observable:<名字>"` 只能探套件自己声明的观测点 |
| `verdict.requirements` | 是（≥1） | list[str] | 必须成立的前置事实；未成立 ⇒ 该试次 `cannot_judge` 并退出有效分母 | 只能是固定六项；与基座并集去重 |
| `verdict.graders` | 否 | mapping 或 list | 判分器声明 | 每项 `impl: "<路径>.py@<版本>"`（版本必填）、`layer: outcome\|trajectory\|both`、可选 `veto: true`；mapping 形式里只有 `extra` 槽位可以给列表 |
| `verdict.anchors` | 否 | str | 打开密封的评分锚点通道 | 只能写 `task_anchors`；打开后每次试次的证据包必须封入套件的 `rubric/task_anchors.json`，轨迹判分器从密封副本读锚点；默认关闭 |
| `metrics` | 否 | list | 指标声明 | 每项 `id` + `kind`（`pass_pow_k` / `cost_normalized` / `exclusion_rate`）；`k` 在任何 `kind` 上都被接受，但这个块只由 `aeval explain` 渲染，判分与 `aeval report` 都不消费它 |
| `provenance` | 是 | mapping 或 `inherit` | 数据来源与许可 | 默认不继承；`license` 取 `CC0`/`MIT`/`Apache-2.0`/`NONE_DECLARED`/`UNKNOWN`；后两者与 `data_imported: true` 不能同时出现 |
| `budget` | 否 | mapping | 单次试次的花费上限 | `max_tokens` / `max_steps` / `max_seconds` 可选；`enforcement_point` 取 `gateway_lease`/`wallclock_kill`/`none`（默认 `none`）。**上限是评测事实，归套件**（Harbor 的 job 配置里没有花费字段可让）：一旦声明，"这个 agent 能不能被计量"就成为起跑前的硬条件，`aeval run` 会拒绝记不了账的适配器（`aeval check` / `aeval job` 不查这一项） |
| `remove` | 否 | mapping | 唯一的删除方式 | 键只能是 `baselines` / `observables` / `metrics` / `graders` / `requirements` / `require`；删除目标必须存在；删完仍要满足必填约束（如 `baselines` 不能为空） |

> 另外还有一个可选顶层键 `image`（`pin`/`rebuild`）：schema 接受它，但这个构建里无法应用——
> 只要套件声明了非空的 `image:`，组合就会直接失败（收窄镜像需要已发布的 Harbor 原生任务版本）。
> 要在这一版里钉镜像，就直接钉在 `task.toml` 与 `environment/Dockerfile` 里，不要声明 `image:`。

## 5. 任务与判分器

### 任务

`tasks/<task_id>/` 的任务事实全部由 Harbor 读：

- `instruction.md` 是给 agent 的唯一自然语言指令；
- `task.toml` 声明环境、网络、`[[verifier.collect]]` 采集命令以及任务自己的 `provenance`；
- `environment/Dockerfile` 决定镜像；出厂套件把镜像按 digest 钉死在 `FROM` 行，并在构建期种下
  基线需要的文件（如 `/workspace/ready`）；
- `tests/` 是 Harbor 校验器，运行在验证阶段，必须把奖励写进 `/logs/verifier/reward.txt`。

任务目录的发现是平铺的：套件数据集目录下每个带 `task.toml` 且含 `environment/` 的子目录都是
一个任务（目录名即 task_id）。任务自带的校验器在验证阶段发布 reward，套件可以把
`file:/logs/verifier/reward.txt` 声明成观测点，让分数有据可查。

### 判分器

判分器按 `impl: "graders/foo.py@v1"` 引用：路径相对套件目录，`@版本` 不可省。组合阶段会检查
文件确实存在、必须以 `.py` 结尾、必须带版本。运行阶段模块还要自证身份：`GRADER_ID` 与
`GRADER_VERSION` 必须是非空字符串，入口 `grade` 必须是协程函数，模块自己声明了 `LAYER` 时它
必须与套件里的 `layer` 一致——任一不满足，判分器就不会被执行。`impl` 里的 `@版本` 只被解析、
不被比对，写成别的版本号不会让加载失败；两边保持一致是你应当遵守的约定，不是加载器强制的身份。

`layer` 决定判什么：

- `outcome`：判任务结果（内容地址、任务校验器发布的 reward）；
- `trajectory`：判 agent 的轨迹（会话质量、安全红线等）；
- `both`：两层都要。

`veto: true` 表示**终裁权**：这个判分器一旦判 `fail`，该次试次的终裁就是 `fail`，即使 outcome
层通过。终裁顺序是：基础设施无效的记录不进入判分 → 任一 veto 判 fail 直接终裁 `fail` → 否则折叠
各层结果（有判不了的就是 `cannot_judge`，全部通过才是 `pass`）。证据不完整或降级时判分器应返回
"判不了"而不是伪装成 0 分；`REQUIRED_FIELDS` 声明它判分所需的轨迹完备字段，字段缺失或降级时
返回"判不了"。

判分的详细语义（阈值折叠、成本归一、排除率）见 [指标语义](metric-semantics.md)。

## 6. 继承与合并

继承是用户最容易搞错的部分。合并是**逐字段声明**的，不是通用深合并。

### 6.1 合并规则

| 形状 | 语义 |
| --- | --- |
| 标量、映射 | 子覆盖父；映射**递归**合并 |
| `baselines`（按 `id`）、`observables`（按 `name`）、`metrics`（按 `id`） | 同键替换，新键追加，顺序稳定 |
| `verdict.requirements`、`driver.require` | 并集 + 去重（追加而不丢） |
| `verdict.graders` | 追加/覆盖 |
| `remove` | 唯一的删除方式，目标必须存在 |
| `id`、`version`、`harbor.job` | **永不继承** |
| `provenance` | 默认**不继承** |
| `extends` | 字符串或列表；左→右，后者胜；子套件胜过所有基座 |

### 6.2 五条必须记住的规则

1. **带 `veto: true` 的判分器不能被继承。** 它必须由套件自己声明，否则加载直接失败——veto 改变
   "什么叫通过"，那是套件自己的判定策略，不能由基座替你决定。想取消一个继承来的 veto 判分器，
   用 `remove.graders` 显式删掉。
2. **身份与 job 永不继承。** `id`、`version`、`harbor.job` 决定"跑的是哪一个套件"，基座里出现
   它们就是错误。
3. **`provenance` 默认不继承。** 静默继承别人的许可证声明等于洗白来源。要么套件自己写，要么显式
   写 `provenance: inherit`（此时被继承的基座必须确实声明了 provenance）。
4. **`remove` 是唯一的删除方式**，且删除目标必须存在：写错名字是错误，不是静默无操作。可删的段
   是 `baselines` / `observables` / `metrics` / `graders` / `requirements` / `require`。删除要成对
   做——删掉 `observables` 里的 `ready` 却留着探它的基线，会在运行期变成基线失败。
5. **身份是整链指纹。** 解析后的覆盖层加上链上每个文件的摘要共同构成套件的运行身份；改基座会
   改变所有依赖它的套件的身份。所以基座文件也必须提交，不能是未提交或未被版本管理的工作副本。

```yaml
# 删除示例（目标必须真实存在，删完仍要满足必填约束）
remove:
  metrics: [reliability]          # 键控列表：按 id 删；删光也只是变成空列表
  requirements: [render_valid]    # 并集列表：按名字删，删完仍至少留一项
  # 想连基座的 ready 观测点一起删，就得自己再声明至少一个 baselines 条目
  # （baselines 不能为空），同时别留下还在探它的基线：
  # observables: [ready]
  # baselines: [ready]
```

### 6.3 路径与结构约束

- `extends` 的路径按 **suites 根**解析：向上找最近的名为 `suites` 的祖先目录，找不到就用套件目录
  的上一级。所以基座通常放在 `<suites 根>/_base/` 下，与套件目录同级；路径**禁止 `..`**，也不能是
  绝对路径；
- 基座放在 `_base/` 这类目录下，**不得命名为 `suite.yaml`**（否则套件发现会把它当成一个套件）；
- 继承链出现环、或深度超过 4 层，直接拒绝；
- 任何以 `_` 开头的路径段都会被套件发现跳过，所以真实套件不能放在 `_` 前缀目录里；
- 基座本身也可以 `extends` 另一个基座，语义完全相同。

### 6.4 哪些事实不许写进 `suite.yaml`

Harbor 已经负责的事实写进覆盖层就是两处真相，两边迟早漂移，所以加载器把它当错误而不是合并。
基座头部与继承层的约定口径列出的这批事实一律留在 Harbor 的 task/job 文件里：

| 事实 | 写在哪 |
| --- | --- |
| 环境与镜像（`environment`、Dockerfile、镜像引用） | `tasks/*/task.toml`、`environment/Dockerfile` |
| 尝试次数与 k（`n_attempts`、`attempts`、`trials`、`k`） | `jobs/*.yaml` |
| 并发、重试、超时（`n_concurrent_trials`、`parallel`、`retry`、`timeout`） | `jobs/*.yaml` |
| egress 与网络策略（`egress`、`network_mode`） | `task.toml` |
| 挂载与 mock（`writable_roots`、`mocks`） | `task.toml` |

**例外是花费上限 `budget`**：Harbor 的 job 配置里没有可让的花费字段，所以它是评测事实，
写在套件自己的 `suite.yaml` 里（见第 4 节）。别把两边的字段名搞混：适配器声明的是它的记账
能力 `budget_enforcement`（类属性 `BUDGET_ENFORCEMENT`）；`enforcement_point` 是套件 `budget:`
块里的字段（也是试次记录里 `budget` 快照的字段），记录时由适配器的记账能力填入。

代码强制拒绝的正是上面这批顶层键（`environment`、`egress`、`writable_roots`、`mocks`、`trials`、
`tasks`、`parallel`、`k`、`attempts`、`retry`、`timeout`），而且雨露均沾：**基座也过同一道检查**，
不能成为复述 Harbor 事实的后门。同理，任务选择只能出现在 `harbor.dataset`，Harbor job 文件里
再写一遍 `tasks`/`datasets` 也会被拒；反过来，Harbor job 文件里出现 `baselines`/`clock`/
`observables`/`verdict` 这些 aeval 侧事实同样被拒。

为什么这么严：镜像、尝试次数、并发这些是**引擎的输入**，改它们会改变"这次运行是什么"。让它们
在套件里各写一份，就会出现同一份分数对应两种配置的情况，而密封证据与可比性检查都看不出差别。

## 7. 校验与运行

按这个顺序走，把能在更便宜阶段发现的问题提前拦住——能力与会话记录槽位不匹配在 `check` 阶段
（建沙箱之前）就会被拒；预算与记账能力不匹配则要到 `run` 才拒跑，`aeval check` 与 `aeval job`
对这类配对仍会打印 OK。

```bash
# 1) 加载 + 组合：不跑 agent、不拉数据集，只验证声明
aeval probe --suite suites/hello-suite

# 2) 看解析后的完整事实与来源链（注意：套件目录是位置参数）
aeval explain suites/hello-suite

# 3) 只读地判断 (套件, agent) 配对能不能跑（组合门 + 运行镜像表；预算门在 run）
aeval check --suite suites/hello-suite --agent my-agent

# 4) 校验 + 组合 Harbor job，然后委托 Harbor 真正执行
aeval run --suite suites/hello-suite --run-dir /tmp/hello-run-1 --store /tmp/hello-store.sqlite3
```

`aeval probe` 健康输出（摘要值这里用省略号代替）：

```text
suite hello-suite v0.1.0 overlay-digest=……… chain-digest=……… extends=['_base/harbor.base.yaml'] task-references=1 remote-datasets=0
Configuration validated; the selected agent's declared capabilities were checked. Remote task content and live runtime capabilities are not probed.
```

它同时打印两个指纹：`overlay-digest` 是 `suite.yaml` 自己的字节摘要，`chain-digest` 是整条继承链
的摘要。带 `extends` 时，可比性看后者；`task-references` 应等于你声明的任务数。

`aeval explain` 打印只读的组合视图（产物，不是输入）：身份与两个指纹、Harbor dataset/job、
`clock`、`driver.require`、`provenance`、来源链（角色 / 路径 / 摘要）、基线、观测点、判分器
（含 `layer` 与 `veto`）、指标，最后附上组合出来的 Harbor job JSON。来源链里应当能看到
`[base] _base/harbor.base.yaml` 与 `[child] <你的套件>/suite.yaml` 两条。

`aeval check` 成功时先打印 `pairing my-agent × hello-suite: composed (impl=…, attempts=…)`；若该
agent 声明了运行时，还会逐项打印镜像检查结果，末尾是 `runtime image check: OK`，没有声明运行时
则打印 `runtime: none declared — nothing to host, nothing to check`。失败时打印
`pairing my-agent × hello-suite: REFUSED` 与原因，退出码 3，且不构建任何东西。

`aeval run` 的硬性前置条件（都不满足就退出码 3，不触 Harbor）：

- `--run-dir` 必须是一个**不存在**的新目录（运行目录永不复用），且不能在套件目录内部；
- `--store` 是必填的（SQLite 路径）；`--agent` 省略时用 job 文件自己的 agent 条目；
- 套件目录与**所有**被继承的基座文件都必须在 Git 里已提交、且没有未提交改动；套件目录里也不能有
  被忽略却未纳入版本管理的文件；
- 选中的 agent 声明的能力必须覆盖 `driver.require`，会话记录槽位必须与 `driver.session_record`
  一致；
- 若套件声明了 `budget:` 上限，所选适配器必须声明 `budget_enforcement: gateway_lease`，否则拒跑
  （唯一的例外是显式传 `--accept-unmetered-budget`，把"这笔账记不了"写进运行清单）。

它写出 `harbor-job.json` 与 `runtime_lock.json`，然后委托
`harbor run --config <run-dir>/harbor-job.json --plugin aeval.hooks:AevalPlugin` 执行，最后封存
（数字以实际运行为准）：

```text
run run-hello-run-1 sealed: 5 trial(s) recorded, 12 file(s) attested, recompute passed
```

退出码：`0` 成功；`2` 参数错误；`3` 校验/配对/封存前置不满足；`4` 系统错误；`5` **Harbor 自己退出
0，但评测链并未完成**（试次缺失、证据没封上、复算失败等）。`5` 是"进程成功 ≠ 评测成功"的那道门。

另外两条常用命令：

```bash
aeval list --suites-dir suites                 # 列出发现到的套件（id / 版本 / 目录）
aeval job --suite suites/hello-suite --agent my-agent   # 只看组合出来的 job，不执行
```

**带 `extends` 的套件不能用 `aeval import` / `aeval export`。** 这两个命令只搬运套件目录，会静默
丢掉基座。要传输就把基座搬进套件目录，或按 `aeval explain` 的解析结果把事实内联。

## 8. 共享用例库 `cases/`

`cases/` 是一个**库**，不属于任何套件：terminal-bench 类、服务类、会话类套件都只是它的消费方。
用例清单的唯一事实源是各类别 checker 里的 `CASES` 表；套件只声明自己要用哪些类别，生成器据此把
任务物化进套件的数据集目录。用例库与生成器随源码分发包提供（常规安装只装 `aeval` 包本身），所以
这条路适用于在源码树里工作的场景。

```yaml
# suites/<你的套件>/cases.yaml
categories: [health, chat, error]   # 要注入的类别
# overrides:
#   collect_command: "mkdir -p /logs/verifier; bash /tests/test.sh; aeval-collect …"
```

```bash
python cases/generate.py --suite suites/<你的套件>            # 注入
python cases/generate.py --suite suites/<你的套件> --check    # 漂移检测
```

为什么要解耦：**用例事实与判分契约分开**。同一批服务用例可以进服务基准全量套件，也可以进某个
混合套件；一个任务算不算过、由哪些层判，始终是消费套件 `suite.yaml` 里的判分契约。注入产物是
任务目录（`instruction.md` / `task.toml` / `Dockerfile` / `tests/`），`datasets/local.yaml` 的
`path: tasks` 约定不变。

所有权规则：套件 `tasks/` 下目录名以 `<库类别>.` 开头的任务归生成器所有（重新注入先删后写，
反选类别后残留的也会被清掉）；不带该前缀的任务是套件自己的，生成器永不触碰。任务集变了，
消费套件应当升版本号。

## 9. 常见错误

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 加载报错：声明复述了 Harbor 侧事实 | 在 `suite.yaml` 里写了 `environment` / `trials` / `parallel` / `attempts` / `retry` / `timeout` / `egress` 等 | 把这些写进 `task.toml` / `jobs/*.yaml`；套件只保留自己的事实 |
| 基座加载就报错 | 基座里声明了 `id` / `version` / `harbor.job` | 把身份与 job 移回套件自己的 `suite.yaml` |
| 报"继承了带 veto 的判分器" | 基座里放了 `veto: true` 的判分器 | 在套件里自己声明它（或 `remove.graders` 删掉） |
| 报"没有声明 provenance" | 套件没写，而基座不默认继承 | 自己写 `provenance: {...}`，或显式写 `provenance: inherit` |
| `aeval list` 里看不到自己的套件 | 套件放在 `_` 前缀目录里 | 移到不带 `_` 前缀的目录 |
| 报"基座未找到"或路径越界 | `extends` 里用了 `..`、绝对路径，或路径不是按 suites 根写的 | 改成按 suites 根解析的相对路径，如 `_base/harbor.base.yaml` |
| 报"基座不得命名为 suite.yaml" | 把共享文件命名成了 `suite.yaml` | 改名，如 `_base/harbor.base.yaml` |
| 报 `remove` 目标不存在 / 该段不可删 | 删的名字写错，或删了 `harbor` 这类不可删的段 | 只删 `baselines` / `observables` / `metrics` / `graders` / `requirements` / `require`，且名字必须与继承来的完全一致 |
| `extends` 报环或层数过深 | 基座之间互相继承成环，或链超过 4 层 | 拆平继承关系；一个基座尽量只做一层约定 |
| `aeval import` / `export` 拒绝执行 | 套件带 `extends` | 把基座搬进套件目录，或内联解析结果 |
| 报判分器路径/版本不合法 | `impl` 没写 `@版本`、不以 `.py` 结尾，或文件不存在 | 写成 `graders/<名字>.py@<版本>`，并确保文件真的在套件目录里 |
| 判分器加载被拒 | 模块缺非空字符串的 `GRADER_ID` / `GRADER_VERSION`、`grade` 不是协程函数，或模块声明的 `LAYER` 与套件的 `layer` 不一致 | 补上两个非空字符串、把 `grade` 写成 `async def`，并让 `LAYER` 与 `suite.yaml` 的 `layer` 一致（版本号写得不一致不会报错，但仍应保持同步） |
| 基线失败：探了未声明的观测点 | `probe` 写了套件没声明的观测点名字，或用了 `db:` | 只在 `observables` 里声明后用 `probe: "observable:<名字>"`；`source` 用 `file:` |
| 观测点宣告不支持 | `source` 用了 `db:` / `screenshot:` / `dom:` | 改成 `file:<沙箱内路径>`，并让任务把结果写进该文件 |
| 配对被拒：能力不匹配 | `driver.require` 要求的能力，所选 agent 没声明 | 改 `driver.require` 为任务真正需要的能力，或换一个能提供这些能力的 agent |
| 配对被拒：会话记录槽位不一致 | `driver.session_record` 与适配器产出的槽位不同 | 把两者改成同一个槽位（本套件适配器产出什么，就声明什么） |
| 组合报错：任务选择重复 | `harbor.dataset` 和 Harbor job 文件里都写了 `tasks`/`datasets` | 任务选择只留在 `harbor.dataset` |
| `aeval run` 拒绝运行目录 | `--run-dir` 已存在，或位于套件目录内部 | 每次运行用一个全新目录，且放在套件目录外 |
| `aeval run` 拒绝来源 | 套件或某个基座有未提交改动 / 未纳入版本管理的忽略文件 | 先把套件与整条继承链提交 |
| `aeval list` 拒绝重复身份 | 两个目录用了同一个 `id` | `id` 必须唯一；不同版本也不行 |
| 退出码 5，但 Harbor 没报错 | Harbor 退出 0，而评测链没完成（试次缺失、证据未封存、复算失败） | 看 `run incomplete:` 后面的原因；不要拿退出码 0 当评测通过 |

## 10. 下一步

- 想让另一个 agent 来跑你的套件：[接入新 agent](adding-an-agent.md)
- 想弄清判分语义（阈值折叠、成本归一、排除率、veto 终裁）：[指标语义](metric-semantics.md)
- 仓库总览、CLI 一览与自带套件清单：[仓库 README](../../README.md)