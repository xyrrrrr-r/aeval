# e2e-hello — 单任务全链 E2E suite 骨架

满足全部离线校验的最小真实套件：一条任务、`file:` 观测源、真实版本化 grader、
arm64 digest 钉死镜像。**离线部分已完成并全绿**；真实执行需要环境验证阶段的
e2b/DSH 前置条件（见下）。

## 布局

```
suite.yaml                    # schema 2 覆盖层；extends _base/harbor.base.yaml，
                              # 本文件只写身份 + 特有观测/grader/metric/provenance
datasets/local.yaml           # path: tasks
jobs/e2e-hello.yaml           # n_attempts=5, 顺序并发 1, agent: nop
graders/hello_outcome.py      # GRADER_ID="hello-outcome"@v1, layer=outcome
tasks/hello/task.toml         # 镜像 digest 钉死 + [[verifier.collect]] 4 固定输出
tasks/hello/instruction.md    # 指令：把 hello 写入 /workspace/result
tasks/hello/environment/Dockerfile  # FROM ubuntu@sha256:…（arm64 manifest digest）+ 基线种子
tasks/hello/tests/test.sh     # Harbor 校验器：result 必须恰为 hello
```

## 关键契约（为什么这样写）

- **镜像钉死**：`ubuntu@sha256:11dc1ccb427f0464a2369e645454c272bb0baece7357c892ba69d313b3a332cf`
  是 ubuntu:24.04 多架构 **manifest digest**（从 Docker Hub registry 实时取得），
  arm64 后端构建时解析为 arm64 镜像。task.toml 与 Dockerfile 同一 digest；
  覆盖层不使用 image narrowing（compose 明确拒绝运行时改镜像——"pin the image
  in task.toml"）。
- **基线**：`probe: observable:ready` 解析 **suite 声明**的 ObservableSpec
  （P0-2：探针不得自造目标；`db:` 源 fail-closed）。Dockerfile 在构建期种下
  `/workspace/ready` = `true`。
- **观测源只用 `file:`**：`ready` 与 `result` 均为 file 源；P0-2 之后 db:/screenshot:/dom:
  明确不支持。
- **collect 声明**：`[[verifier.collect]]` 一条命令覆盖 4 个固定证据输出
  （runtime_dump / mock_call_log / dsh_session / canonical_transcript）；
  observable 由环境 API 探测，不是 collect 输出。
- **grader**：`hello-outcome@v1` 按**内容地址**判分——把封存记录里
  `observable:result` 工件引用的 sha256 与期望序列化
  `{"name": "result", "value": "hello"}` 的 sha256 比对。REQUIRED_FIELDS
  = `["events", "token_usage"]`：transcript 降级 → cannot_judge（partial 也阻断）。
- **metrics**：`pass_pow_k, k=5`；job 声明 `n_attempts: 5` 与之匹配。

## 离线校验（已完成）

```bash
cd aeval
.venv/bin/python -m pytest -q tests/integration/test_e2e_suite.py   # 12/12
```

覆盖：suite 加载与 compose、file-only 观测源、基线只探声明观测、collect 计划一致、
digest 双处钉死、grader 身份/通过/错误内容失败/缺工件失败/transcript 降级 cannot_judge。

## 环境验证阶段执行清单（本骨架未覆盖、需真实环境）

1. **沙箱镜像**：e2b 模板从该 Dockerfile 构建（arm64）；实际拉取验证 digest。
2. **runtime lock 钉镜像**：
   ```bash
   aeval run --suite suites/e2e-hello --run-dir <新目录> --store <store.sqlite3> \
     --sandbox-image ubuntu@sha256:11dc1ccb…332cf --sandbox-platform arm64 \
     --harbor-cli harbor
   ```
   `--sandbox-image/--sandbox-platform` 已实现并测试（锁内 `images.sandbox`，
   observed-identity 绑定的比对目标）。
3. **agent**：把 job 的 `agents: [{name: nop}]` 换成 DSH 受控 agent（P0-4 环境阶段
   接线：`AEVAL_BROKER_JSON` broker spec + bootstrap 注入沙箱）。
4. **基线/观测探针**需要真实环境句柄（`await env.exec`）；句柄缺失=阻断。
5. **校验器**：`tests/test.sh` 在沙箱内执行（Harbor verifier）。
6. 收尾走 P0-8 finalize gate：未完成 → exit 5。

## 已知边界

- `agents: [{name: nop}]` 是占位：真实运行前要替换为受控 DSH agent（nop 不做任何事，
  任务会失败但链路各门禁仍会如实运转——适合先验证门禁再接 agent）。
- 本机（macOS）不能执行 e2b 构建；全部环境相关步骤在 Linux 目标机执行。
- digest 是多架构 manifest digest；若 e2b 模板构建器要求单架构镜像 digest，
  环境阶段以实际拉取结果为准更新（更新时 task.toml 与 Dockerfile 必须同步）。
