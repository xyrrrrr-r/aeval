# Changelog

本文件记录 aeval 的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**0.x 实验期政策**：0.x 阶段的 minor 版本允许包含破坏性变更（套件
schema、CLI 参数、存储格式），每次都会在下方 `Changed`/`Removed` 中
显式列出并附迁移说明；patch 版本只含修复。1.0.0 起严格遵循 SemVer。

## [Unreleased]

## [0.2.0] - 2026-10-08

### Changed

- **`README.md` 的链接改为绝对 URL**：原先 13 处相对链接（指南、图示、`CHANGELOG`、
  `LICENSE`、`pyproject.toml`、`cases/`）在 PyPI 的项目页上全部指向不存在的路径，
  现统一指向 `gitcode.com/open_kunpeng_agentic_infra/aeval/blob/main/…`，与
  `dsh-eval-control` 的 README 保持一致。注意这些链接指向 `main` 分支，只有改动合入
  `main` 之后才会生效。
- **对外文档清洗（为社区发布）**：新增三份自足的使用者指南
  [`docs/guides/writing-a-suite.md`](https://gitcode.com/open_kunpeng_agentic_infra/aeval/blob/main/docs/guides/writing-a-suite.md)、
  [`docs/guides/adding-an-agent.md`](https://gitcode.com/open_kunpeng_agentic_infra/aeval/blob/main/docs/guides/adding-an-agent.md)、
  [`docs/guides/metric-semantics.md`](https://gitcode.com/open_kunpeng_agentic_infra/aeval/blob/main/docs/guides/metric-semantics.md) 与索引
  [`docs/guides/README.md`](https://gitcode.com/open_kunpeng_agentic_infra/aeval/blob/main/docs/guides/README.md)。开发过程记录、阶段台账
  记录、环境验证报告与设计稿移入 `docs/internal/`，并**不再随 sdist 分发**
  （`[tool.hatch.build.targets.sdist] exclude = ["/docs/internal", "/ops/e2b"]`——
  前者是开发过程记录，后者是自托管 e2b 集群的内部运维脚本，两者对使用者都不可读、
  也不该随包分发）；内部资料按历史原样保留，接口说明一律以使用者指南为准。
- **套件版本进位（注释与任务正文清洗导致字节变化）**：`sbench-pilot` 0.5.0 → 0.6.0
  （任务正文与执行脚本去掉对外不可读的来源引用，任务集仍是 12 类 89 例，判分
  口径不变）；`e2e-hello` 0.2.0 → 0.3.0、`tbench-pilot` 0.2.0 → 0.4.0
  （注释清理；tbench-pilot 因试点任务 `tests/test.sh` 的注释去术语化在 0.3.0 之后
  再进一版）、`deepagent-budget` 0.1.0 → 0.2.0、`deepagent-hello` 0.1.0 → 0.2.0
  （注释清理）。**这些套件的身份摘要已变化，不与各自上一版同域可比**，按旧版本
  生成的封存 bundle 不能按原值复算（这是预期行为，下限不会被误平均）。

### Added

- **上下文窗口随密封证据自携带**：provider 声明的窗口经官方 `request/context`
  事件写入会话并密封进证据（控制面 `upstream.contextWindow` 可选正整数，
  `resolveModel` 回显 `context.contextWindow`）；ATIF mapper 将其抬进 agent 块的
  中立键 `contextWindow`（不绑 agent 名）。`aeval trajectory` 面板优先采用调用方
  `--context-window` 声明，否则回退证据自携带窗口，自动换算上下文压力占用率；
  两者不一致或缺失时如实标注、不臆造。
- **轨迹面板：五轨联动执行视图 + 批量模式**。面板重构为执行、工具、Token、
  输入 Token、上下文压力五轨共享横轴（纵向轨道只作对比、不表示并发），试次按
  各自起点的相对执行时间对齐，虚线轨道为记忆基底（fork base）；点击轨道标记
  同步高亮同一步，右侧详情检查器展示消息、工具观测与 turn 判分理由；高密度
  轨迹按密度分桶下钻。改为宽屏自适应（去掉 900px 硬底与横向滚动）。
  `aeval trajectory --out <dir>` 一次为 run 下每个 task 各写一份自包含面板
  （`<dir>/<task>.html`）。
- **对外发布面的"黑话回流"守卫**：新增
  [`tests/unit/test_public_surface.py`](https://gitcode.com/open_kunpeng_agentic_infra/aeval/blob/main/tests/unit/test_public_surface.py)，
  扫描发布面并禁止内部词汇回流——内部阶段编号、散落在正文里的裸优先级记号、内部机器名、
  内部资料归档路径、内部文档标题、设计稿的小节记号、缺陷编号与中文内部术语（确切的
  禁用清单写在该测试文件里，因此它本身不随包分发）。作用域直接取自 sdist 的 exclude
  列表加 git 忽略规则，不另立一份清单；低精度模式按文件类型收窄（裸优先级记号只在
  `.md` 里判定，架构图的 SVG 路径数据里那些 moveto 指令不该被误伤），避免误报把守卫
  自己关掉。守卫文件随 sdist 排除：禁用词清单必须写出这些词，发布面不能带上它们。
  当前发布面 768 个文本文件、8 个模式全 0 命中。
- **六项前置事实（`verdict.requirements`）成为真正的门**。套件声明的位只要有一位没置位，
  该试次就记为 `cannot_judge`（是哪几位写在记录的 `aeval.requirement_shortfall` 里），
  退出有效分母——既不当作通过，也不当作做错；判分层已产出的 grade 仍留在记录里供追溯。
  在此之前这六位只被记录：`evaluate_requirements()` 的返回值在调用处被直接丢弃，是死代码。
  现在它是决定试次位图的唯一出口，并负责在 `infra_error` 时清掉 `judge_finished`
  （死了的试次不算"判过了"）；`RequirementBitmap` 新增 `missing(required)`。
  **行为变化**：以前崩溃或被取消的试次（`agent_finished` 未置位）照样会拿到 `pass`/`fail`，
  现在会被记为 `cannot_judge` 并退出分母。`infra_invalid`（基础设施故障）优先于本门，
  不会被改写；未在 `verdict.requirements` 里声明的位只记录、不判定，共享基座默认声明
  全部六位，所以默认口径是"六位全要"。

### Fixed

- **README/demo 的套件版本与用例数对齐当前套件**：面板表与演示 docstring 的
  版本标注更新为 aeval-intel 0.4.0、tbench-pilot 0.4.0、sbench-pilot 0.6.0；
  memory 类用例数 14 → 13（6 基础 fork 召回 + 7 安全红线，与
  `task_categories.yaml` 的 `redline_tasks` 一致）；演示命令示例的 store 路径
  修正为真实产物目录（`out/<run 目录名>/store.sqlite3`，`run-` 前缀属于
  run id 而非目录名）。
- **README 版本号与实际不符**：README 写 `v0.1.0`，而发行版为 `0.1.1`。
- **README 套件表任务数过期**：`sbench-pilot` 标注"123 任务全量"，实际为
  12 类 89 例（0.5.0 起）。README 的 `0.1.0` 版本号与套件数已同步。
- **指南事实修正**：判分器模块的 `GRADER_VERSION` **不会**与 `impl: "…@vN"`
  上的版本号做比对（该后缀被解析后丢弃；实际强制的是 `GRADER_ID`／
  `GRADER_VERSION` 非空、`grade` 是协程函数、以及 `LAYER` 与声明层一致）；
  `image:`（`pin`/`rebuild`）在本版本一律拒绝组合，镜像请在 `task.toml`
  与 Dockerfile 中固定。
- **跨仓库死链**：`docs/internal/INDEX.md` 指向 `dsh-eval-control` 的相对链接
  少一层 `../`，清洗前即已失效，已修正。

## [0.1.1] - 2026-10-08

### Fixed

- **Intel macOS 上的纯 pip 安装**（`pip install aeval-harbor`）：cryptography
  ≥50 不再发布该平台 wheel，会退化为 Rust 源码构建（数十分钟，无可写用户
  缓存目录的环境直接失败）。0.1.0 的平台约束放在 `[tool.uv]` 下，只有 uv
  识别；本版将其提升为带环境标记的正式依赖
  （`cryptography<45; sys_platform=='darwin' and platform_machine=='x86_64'`），
  pip / uv / 任何 PEP 508 安装器在 Intel Mac 上都解析到 44.0.3（有
  universal2 wheel，秒装）；Linux 与 Apple Silicon 不受任何影响。

## [0.1.0] - 2026-10-08

首个公开版本（experimental）。

### Added

- **套件体系**：schema v2 套件格式（baselines 环境基线探针、虚拟时钟
  `clock: virtual_offset`、observables 声明式采集、verdict 判分契约、
  pass^k 指标、provenance 溯源）；`_base` 继承机制；到 Harbor job 的
  静态组合与只读解释视图（`explain`）；harbor-task 格式双向转换
  （`import` / `export`）；套件源内容锁定（source commit / digest）。
- **确定性判分链路**：六项 requirements 门（input_complete /
  agent_finished / integration_valid / render_valid / judge_finished /
  artifact_schema_ok）先行；grader 版本化加载（`impl@version`）；
  outcome / trajectory 分层判分；质量维度阈值折叠；security 层 veto
  终裁（reward=1 也可被推翻）；不做加权平均。
- **防篡改证据链**：采集清单绑定运行时锁（runtime lock digest）；
  逐产物 sha256 校验；证据门 `verify_evidence_bundle` fail-closed；
  密封 bundle + attestation；`recompute` 对密封 bundle 独立复算；
  `rejudge` 受保护入口——前置条件不满足即拒绝，绝不静默重判。
- **可靠性度量**：pass^k（k 次尝试次次通过）与 pass@k 并列输出。
- **存储与报告**：TrialStore（SQLite，trials + rubric_results）；
  Markdown 报告；自包含 HTML dashboard（pass^k、维度达标、红线状态、
  token/时长/工具调用统计）；单任务轨迹面板（含聚合轨道视图）。
- **agent 中立适配**：dsh / deepagent / openai_acp 三个生产适配器 +
  fakeagent（测试用）；`new-agent` 脚手架；`conformance` 一致性测试
  ——跳过的检查如实报告为 skipped，绝不谎报 passed。
- **自检**：`selftest manifest` / `selftest isolation` 故障注入——
  每类该拦的失败都必须被拦下。
- **自带套件**：tbench-pilot（terminal-bench 试点）、aeval-intel
  （会话智能 + 跨会话记忆，含红线注入任务）、sbench-pilot（服务自查
  全量）、deepagent-hello / deepagent-budget / e2e-hello（冒烟）。
- **共享用例库** `cases/`：12 个服务类别，套件经 `cases.yaml` 声明式
  注入，用例事实与判分契约解耦。
- **离线 quickstart** `examples/offline-chain/`：零外部依赖
  （无 docker / e2b / API key）一条命令跑通完整判分链路并自动打开
  dashboard；从"采集完成"切入，其后每一环都是生产代码。
- **控制面** `control/`（aeval-control）：agent 中立的宿主模型 broker
  与网关租约面，供各 agent 控制栈（如 dsh-eval-control）组合部署。

### 已知限制

- 真实沙箱全链路（Linux 主机 + 自托管 ARM e2b）：**已在真机上跑通并封存**——真实
  provider 凭据下沙箱内真实 agent 完成多步推理与工具调用，`exit 0`、
  `sealed: 1 trial(s) recorded, 34 file(s) attested, recompute passed`，终态产物
  得到计分 verdict。注意该验证是在作者自己的拓扑（自托管 e2b 集群 + 内网宿主机）
  上完成的，换环境需按自己的网络与凭据重新验证；离线链路（上述 quickstart）不受影响。
- `e2b` 依赖锁定 `>=2.25.0,<2.51.0`：自托管 e2b 集群仅实现 v1
  sandbox API（详见 `pyproject.toml` 内注释）。

[Unreleased]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/compare/v0.2.0...HEAD
[0.2.0]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/compare/v0.1.1...v0.2.0
[0.1.1]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/compare/v0.1.0...v0.1.1
[0.1.0]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/releases/tag/v0.1.0
