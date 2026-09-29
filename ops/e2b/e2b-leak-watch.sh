#!/bin/bash
# e2b-leak-watch.sh — 每 60 秒探测一次 e2b 控制面；发现问题就自己动手，不等人。
#
# 与 e2b-leak-guard.sh 的分工：
#   guard（每 5 分钟）= 常规维护：回收孤儿 VM、按构建记录清理 scratch；
#   watch（每 60 秒）  = 问题探测 + 自动加载/自愈：
#     1) 探测 API 健康、template-manager cgroup 水位、孤儿 VM 信号；
#     2) 探测"加固是否还在"（--check 漂移检查）——镜像重建/换机后加固会丢，
#        这时自动重跑安装脚本把 bind mount、Nomad env、限额、单元装回来；
#     3) 任何一项异常：先落一份诊断快照（便于事后复盘），再立刻执行 guard。
#
# 设计取向：探测便宜、动作保守、失败不升级。
#   - 永远 exit 0（不让 systemd 单元变红刷屏）；结论写 journald 与日志文件；
#   - 需要动 Nomad 作业（会重启任务）的只有"漂移已确认"这一种情况，且受
#     AUTO_INSTALL 开关控制；不确定时只记录、不动作。
#
# 手动排查：
#   DRY_RUN=1 bash /usr/local/bin/e2b-leak-watch.sh   # 只探测与记录，不执行安装/回收
set -uo pipefail

