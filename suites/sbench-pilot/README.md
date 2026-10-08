# sbench-pilot —— 服务基准试点套件

源方案《Benchmark 测评指标设计方案》§2 的 16 类 148 用例中，**12 个
服务类共 103 例**的**全类别消费套件**（0.2.0 起钉钉集成与 Plan 编排
两类移出评测范围）。判分层只有 outcome 一层
（② 道：被测对象是引擎服务本身，agent 在这类用例里保持待命，用
nop agent）；会话质量/安全类用例在 `aeval-intel` 套件（① 道 +
④ 道记忆用例）。

用例本身不属于本套件：它们来自**共享用例库 `aeval/cases/`**
（terminal-bench 等其他评测集同样可以按类别消费）。本套件只是
声明了"全类别"的那个消费方。

## 结构

```
suites/sbench-pilot/
├── suite.yaml                # 判定契约：outcome-only，pass^1
├── cases.yaml                # 按类别注入声明（全 12 类 = 103 例）
├── task_titles.cases.yaml    # 注入产物：库属任务的中文显示名（报告用）
├── datasets/local.yaml       # path: tasks —— 任务注册（Harbor 发现约定）
├── jobs/sbench-smoke.yaml    # nop agent、n_attempts=1、e2b 后端
├── graders/sbench_outcome.py # 内容寻址 reward 判分器
└── tasks/<cat>.<case>/       # 注入产物（平铺，点分 id）
    ├── instruction.md        # 服务自查说明（agent 无需操作）
    ├── task.toml             # verifier collect 链 + 网络许可
    ├── environment/Dockerfile# ubuntu 钉扎 + ready seed
    └── tests/
        ├── test.sh           # exec python3 /tests/check_<cat>.py <case_id>
        └── check_<cat>.py    # 按类别生成的执行脚本（自包含）
```

任务树与显示名文件是**注入产物**：本套件的类别选择在 `cases.yaml`；
改用例改 `aeval/cases/checkers/<cat>.py` 的 CASES 表，然后
`aeval/.venv/bin/python cases/generate.py --suite suites/sbench-pilot`
重新注入（`--check` 做漂移检测）。生成器按源方案的类别用例数表
（3/10/8/6/15/8/15/9/5/12/5/8/11/8）拒绝清单漂移。

## 判定语义

* 每个用例的执行脚本在验证阶段直接探测 `ENGINE_BASE_URL` 指向的引
  擎，把检查结论写进 `/logs/verifier/reward.txt`：通过 = `1`，失败
  （含引擎不可达——部署依赖缺失，原因打印到 verifier 日志留痕）=
  `0`；判定层对 reward 做内容寻址判分。
* 源方案的 SKIPPED（依赖不满足）在本套件映射为 **fail + 原因**：
  Harbor 的 verifier 契约要求每个任务都产出 reward，无法用缺失值表
  达跳过。真正的"评测基础设施故障"仍走 aeval 的排除路径（reward
  缺失 ⇒ cannot_judge）。

## 部署假设（对齐真实引擎时改这里）

源方案描述了"测什么"但没有给出 API 形态。本套件的检查逻辑按合理
的标准形态实现，以下环境变量是部署接口：

| 变量 | 含义 | 默认 |
|---|---|---|
| `ENGINE_BASE_URL` | 被引擎的基址 | `http://engine:8080` |
| `ENGINE_TOKEN` | 商家 A 的 bearer token | 空 |
| `ENGINE_TOKEN_B` | 商家 B 的 token（跨租户用例） | 空 |
| `ENGINE_HMAC_SECRET` | HMAC 签名密钥（签名/重放用例） | `sbench-hmac-secret` |

* task.toml 的网络是 `allowlist: ["engine"]`：部署侧把 `engine` 别
  名映射到真实服务（compose network alias / hosts 注入）。
* 9 张 DDL 表、报告端点族、`/engine/*` 生命周期族的具体路径是约定
  形态；对齐时改 `tools/checkers/` 对应文件并重新生成。

## 与 148 用例全景的对应

| 落点 | 用例 |
|---|---|
| 本套件（② 道，outcome） | 12 个服务类 103 例（0.2.0 起钉钉集成/Plan 编排移出） |
| aeval-intel（① 道，质量+安全） | intelligence 11 例 → 10 个对话任务（multi-step-plan 覆盖 complexity+tool_selection 两维） |
| aeval-intel（④ 道，fork 记忆） | memory 14 例 → 14 个记忆任务（7 基础召回 + 7 安全） |
