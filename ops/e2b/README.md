# ops/e2b — e2b 控制面加固（环境级，与测试集/harness 无关）

这里放的是 **e2b 单机部署本身**的运维加固，不属于任何 suite（`suites/…`）或 harness
（`dsh-eval-control`）。任何测试集、任何 harness 跑在同一套 e2b 上，都受益于这里的修复；
所以它单独存放，而不是寄生在某个 suite 的 `tools/` 下。

- 故障机理、证据、验证记录与残留风险：[docs/TESTS/E2B-CONTROL-PLANE-HARDENING.md](../../docs/TESTS/E2B-CONTROL-PLANE-HARDENING.md)
- 现场验收轮与影响面：[docs/TESTS/TBENCH-M0-PILOT.md](../../docs/TESTS/TBENCH-M0-PILOT.md) §11

## 一句话机理

模板构建的 `optimize`/`ResumeSandbox` 阶段 wedge → 沙箱销毁卡住 → **孤儿 firecracker VM**；
其 guest 内存是 `/tmp/templates/<build_id>/memfile`（tmpfs 快照），按 cgroup v2 记到
template-manager 任务 cgroup 的**不可回收 `shmem`**；swap 已满时 tmpfs 页不可回收 →
20 GiB 限额被顶满 → OOM → template-manager 反复重启 → 构建 `BuildException`、
沙箱放置 500（"控制面不可用"）。加固把 scratch 移出 tmpfs，并让主机能自愈。

## 内容

| 文件 | 作用 |
| --- | --- |
| `e2b-leak-guard.sh` | 常规维护（每 5 分钟）：按 API 活清单回收孤儿 VM、按 `env_builds` 状态精确清理陈旧 scratch、报告 cgroup 水位 |
| `e2b-leak-watch.sh` | 问题探测（每 60 秒）：API 健康 + cgroup 水位 + 孤儿信号 + **加固漂移检查**；异常时落诊断快照、自动重装加固、立即执行护栏 |
| `e2b-hardening-sync.sh` | 同步器：从 git 源（裸镜像 / 远端 fetch）取出 `ops/e2b`、同步到 `/opt/e2b-hardening/` 并应用；推送触发与定时拉取共用 |
| `install-e2b-hardening.sh` | 幂等部署/校验：`--check` 只报告漂移（有漂移 exit 1），默认 apply；把自身与脚本、单元、推送钩子复制成主机上的自包含副本 |
| `git/post-receive` | 裸镜像钩子：开发机一推送就立即同步并应用（"推送即生效"） |
| `git/dev-post-commit` | 开发机钩子：每次提交后自动把当前分支推到实验机镜像 |
| `systemd/` | 单元模板：`e2b-hardening-boot.service`（开机自举）、`e2b-leak-guard.{service,timer}`、`e2b-leak-watch.{service,timer}` |

## 部署（在 e2b 宿主机上，root）

```bash
# 1) 把本目录放到主机（自包含副本目录，开机自举从这里跑）
rsync -a ops/e2b/ <host>:/opt/e2b-hardening/
# 2) 应用（幂等）；只想看会不会动：先 --check
ssh <host> '/opt/e2b-hardening/install-e2b-hardening.sh --check'
ssh <host> '/opt/e2b-hardening/install-e2b-hardening.sh'
```

部署后主机上：

```text
/opt/e2b-hardening/            # 自包含副本（部署源；rsync 自仓库）
/usr/local/bin/e2b-leak-guard.sh
/usr/local/bin/e2b-leak-watch.sh
/etc/systemd/system/e2b-leak-*.{service,timer}  e2b-hardening-boot.service
/var/log/e2b-hardening/        # watchdog 的诊断快照与自动加载日志
```

## 自动加载与自愈（"服务器遇到问题自己动手"）

