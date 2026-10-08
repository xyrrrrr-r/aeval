# 共享服务用例库（aeval/cases/）

源方案《Benchmark 测评指标设计方案》§2 的 **12 个服务类共 103 例**
的用例库（0.2.0 起钉钉集成与 Plan 编排两类退役移出）。它不属于任何单个评测集：terminal-bench、服务基准、会话
基准……都只是消费方。一个评测集（套件）想用哪些类别，在自己的
`cases.yaml` 里声明，生成器据此把任务注入该套件的数据集目录。

## 为什么是库而不是某个套件的目录

- **跨评测集复用是硬要求**：同一批服务用例可能进服务基准全量套件，
  也可能进某个混合评测集（terminal-bench 任务 + 若干服务类别）。
  用例清单放在任何一个消费套件里都会把"用例事实"与"某套件的判分
  契约"耦死。
- **判分契约留在套件侧**：注入的只是任务（instruction/task.toml/
  Dockerfile/test.sh/checker）；一个任务算不算过、由哪些层判，是
  消费套件 suite.yaml 的判分契约（outcome-only、质量层、安全层
  ……各套件自定）。

## 结构

```
aeval/cases/
├── README.md            # 本文件
├── generate.py          # 注入器：读消费套件 cases.yaml，按类别物化任务
└── checkers/
    ├── health.py        # 12 个类别主体 —— 用例清单唯一事实源：
    ├── chat.py          #   CASES 表 {<category>.<case>: (标题, 说明, 检查函数)}
    ├── session.py       # 改用例 = 改这里，然后对消费套件重新注入
    ├── tools.py
    ├── a2a.py
    ├── ddl.py
    ├── error.py
    ├── task_center.py
    ├── artifact.py
    ├── engine_lifecycle.py
    ├── tool_audit.py
    └── report.py
```

| 类别 | 用例数 | 类别 | 用例数 |
|---|---|---|---|
| health | 3 | error | 9 |
| chat | 10 | task_center | 12 |
| session | 8 | artifact | 5 |
| tools | 6 | engine_lifecycle | 8 |
| a2a | 8 | tool_audit | 11 |
| ddl | 15 | report | 8 |

（intelligence 11 例与 memory 14 例是会话契约用例，锚定在消费套件
的判分器里——见 aeval-intel——不属于本库。）

## 消费方式（按类别注入）

套件侧两件事：

1. `datasets/local.yaml`：`path: tasks`（Harbor 数据集注册约定，
   与其他套件相同）；
2. `cases.yaml`：声明类别（+ 可选 `overrides.collect_command` 覆盖
   默认采集链，适配非 dsh 的 agent 家族）。

```yaml
categories: [health, chat, error]   # 要哪些类别
# overrides:
#   collect_command: "mkdir -p /logs/verifier; bash /tests/test.sh; aeval-collect ..."
```

然后注入 / 漂移检测：

```sh
.venv/bin/python cases/generate.py --suite suites/<你的套件>
.venv/bin/python cases/generate.py --suite suites/<你的套件> --check
```

**所有权规则**：套件 `tasks/` 下目录名以 `<库类别>.` 开头的任务归
生成器所有——重新注入先删后写，反选类别后残留的也会被清掉（Harbor
的发现是平铺 `iterdir()`，残留会被当成任务继续发现，必须显式清理）。
不带点或点前缀不是库类别的任务（如 `hello-world`、`memory.*`）是套
件自己的，生成器永不触碰。同一规则覆盖注入产物
`task_titles.cases.yaml`（库属任务的中文显示名，标题即 CASES 里的
title，随清单封存供报告渲染）；套件自有任务的显示名放套件自己的
`task_titles.yaml`，加载时合并、套件侧优先——混合套件互不干扰。

## 执行脚本与判定语义

- 每个任务自带按类别生成的 `tests/check_<category>.py`（自包含，
  stdlib urllib + hmac）与 `tests/test.sh`；verifier 阶段直接探测
  `ENGINE_BASE_URL` 并发布 `/logs/verifier/reward.txt`：通过 = 1，
  失败 = 0。引擎不可达 = 失败 + 原因留 verifier 日志（源方案的
  SKIPPED 在"每 verifier 必产出 reward"的契约下映射为 fail+reason）。
- 部署接口（消费套件的部署侧提供）：`ENGINE_BASE_URL`（默认
  `http://engine:8080`）、`ENGINE_TOKEN`、`ENGINE_TOKEN_B`（跨租户
  用例）、`ENGINE_HMAC_SECRET`（签名/重放用例）。任务网络
  `allowlist: ["engine"]`。
- 源方案只描述"测什么"；9 张 DDL 表、报告/生命周期端点族、HMAC
  形制是按合理标准形态实现的约定，对齐真实引擎时改对应 checker 重
  新注入。
