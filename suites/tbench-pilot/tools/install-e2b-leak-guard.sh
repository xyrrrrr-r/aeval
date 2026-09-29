#!/bin/bash
# install-e2b-leak-guard.sh — 幂等部署 e2b 控制面加固（可重复执行）。
#
# 背景与验证见 aeval/docs/TESTS/E2B-CONTROL-PLANE-HARDENING.md。
# 本脚本部署四层修复里需要落在宿主机/集群上的部分（不依赖重新编译 e2b）：
#   1) 构建 scratch 移出 tmpfs：bind mount /tmp/templates → 真实磁盘；
#   2) Nomad 环境变量：TEMPLATE_CACHE_DIR / TMPDIR 指向真实磁盘；
#   3) template-manager 内存限额抬到 32 GiB（缓冲用，不改故障机理）；
#   4) 自愈护栏：每 5 分钟回收孤儿 firecracker VM + 按构建记录清理 scratch。
#
# 在宿主机（e2b 单机部署）上以 root 执行：
#   bash install-e2b-leak-guard.sh            # 部署
#   DRY_RUN=1 bash install-e2b-leak-guard.sh  # 只打印将执行的动作
set -uo pipefail

GUARD_SRC=${GUARD_SRC:-"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/e2b-leak-guard.sh"}  # 仓库内的部署源
GUARD_DST=${GUARD_DST:-/usr/local/bin/e2b-leak-guard.sh}        # 服务器上的运行副本
CACHE_DIR=${CACHE_DIR:-/orchestrator/template-cache}           # 模板缓存（真实磁盘）
TMP_DIR=${TMP_DIR:-/orchestrator/tmp}                          # TMPDIR（socket 等）
SCRATCH_DIR=${SCRATCH_DIR:-/orchestrator/template-scratch}     # /tmp/templates 的落盘位置
TM_MEMORY_MB=${TM_MEMORY_MB:-32768}
DRY_RUN=${DRY_RUN:-0}
E2B_INFRA_DIR=${E2B_INFRA_DIR:-/opt/e2b-infra}

run(){ [ "$DRY_RUN" = "1" ] && { echo "DRY_RUN: $*"; return 0; }; "$@"; }
say(){ echo "[install-e2b-leak-guard] $*"; }

[ "$(id -u)" = "0" ] || { echo "需要 root"; exit 1; }
[ -f "$GUARD_SRC" ] || { echo "缺少护栏脚本（部署源）$GUARD_SRC"; exit 1; }

# ---------- 1) bind mount：构建 scratch 落盘而非 tmpfs ----------
say "1/4 bind mount /tmp/templates → $SCRATCH_DIR"
run install -d -m 700 "$SCRATCH_DIR"
if ! mountpoint -q /tmp/templates; then
  # 迁移已有内容，避免隐藏 ready 模板的 scratch
  run cp -a /tmp/templates/. "$SCRATCH_DIR/" 2>/dev/null || true
  run mount --bind "$SCRATCH_DIR" /tmp/templates
fi
grep -q "template-scratch" /etc/fstab 2>/dev/null || \
  run bash -c "echo '$SCRATCH_DIR /tmp/templates none bind 0 0' >> /etc/fstab"
findmnt -no SOURCE,FSTYPE,TARGET /tmp/templates || say "  （bind mount 未生效，请检查）"

