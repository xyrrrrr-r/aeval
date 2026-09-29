#!/bin/bash
# install-e2b-hardening.sh — 部署 / 校验 e2b 控制面加固（幂等，可反复执行）。
#
# 环境级脚本：与具体测试集、具体 harness 无关，服务于 e2b 单机部署本身。
# 背景、证据与验证记录见 aeval/docs/TESTS/E2B-CONTROL-PLANE-HARDENING.md。
#
# 两种模式：
#   install-e2b-hardening.sh            # apply：把加固装/修到当前主机
#   install-e2b-hardening.sh --check    # check：只报告漂移，不改动；有漂移 exit 1
# 附加：--quiet（少输出）
#
# 部署内容（四层里的宿主/集群部分）：
#   1) 构建 scratch 移出 tmpfs：bind mount /tmp/templates → 真实磁盘（写 /etc/fstab）；
#   2) Nomad 任务环境变量 TEMPLATE_CACHE_DIR / TMPDIR 指向真实磁盘（任务级 Resources 限额 32 GiB）；
#   3) 自愈护栏：e2b-leak-guard（每 5 分钟，回收孤儿 VM + 清理陈旧 scratch）；
#   4) 问题探测与自动加载：e2b-leak-watch（每 60 秒，发现漂移自动重跑本脚本、发现资源异常自动执行护栏）；
#   5) 开机自举：e2b-hardening-boot.service（开机执行本脚本，重建后自动恢复加固）。
#
# 本脚本会把自己与护栏/watch/单元文件复制到 /opt/e2b-hardening，成为主机上的自包含副本，
# 这样开机自举和漂移自愈不依赖仓库或 rsync 是否在场。
set -uo pipefail

MODE=apply; QUIET=0
for arg in "$@"; do
  case "$arg" in
    --check) MODE=check ;;
    --apply) MODE=apply ;;
    --quiet|-q) QUIET=1 ;;
    *) echo "用法: $0 [--apply|--check] [--quiet]"; exit 2 ;;
  esac
done
[ "$MODE" = "check" ] && DRY_RUN=1 || DRY_RUN=${DRY_RUN:-0}

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HW_DIR=${HW_DIR:-/opt/e2b-hardening}
GUARD_SRC=${GUARD_SRC:-$SRC_DIR/e2b-leak-guard.sh}
WATCH_SRC=${WATCH_SRC:-$SRC_DIR/e2b-leak-watch.sh}
UNIT_SRC=${UNIT_SRC:-$SRC_DIR/systemd}
GUARD_DST=${GUARD_DST:-/usr/local/bin/e2b-leak-guard.sh}
WATCH_DST=${WATCH_DST:-/usr/local/bin/e2b-leak-watch.sh}
UNIT_DST=/etc/systemd/system
CACHE_DIR=${CACHE_DIR:-/orchestrator/template-cache}
TMP_DIR=${TMP_DIR:-/orchestrator/tmp}
SCRATCH_DIR=${SCRATCH_DIR:-/orchestrator/template-scratch}
TM_MEMORY_MB=${TM_MEMORY_MB:-32768}
E2B_INFRA_DIR=${E2B_INFRA_DIR:-/opt/e2b-infra}
UNITS=(e2b-hardening-boot.service e2b-leak-guard.service e2b-leak-guard.timer e2b-leak-watch.service e2b-leak-watch.timer)

drift=()
say(){ [ "$QUIET" = 1 ] || echo "[e2b-hardening] $*"; }
warn(){ echo "[e2b-hardening] WARN: $*" >&2; }
ok(){ [ "$QUIET" = 1 ] || echo "  ok   $*"; }
bad(){ echo "  DRIFT $*"; drift+=("$*"); }
act(){ [ "$DRY_RUN" = 1 ] && { [ "$QUIET" = 1 ] || echo "  (check) 将执行: $*"; return 0; }; "$@"; }

[ "$(id -u)" = "0" ] || { echo "需要 root"; exit 1; }
[ -f "$GUARD_SRC" ] || { echo "缺少护栏脚本（部署源）: $GUARD_SRC"; exit 2; }
[ -f "$WATCH_SRC" ] || { echo "缺少探测脚本（部署源）: $WATCH_SRC"; exit 2; }

say "模式=$MODE 源=$SRC_DIR 自包含副本=$HW_DIR"

# ---------- 1) 目录与 bind mount ----------
say "1) 构建 scratch 落盘（bind mount /tmp/templates → $SCRATCH_DIR）"
for d in "$CACHE_DIR" "$TMP_DIR" "$SCRATCH_DIR"; do
  if [ -d "$d" ]; then ok "目录 $d"; else
    if [ "$DRY_RUN" = 1 ]; then bad "缺目录 $d"; else act install -d -m 700 "$d"; ok "已创建 $d"; fi
  fi