| 场景 | 机制 | 效果 |
| --- | --- | --- |
| 主机重启 / 镜像重建后加固丢失 | `e2b-hardening-boot.service`（`WantedBy=multi-user.target`）开机执行安装脚本 | bind mount、`/etc/fstab`、Nomad env 与限额、单元全部自动恢复 |
| 加固被改动/删除/降级 | `e2b-leak-watch.timer`（60 秒）跑 `install-e2b-hardening.sh --check`，`exit 1` 即漂移 | 自动重跑安装脚本把加固装回来（`AUTO_INSTALL=1`，可用环境变量关闭） |
| 孤儿 VM / scratch 堆积 | `e2b-leak-guard.timer`（5 分钟）+ watchdog 发现异常时立即执行 | 资源自动回收，不等人工 |
| API/cgroup 异常 | watchdog 落诊断快照到 `/var/log/e2b-hardening/`，并执行护栏 | 现场留存 + 资源侧自愈；需要重启 Nomad 作业的情况只记录、不擅自动手 |
| 仓库里改了脚本 | 推送即生效（A）+ 每 5 分钟自主拉取（B），见下节 | 不再需要人工 rsync |

## 自动拉取（A 推送即生效 / B 服务器自主拉取）

`e2b-hardening-sync.timer` 每 5 分钟跑一次同步器；同步器做三件事：取源 → 同步到
`/opt/e2b-hardening/` → 跑安装器应用并自检。取源有两条路径，互为兜底：

- **A（默认可用，无需任何凭据）**：开发机 `push` → 实验机裸镜像 `/srv/git/aeval.git`
  → 镜像的 `post-receive` 钩子 → **立即同步并应用**。开发机侧装一次钩子即可全自动：

  ```bash
  cp ops/e2b/git/dev-post-commit .git/hooks/post-commit && chmod +x .git/hooks/post-commit
  # 之后每次 git commit 都会在后台推送当前分支（失败不影响提交，日志见 .git/e2b-sync.log）
  ```

- **B（服务器自己拉）**：仓库是私有仓库，所以只有当令牌文件存在时才尝试远端 fetch：
  把访问令牌写入 `/etc/e2b-hardening/git-token`（`chmod 600`）即可，随后同步器会
  `git fetch` 远端分支并应用。凭据经 0600 的 credential store 传递，**不出现在命令行、
  也不写进日志**；没有令牌文件时 B 自动跳过，不影响 A。

配置与日志：`/etc/e2b-hardening/sync.conf`（镜像、分支、远端 URL、令牌路径）、
`/var/log/e2b-hardening/sync.log`。手动触发一次：`systemctl start e2b-hardening-sync.service`。

## 版本一致性与漂移

- 仓库是本目录的**部署源**；主机上 `/opt/e2b-hardening/` 是**自包含副本**（开机自举与自愈都基于它），
  `/usr/local/bin/` 是被 systemd 调用的**运行副本**。
- 改脚本 = 改仓库 → `rsync` 到 `/opt/e2b-hardening/` → 跑安装脚本（或等 watchdog 自动发现漂移）。
- 基线校验：`sha256 e2b-leak-guard.sh = 300000cf5b3c73f084c172670d0c6bb2c5e649f6e69e59bbe9dbfb38963d5de7`
- 安装脚本自身也会核对运行副本与部署源的哈希/内容，不一致会明确报告。

## 安全边界（为什么它敢自动动手）

- **不动活沙箱**：孤儿判据要求"不在 API 活清单 + 超过最小年龄（15 分钟）+ 当前无在途构建"；已用
  "活沙箱 + 人为改老 socket" 做过专项测试。
- **不删在途构建**：scratch 判据来自数据库 `env_builds`（保留 `uploaded` 与 30 分钟内在途记录）。
- **不擅自重启服务**：需要重启 Nomad 作业的只有"漂移已确认"这一种情况（由安装脚本幂等处理）；API 不健康时
  watchdog 只记录诊断，不动手。
- **失败不升级**：watchdog 永远 `exit 0`（避免单元刷红），结论写 journald 与日志文件。
