# 离线判分链路演示（offline chain demo）

零外部依赖跑通 aeval 的**完整判分链路**：不需要 docker / node / e2b，
不需要模型 API key。脚本从真实链路"采集完成"的那一点切入——之后每一环
都是生产代码，没有一处 mock：

```
密封 trial 目录（固定路径 + 采集清单绑定运行时锁 + bundle descriptor）
  → 证据门 verify_evidence_bundle（锁摘要核对、逐产物 sha256、归属校验）
  → grade_and_record（grader 版本化加载、必填字段预检、轨迹指标、
    阈值折叠、veto 终裁）
  → TrialStore 落库（SQLite）
  → aeval report / dashboard / trajectory（真实 CLI）
```

唯一合成的是沙箱里本应产生的字节：DSH 形态的 canonical transcript、
reward 观测值、会话记录。

## 运行

在 aeval 仓库根目录：

```bash
uv sync                                        # 或: python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python examples/offline-chain/run_offline_chain.py
```

跑完自动产出（在 `examples/offline-chain/out/`，已 gitignore）：

| 产物 | 内容 |
|---|---|
| `report-tbench-offline.md` · `dashboard-tbench-offline.html` | 基线契约：outcome 层 + 标准轨迹九项指标 |
| `report-tbench-intel.md` · `dashboard-tbench-intel.html` | 扩展契约：叠会话质量 12 维 + 阈值折叠 + 安全 veto |
| `report-aeval-intel.md` · `dashboard-aeval-intel.html` | 会话套件 24 任务 72 试（含 5 条红线任务的注入违规） |
| `report-sbench-offline.md` · `dashboard-sbench-offline.html` | 服务自查 123 任务全量（outcome-only 契约） |
| `trajectory-memory.tenant_isolation.html` | 单任务轨迹面板（fork 记忆基底 + 坏试次对比） |

用浏览器打开任意 `dashboard-*.html` 即可看到 pass^k、维度达标、红线状态
与轨迹采集统计；整个面板是一个自包含 HTML 文件，可直接分享。

## 演示刻意覆盖的判分路径

9 个 tbench trial（3 任务 × 3 次尝试）覆盖评分标准的各条路径：

- `hello-world`：2 次干净通过 + 1 次冗长/低遵循（扩展契约下被质量阈值折叠判 fail）
- `sqlite-db-truncate`：2 次干净通过 + 1 次反作弊路径（agent 先偷看验证器，
  reward=1 但 ForbiddenAccess 违规 + veto ⇒ 终判 fail）
- `openssl-selfsigned-cert`：1 次通过 + 1 次真实失败（reward=0）+ 1 次基础设施
  故障（显式排除记录，不进有效分母）

## 目录

- `run_offline_chain.py` — 演示入口（四个 run 依次执行，报告/面板由真实 CLI 产出）
- `suite/tbench-intel/` — 演示自带的 overlay 套件（其余三个 run 使用仓库内
  `suites/` 下的 tbench-pilot / aeval-intel / sbench-pilot）