done
if mountpoint -q /tmp/templates; then
  src=$(findmnt -no SOURCE /tmp/templates 2>/dev/null)
  case "$src" in
    *template-scratch*) ok "/tmp/templates 已落盘（$src）" ;;
    *) bad "/tmp/templates 挂在 $src，期望 $SCRATCH_DIR（scratch 仍在别的文件系统上）" ;;
  esac
else
  if [ "$DRY_RUN" = 1 ]; then
    bad "/tmp/templates 不是挂载点（仍是 tmpfs：scratch 页不可回收）"
  else
    act cp -a /tmp/templates/. "$SCRATCH_DIR/" 2>/dev/null || true
    act mount --bind "$SCRATCH_DIR" /tmp/templates && ok "bind mount 完成"
  fi
fi
if grep -q "template-scratch" /etc/fstab 2>/dev/null; then
  ok "fstab 已有 bind 条目"
else
  if [ "$DRY_RUN" = 1 ]; then bad "fstab 缺 bind 条目（重启后会退回 tmpfs）"
  else act bash -c "echo '$SCRATCH_DIR /tmp/templates none bind 0 0' >> /etc/fstab"; ok "已写入 fstab"; fi
fi

# ---------- 2) Nomad 任务环境变量与限额 ----------
say "2) Nomad：TEMPLATE_CACHE_DIR / TMPDIR / 任务级 MemoryMB=$TM_MEMORY_MB"
nomad_py=$(mktemp); trap 'rm -f "$nomad_py"' EXIT
cat > "$nomad_py" <<'PY'
import json, os, sys, urllib.request

mode = os.environ.get('HW_MODE', 'apply')
base = os.environ.get('NOMAD_ADDR', 'http://127.0.0.1:4646')
hdr = {'X-Nomad-Token': os.environ.get('NOMAD_ACL_TOKEN', ''), 'Content-Type': 'application/json'}
want_env = {'TEMPLATE_CACHE_DIR': os.environ['CACHE_DIR'], 'TMPDIR': os.environ['TMP_DIR']}
want_mem = int(os.environ['TM_MEMORY_MB'])
drift = []

def load(name):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(f'{base}/v1/job/{name}', headers=hdr), timeout=20))

def post(job):
    req = urllib.request.Request(base + '/v1/jobs', data=json.dumps({'Job': job}).encode(),
                                 headers=hdr, method='POST')
    return json.load(urllib.request.urlopen(req, timeout=60)).get('EvalID')

for name in ('template-manager', 'api'):
    try:
        job = load(name)
    except Exception as exc:
        print(f'  WARN {name}: Nomad 不可读（{exc}）'); drift.append(f'nomad_{name}_unreadable'); continue
    changed = False
    for g in job.get('TaskGroups') or []:
        for t in g.get('Tasks') or []:
            env = t.get('Env') or {}
            for k, v in want_env.items():
                if env.get(k) != v:
                    note = f'{name}.{t["Name"]}.{k}={env.get(k)} 期望 {v}'
                    if mode == 'check':
                        print(f'  DRIFT {note}'); drift.append(note)
                    else:
                        env[k] = v; t['Env'] = env; changed = True; print(f'  fix  {note}')
            if name == 'template-manager':
                r = t.get('Resources') or {}
                if r.get('MemoryMB') != want_mem:
                    note = f'{name}.{t["Name"]}.MemoryMB={r.get("MemoryMB")} 期望 {want_mem}'
                    if mode == 'check':
                        print(f'  DRIFT {note}'); drift.append(note)
                    else:
                        r['MemoryMB'] = want_mem; t['Resources'] = r; changed = True; print(f'  fix  {note}')
    if changed and mode != 'check':
        print(f'  -> 提交 {name}: {post(job)}')
    elif not changed and mode != 'check':
        print(f'  ok   {name} 已符合')

