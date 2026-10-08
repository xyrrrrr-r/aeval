# Changelog

本文件记录 aeval 的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**0.x 实验期政策**：0.x 阶段的 minor 版本允许包含破坏性变更（套件
schema、CLI 参数、存储格式），每次都会在下方 `Changed`/`Removed` 中
显式列出并附迁移说明；patch 版本只含修复。1.0.0 起严格遵循 SemVer。

## [Unreleased]

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

- 真实沙箱全链路（Linux 主机 + 自托管 ARM e2b）仍在逐层推进：控制
  插件库、DSH 运行适配、官方 session reader/stub 与 ATIF 映射已实现，
  采集/评分/封存的生产接线未完成——逐层验收步骤见
  `HARBOR_DSH_E2E_LINUX.md`。离线链路（上述 quickstart）不受影响。
- `e2b` 依赖锁定 `>=2.25.0,<2.51.0`：自托管 e2b 集群仅实现 v1
  sandbox API（详见 `pyproject.toml` 内注释）。

[Unreleased]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/compare/v0.1.1...HEAD
[0.1.1]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/compare/v0.1.0...v0.1.1
[0.1.0]: https://gitcode.com/open_kunpeng_agentic_infra/aeval/releases/tag/v0.1.0