# ---------- 2) Nomad 任务环境变量 ----------
say "2/4 Nomad env: TEMPLATE_CACHE_DIR=$CACHE_DIR TMPDIR=$TMP_DIR"
run install -d -m 700 "$CACHE_DIR" "$TMP_DIR"
if [ -f "$E2B_INFRA_DIR/.env" ]; then
  ( set -a; . "$E2B_INFRA_DIR/.env"; set +a
    [ -n "${NOMAD_ACL_TOKEN:-}" ] || { say "  跳过：.env 里没有 NOMAD_ACL_TOKEN"; exit 0; }
    NOMAD_TOKEN="$NOMAD_ACL_TOKEN" NOMAD_ADDR=${NOMAD_ADDR:-http://127.0.0.1:4646} \
    CACHE_DIR="$CACHE_DIR" TMP_DIR="$TMP_DIR" python3 - <<'PY'
import json, os, urllib.request
base=os.environ.get('NOMAD_ADDR','http://127.0.0.1:4646')
hdr={'X-Nomad-Token':os.environ['NOMAD_TOKEN'],'Content-Type':'application/json'}
for jobname in ('template-manager','api'):
    job=json.load(urllib.request.urlopen(urllib.request.Request(f'{base}/v1/job/{jobname}', headers=hdr), timeout=20))
    changed=False
    for g in job['TaskGroups']:
        for t in g['Tasks']:
            env=t.setdefault('Env',{})
            for k,v in (('TEMPLATE_CACHE_DIR',os.environ['CACHE_DIR']),('TMPDIR',os.environ['TMP_DIR'])):
                if env.get(k)!=v: env[k]=v; changed=True
    print(f"  {jobname}: {'更新' if changed else '已符合'}")
    if changed:
        req=urllib.request.Request(base+'/v1/jobs', data=json.dumps({'Job':job}).encode(), headers=hdr, method='POST')
        print('    ->', json.load(urllib.request.urlopen(req, timeout=60)).get('EvalID'))
PY
  )
else
  say "  跳过：找不到 $E2B_INFRA_DIR/.env"
fi

# ---------- 3) template-manager 内存限额 ----------
say "3/4 template-manager Resources.MemoryMB = $TM_MEMORY_MB"
( set -a; . "$E2B_INFRA_DIR/.env" 2>/dev/null; set +a
  [ -n "${NOMAD_ACL_TOKEN:-}" ] || exit 0
  NOMAD_TOKEN="$NOMAD_ACL_TOKEN" NOMAD_ADDR=${NOMAD_ADDR:-http://127.0.0.1:4646} \
  TM_MEMORY_MB="$TM_MEMORY_MB" python3 - <<'PY'
import json, os, urllib.request
base=os.environ.get('NOMAD_ADDR','http://127.0.0.1:4646')
hdr={'X-Nomad-Token':os.environ['NOMAD_TOKEN'],'Content-Type':'application/json'}
want=int(os.environ['TM_MEMORY_MB'])
job=json.load(urllib.request.urlopen(urllib.request.Request(f'{base}/v1/job/template-manager', headers=hdr), timeout=20))
changed=False
# 生效的是「任务级」Resources.MemoryMB；组级 Resources 只是调度聚合，在这份单机
# spec 里本来就是 None —— 不要凭空创建它，否则每次重跑都会无谓地重提作业+重启任务。
for g in job['TaskGroups']:
    for t in g['Tasks']:
        r=t.get('Resources') or {}
        print(f"  任务 {t['Name']}: MemoryMB={r.get('MemoryMB')} -> {want}")
        if r.get('MemoryMB')!=want:
            r['MemoryMB']=want; t['Resources']=r; changed=True
print('  template-manager:', '更新' if changed else '已符合')
if changed:
    req=urllib.request.Request(base+'/v1/jobs', data=json.dumps({'Job':job}).encode(), headers=hdr, method='POST')
    print('    ->', json.load(urllib.request.urlopen(req, timeout=60)).get('EvalID'))
PY
)

# ---------- 4) 自愈护栏 ----------
say "4/4 自愈护栏（每 5 分钟）"
# 运行副本始终从仓库内的部署源安装：改仓库 → 跑本脚本 → 服务器生效
run install -m 0755 -o root -g root "$GUARD_SRC" "$GUARD_DST"
run bash -c 'cat > /etc/systemd/system/e2b-leak-guard.service' <<'EOF'
[Unit]
Description=E2B leak guard (orphan firecracker VMs + stale build scratch)
Documentation=file:/usr/local/bin/e2b-leak-guard.sh
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
Environment=MIN_AGE_MIN=15 INFLIGHT_MIN=30
ExecStart=/usr/local/bin/e2b-leak-guard.sh
Nice=10
IOSchedulingClass=idle
EOF
run bash -c 'cat > /etc/systemd/system/e2b-leak-guard.timer' <<'EOF'
[Unit]
Description=Run the E2B leak guard every 5 minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF
run systemctl daemon-reload
run systemctl enable --now e2b-leak-guard.timer
say "完成；立刻验证一次："
DRY_RUN=1 bash "$GUARD_DST" | tail -3

echo
say "部署源 (仓库): $GUARD_SRC"
say "运行副本 (服务器): $GUARD_DST"
if command -v sha256sum >/dev/null; then
  src_sum=$(sha256sum "$GUARD_SRC" | cut -d' ' -f1)
  dst_sum=$(sha256sum "$GUARD_DST" | cut -d' ' -f1)
  say "sha256 部署源=$src_sum"
  say "sha256 运行副本=$dst_sum"
  [ "$src_sum" = "$dst_sum" ] && say "一致性校验通过 ✓（仓库改完必须重跑本脚本，否则服务器仍是旧版本）" \
    || say "不一致 ✗ —— 请重跑本脚本"
fi