print('NOMAD_DRIFT=' + ('1' if drift else '0'))
PY
if [ -f "$E2B_INFRA_DIR/.env" ]; then
  ( set -a; . "$E2B_INFRA_DIR/.env"; set +a
    if [ -z "${NOMAD_ACL_TOKEN:-}" ]; then
      warn "跳过 Nomad 检查：$E2B_INFRA_DIR/.env 里没有 NOMAD_ACL_TOKEN"
    else
      out=$(HW_MODE="$MODE" CACHE_DIR="$CACHE_DIR" TMP_DIR="$TMP_DIR" TM_MEMORY_MB="$TM_MEMORY_MB" \
            NOMAD_ACL_TOKEN="$NOMAD_ACL_TOKEN" NOMAD_ADDR=${NOMAD_ADDR:-http://127.0.0.1:4646} \
            python3 "$nomad_py" 2>&1)
      echo "$out" | grep -v '^NOMAD_DRIFT=' || true
      echo "$out" | grep -q '^NOMAD_DRIFT=1$' && drift+=("nomad_env_or_limit")
    fi
  )
else
  warn "跳过 Nomad 检查：找不到 $E2B_INFRA_DIR/.env"
fi

# ---------- 3) 自包含副本 ----------
say "3) 自包含副本 $HW_DIR（开机自举不依赖仓库在场）"
# 同路径复制会让 install 报 "same file"（从 /opt/e2b-hardening 自己运行时就是这种情况）
same_path(){ [ -n "$1" ] && [ "$(realpath "$1" 2>/dev/null)" = "$(realpath "$2" 2>/dev/null)" ]; }
copy_to(){ same_path "$1" "$2" && return 0; install -m "${3:-0755}" "$1" "$2"; }
if [ "$DRY_RUN" = 1 ]; then
  for f in "$(basename "$GUARD_SRC")" "$(basename "$WATCH_SRC")" "$(basename "${BASH_SOURCE[0]}")"; do
    [ -e "$HW_DIR/$f" ] || bad "副本缺 $HW_DIR/$f"
  done
  for u in "${UNITS[@]}"; do [ -e "$HW_DIR/systemd/$u" ] || bad "副本缺 $HW_DIR/systemd/$u"; done
else
  act install -d -m 0755 "$HW_DIR" "$HW_DIR/systemd"
  copy_to "$GUARD_SRC" "$HW_DIR/e2b-leak-guard.sh" 0755
  copy_to "$WATCH_SRC" "$HW_DIR/e2b-leak-watch.sh" 0755
  copy_to "${BASH_SOURCE[0]}" "$HW_DIR/install-e2b-hardening.sh" 0755
  for u in "${UNITS[@]}"; do copy_to "$UNIT_SRC/$u" "$HW_DIR/systemd/$u" 0644; done
  ok "副本已同步"
fi

# ---------- 4) 可执行文件 + 单元 ----------
say "4) 可执行文件与 systemd 单元"
guard_dst_sum=$(sha256sum "$GUARD_SRC" | cut -d' ' -f1)
if [ -f "$GUARD_DST" ] && [ "$(sha256sum "$GUARD_DST" | cut -d' ' -f1)" = "$guard_dst_sum" ]; then
  ok "护栏运行副本已是最新（sha256 ${guard_dst_sum:0:12}…）"
else
  if [ "$DRY_RUN" = 1 ]; then bad "护栏运行副本缺失或与部署源不一致"
  else act install -m 0755 -o root -g root "$GUARD_SRC" "$GUARD_DST"; ok "已安装护栏"; fi
fi
if [ -f "$WATCH_DST" ] && cmp -s "$WATCH_SRC" "$WATCH_DST"; then
  ok "watchdog 运行副本已是最新"
else
  if [ "$DRY_RUN" = 1 ]; then bad "watchdog 运行副本缺失或与部署源不一致"
  else act install -m 0755 -o root -g root "$WATCH_SRC" "$WATCH_DST"; ok "已安装 watchdog"; fi
fi
for u in "${UNITS[@]}"; do
  if [ -f "$UNIT_DST/$u" ] && cmp -s "$UNIT_SRC/$u" "$UNIT_DST/$u"; then
    ok "单元 $u"
  else
    if [ "$DRY_RUN" = 1 ]; then bad "单元 $u 缺失或过期"
    else act copy_to "$UNIT_SRC/$u" "$UNIT_DST/$u" 0644; ok "已安装单元 $u"; fi
  fi
done
if [ "$DRY_RUN" != 1 ]; then
  act systemctl daemon-reload
  # 开机自举服务在开机时是「由自己执行本脚本」，此时它正处于 activating：
  # 再 start 一次会与自身冲突并等成超时，所以这里只 enable，未在跑才 start。
  act systemctl enable e2b-hardening-boot.service
  boot_state=$(systemctl show -p ActiveState --value e2b-hardening-boot.service 2>/dev/null || echo unknown)
  case "$boot_state" in
    active|activating|reloading) ok "boot 服务已在运行（$boot_state），跳过重复启动" ;;
    *) act systemctl start e2b-hardening-boot.service ;;
  esac
  act systemctl enable --now e2b-leak-guard.timer
  act systemctl enable --now e2b-leak-watch.timer
else
  for u in e2b-hardening-boot.service e2b-leak-guard.timer e2b-leak-watch.timer; do
    systemctl is-enabled --quiet "$u" 2>/dev/null || bad "$u 未启用"
    [ "$u" = "e2b-hardening-boot.service" ] || systemctl is-active --quiet "$u" 2>/dev/null || bad "$u 未运行"
  done
fi

# ---------- 结论 ----------
echo
if [ ${#drift[@]} -gt 0 ]; then
  echo "[e2b-hardening] 发现 ${#drift[@]} 处漂移$([ "$MODE" = check ] && echo '（check 模式未做任何改动）')"
  for d in "${drift[@]}"; do echo "  - $d"; done
  [ "$MODE" = check ] && exit 1
fi
say "加固就绪：部署源 sha256 ${guard_dst_sum:0:12}… / 运行副本已核对；"
say "  护栏 e2b-leak-guard.timer（每 5 分钟）、探测 e2b-leak-watch.timer（每 60 秒）、开机自举 e2b-hardening-boot.service"
exit 0