API=${E2B_API_URL:-http://localhost:3000}
CONFIG=${E2B_CONFIG:-/root/.e2b/config.json}
WARN_PCT=${WARN_PCT:-85}
AUTO_INSTALL=${AUTO_INSTALL:-1}
DRY_RUN=${DRY_RUN:-0}
LOG_DIR=${LOG_DIR:-/var/log/e2b-hardening}
GUARD=${GUARD:-/usr/local/bin/e2b-leak-guard.sh}
HW_DIR=${HW_DIR:-/opt/e2b-hardening}
INSTALL="$HW_DIR/install-e2b-hardening.sh"
PG_CONTAINER=${PG_CONTAINER:-postgres}
PG_DB=${PG_DB:-mydatabase}
PG_USER=${PG_USER:-postgres}
API_RETRIES=${API_RETRIES:-3}          # 健康探测次数（避免抓到秒级抖动）

log(){ echo "[$(date '+%F %T')] $*"; }
problems=()

# ---------- 1) API 健康 ----------
health_code=000
for _ in $(seq 1 "$API_RETRIES"); do
  health_code=$(timeout 15 curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$API/health" 2>/dev/null || echo 000)
  [ "$health_code" = "200" ] && break
  sleep 2
done
[ "$health_code" = "200" ] || problems+=("api_health=$health_code")

# ---------- 2) template-manager cgroup 水位 ----------
tm_pid=$(pgrep -f 'template-manager --port' | head -1)
cgroup_line=""
if [ -n "${tm_pid:-}" ]; then
  scope=/sys/fs/cgroup$(awk -F: '{print $3}' /proc/"$tm_pid"/cgroup 2>/dev/null | head -1)
  cg_max=$(cat "$scope/memory.max" 2>/dev/null)
  cg_cur=$(cat "$scope/memory.current" 2>/dev/null)
  if [ -n "${cg_max:-}" ] && [ -n "${cg_cur:-}" ] && [ "$cg_max" != "max" ]; then
    pct=$(( cg_cur * 100 / cg_max ))
    cgroup_line="current=$((cg_cur/1048576))MiB max=$((cg_max/1048576))MiB (${pct}%)"
    [ "$pct" -ge "$WARN_PCT" ] && problems+=("cgroup=${pct}%")
  fi
else
  problems+=("template_manager_down")
fi

# ---------- 3) 加固漂移（自动加载的判据）----------
drift=0
if [ -x "$INSTALL" ]; then
  if ! timeout 90 "$INSTALL" --check --quiet >/dev/null 2>&1; then
    drift=1
    problems+=("hardening_drift")
  fi
else
  drift=1
  problems+=("hardening_missing:$HW_DIR")
fi

# ---------- 4) 孤儿 VM 信号（保守：只在明显多于"活沙箱+在途构建"时报警）----------
vm_count=$(pgrep -cf 'fc-versions/.*firecracker' 2>/dev/null); vm_count=${vm_count//[^0-9]/}; vm_count=${vm_count:-0}
live_count=0
if [ -f "$CONFIG" ]; then
  key=$(python3 -c "import json;print(json.load(open('$CONFIG'))['teamApiKey'])" 2>/dev/null || true)
  if [ -n "${key:-}" ]; then
    live_count=$(timeout 20 curl -sS --max-time 15 -H "X-API-KEY: $key" "$API/sandboxes?limit=200" 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
items = d if isinstance(d,list) else (d.get('sandboxes') or d.get('data') or [])
print(sum(1 for i in items if isinstance(i,dict)))
" 2>/dev/null || echo 0)
  fi
fi
live_count=${live_count//[^0-9]/}; live_count=${live_count:-0}
inflight=$(timeout 20 docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -A -t \
  -c "select count(*) from env_builds where status not in ('uploaded','failed') and updated_at > now() - interval '30 minutes';" 2>/dev/null | tr -d ' \r')
inflight=${inflight//[^0-9]/}; inflight=${inflight:-0}
expected=$(( live_count + inflight ))
if [ "$vm_count" -gt "$expected" ]; then
  problems+=("orphan_vms=$(( vm_count - expected ))")
fi

# ---------- 结论 ----------
if [ ${#problems[@]} -eq 0 ]; then
  log "正常: health=200 vm=${vm_count}(live=${live_count}+inflight=${inflight}) ${cgroup_line}"
  exit 0
fi

summary=$(IFS=,; echo "${problems[*]}")
log "发现问题: $summary"

# ---------- 诊断快照 ----------
if [ "$DRY_RUN" != "1" ]; then
  mkdir -p "$LOG_DIR" 2>/dev/null
  snap="$LOG_DIR/watch-$(date '+%Y%m%dT%H%M%SZ').log"
  {
    echo "problems: $summary"
    date -u; uptime; echo "loadavg: $(cut -d' ' -f1-3 /proc/loadavg)"
    echo "--- api health: $health_code"
    echo "--- cgroup: $cgroup_line"
    [ -n "${scope:-}" ] && { echo "memory.stat:"; grep -E '^(shmem|file|anon) ' "$scope/memory.stat" 2>/dev/null; echo "memory.events:"; grep -E '^(oom|oom_kill|max) ' "$scope/memory.events" 2>/dev/null; }
    echo "--- free/swap"; free -g 2>/dev/null | head -3
    echo "--- mounts"; findmnt -no SOURCE,FSTYPE,TARGET /tmp/templates 2>/dev/null; df -h / /tmp 2>/dev/null | tail -3
    echo "--- scratch: $(ls -d /tmp/templates/* 2>/dev/null | wc -l) dirs, $(du -sh /tmp/templates 2>/dev/null | cut -f1)"
    echo "--- firecracker VMs (${vm_count})"; ps -eo pid,etimes,args | grep 'fc-versions/.*firecracker' | grep -v grep | sed 's/--config-file.*//' | head -20
    echo "--- live sandboxes: ${live_count}, inflight builds: ${inflight}"
    echo "--- guard 最近记录"; journalctl -u e2b-leak-guard.service --no-pager -n 5 2>/dev/null | tail -5
  } > "$snap" 2>&1
  log "诊断快照: $snap"
else
  log "(DRY_RUN) 不写快照、不动手"
fi

# ---------- 自动加载（漂移）----------
if [ "$drift" = "1" ]; then
  if [ "$AUTO_INSTALL" = "1" ] && [ "$DRY_RUN" != "1" ]; then
    log "加固缺失/漂移 → 自动重新加载（$INSTALL --quiet）"
    "$INSTALL" --quiet >>"$LOG_DIR/watch.log" 2>&1 && log "自动加载完成 ✓"
  else
    log "(AUTO_INSTALL=$AUTO_INSTALL DRY_RUN=$DRY_RUN) 需要人工执行: $INSTALL"
  fi
fi

# ---------- 自愈（资源异常）----------
if [ "$DRY_RUN" != "1" ] && [ -x "$GUARD" ]; then
  log "执行护栏自愈: $GUARD"
  timeout 180 "$GUARD" 2>&1 | tail -4
fi
exit 0
