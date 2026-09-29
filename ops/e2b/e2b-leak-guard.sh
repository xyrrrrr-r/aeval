#!/bin/bash
# e2b-leak-guard.sh — 防止 e2b 单机部署的构建沙箱泄漏再次拖垮控制面。
#
# 背景（2026-09-28/29 实测，见 aeval/docs/TESTS/TBENCH-M0-PILOT.md §11）：
#   模板构建的 optimize/内存预取阶段要 resume 一个沙箱采集预取映射；该阶段
#   wedge（ResumeSandbox context deadline exceeded）时沙箱销毁路径卡住，
#   firecracker VM 变成孤儿：既不在 API 的沙箱清单里，也不被 /orchestrator
#   状态回收。孤儿 VM 的 guest 内存是 tmpfs 快照（/tmp/templates/<build_id>/
#   memfile，/tmp 是 tmpfs），按 cgroup v2 记到 template-manager 任务 cgroup；
#   12 个孤儿把 20 GiB 限额顶满、swap 已满使 tmpfs 页不可回收，于是任何新构建
#   都触发 OOM，template-manager 反复重启、构建 BuildException，同时沙箱放置
#   500 Failed to place sandbox。清理孤儿与陈旧 scratch 后立刻恢复。
#
# 本脚本做三件事（全部 fail-safe：任何判据不成立就「不动作」）：
#   1) 回收孤儿 VM：判据 = API /sandboxes 清单里没有它 + socket 早于 MIN_AGE_MIN
#      + 当前没有在途构建（在途构建的沙箱是正常的，绝不能碰）。
#   2) 清理陈旧构建 scratch：按数据库 env_builds.status 精确保留 uploaded 与
#      在途构建，其余且超过 MIN_AGE_MIN 的 /tmp/templates/<build_id> 与
#      $TEMPLATE_CACHE_DIR/<build_id> 删除并统计真实释放量。
#   3) 观测：输出 template-manager cgroup 的内存水位与孤儿数，超阈值打 WARN。
#
# 用法：DRY_RUN=1 bash e2b-leak-guard.sh    # 只报告，不删除
#      bash e2b-leak-guard.sh               # 实际执行（systemd timer 每 5 分钟）
set -uo pipefail

MIN_AGE_MIN=${MIN_AGE_MIN:-15}       # 只碰比这更旧的 socket / 目录（构建可能耗时，宁松勿严）
INFLIGHT_MIN=${INFLIGHT_MIN:-30}     # 构建记录多久没更新就算「已死」，不再保护
WARN_PCT=${WARN_PCT:-85}             # cgroup 水位告警阈值
DRY_RUN=${DRY_RUN:-0}
SCAN_DIRS=${SCAN_DIRS:-/tmp/templates}   # 另加 $TEMPLATE_CACHE_DIR（若设置）
CONFIG=${E2B_CONFIG:-/root/.e2b/config.json}
API=${E2B_API_URL:-http://localhost:3000}
PG_CONTAINER=${PG_CONTAINER:-postgres}
PG_DB=${PG_DB:-mydatabase}
PG_USER=${PG_USER:-postgres}
SANDBOX_STATE_DIR=/orchestrator/sandbox

log(){ echo "[$(date '+%F %T')] $*"; }
warn(){ echo "[$(date '+%F %T')] WARN: $*"; }

removed_vms=0; removed_dirs=0; freed_kb=0

# ---------- 观测：template-manager cgroup 水位 ----------
report_cgroup(){
  local pid scopes scope max cur
  pid=$(pgrep -f 'template-manager --port' | head -1)
  [ -n "$pid" ] || { warn "template-manager 进程不存在"; return; }
  scopes=$(cat /proc/"$pid"/cgroup 2>/dev/null | awk -F: '{print $3}' | head -1)
  scope=/sys/fs/cgroup${scopes}
  max=$(cat "$scope/memory.max" 2>/dev/null); cur=$(cat "$scope/memory.current" 2>/dev/null)
  [ -n "${max:-}" ] && [ -n "${cur:-}" ] || { warn "读不到 cgroup 内存（$scope）"; return; }
  if [ "$max" = "max" ]; then
    log "template-manager cgroup: 无上限, current=$((cur/1073741824))GiB"
    return
  fi
  local pct=$(( cur * 100 / max ))
  log "template-manager cgroup: current=$((cur/1048576))MiB / max=$((max/1048576))MiB (${pct}%)"
  [ "$pct" -ge "$WARN_PCT" ] && warn "cgroup 水位 ${pct}% ≥ ${WARN_PCT}% —— 构建随时可能 OOM，检查孤儿 VM 与 scratch"
  grep -E '^(oom_kill|max) ' "$scope/memory.events" 2>/dev/null | while read -r k v; do log "  memory.events $k=$v"; done
}

# ---------- oracle 1：活沙箱清单（失败即放弃回收 VM）----------
live_sandbox_ids(){
  local key
  key=$(python3 -c "import json;print(json.load(open('$CONFIG'))['teamApiKey'])" 2>/dev/null) || return 1
  [ -n "$key" ] || return 1
  curl -sS --max-time 20 -H "X-API-KEY: $key" "$API/sandboxes?limit=200" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(1)
items = d if isinstance(d,list) else (d.get('sandboxes') or d.get('data') or [])
for i in items:
    if isinstance(i,dict):
        sid = i.get('sandboxID') or i.get('sandboxId') or i.get('sandbox_id')
        if sid: print(sid)
" 2>/dev/null
}

# ---------- oracle 2：构建记录（失败即放弃清理 scratch / 回收 VM）----------
psql_q(){
  # timeout：docker exec 在 OOM/守护进程拥塞时可能无限期阻塞；拿不到构建状态就 fail-safe 不动作
  timeout 20 docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -A -t -c "$1" 2>/dev/null
}

# ---------- 在途构建（近期仍在更新的非终态构建）----------
inflight_build_ids(){
  psql_q "select id from env_builds where status not in ('uploaded','failed') and updated_at > now() - interval '$INFLIGHT_MIN minutes';" \
    | tr -d ' \r' | grep -v '^$'
}

# ---------- 需要保留的构建 id（uploaded + 在途）----------
keep_build_ids(){
  psql_q "select id from env_builds where status='uploaded' or (status not in ('uploaded','failed') and updated_at > now() - interval '$INFLIGHT_MIN minutes');" \
    | tr -d ' \r' | grep -v '^$'
}

# ---------- 1) 回收孤儿 VM ----------
reap_vms(){
  local live inflight pids id age now sock pid
  live=$(live_sandbox_ids) || { warn "拿不到活沙箱清单，跳过 VM 回收"; return; }
  inflight=$(inflight_build_ids)
  if [ -n "$inflight" ]; then
    log "有 $(echo "$inflight" | wc -l) 个在途构建，跳过 VM 回收（构建沙箱不在 API 清单里，属正常）"
    return
  fi
  now=$(date +%s)
  local live_count=0; [ -n "$live" ] && live_count=$(echo "$live" | wc -l)
  log "活沙箱 ${live_count} 个；开始核对 firecracker VM"
  while read -r pid sock; do
    [ -n "${pid:-}" ] || continue
    id=$(basename "$sock" | sed 's/^fc-//; s/-[^-]*\.sock$//')
    [ -n "$id" ] || continue
    # 只清理该 VM 自己的 socket 与同级 uffd socket（socket 目录可由 TMPDIR 改变）
    sock_dir=$(dirname "$sock"); uffd_sock="$sock_dir/$(basename "$sock" | sed 's/^fc-/uffd-/')"

    if echo "$live" | grep -qx "$id"; then continue; fi
    age=$(( (now - $(stat -c %Y "$sock" 2>/dev/null || echo "$now")) / 60 ))
    if [ "$age" -lt "$MIN_AGE_MIN" ]; then
      log "跳过年轻 VM $id（${age}min < ${MIN_AGE_MIN}min）"
      continue
    fi
    if [ "$DRY_RUN" = "1" ]; then
      log "DRY_RUN 会回收孤儿 VM $id (pid=$pid, age=${age}min)"
    else
      kill -9 "$pid" 2>/dev/null && log "回收孤儿 VM $id (pid=$pid, age=${age}min)"
      rm -f "$sock" "$uffd_sock"
      rm -f "$SANDBOX_STATE_DIR"/*"$id"* 2>/dev/null
    fi
    removed_vms=$((removed_vms+1))
  done < <(ps -eo pid,args | awk '/fc-versions\/.*firecracker/ && !/awk/ {for(i=1;i<=NF;i++) if ($i=="--api-sock") print $1, $(i+1)}')
}

# ---------- 2) 清理陈旧构建 scratch ----------
prune_scratch(){
  local keep dirs dir base kb
  keep=$(keep_build_ids)
  if [ -z "$keep" ]; then
    if psql_q "select 1;" >/dev/null && [ -n "$(psql_q "select count(*) from env_builds;")" ]; then
      log "保留集合为空但数据库可读，按「不做删除」处理"
    else
      warn "数据库不可用，跳过 scratch 清理"
    fi
    return
  fi
  for base in $SCAN_DIRS ${TEMPLATE_CACHE_DIR:-}; do
    [ -d "$base" ] || continue
    for dir in "$base"/*; do
      [ -d "$dir" ] || continue
      local name; name=$(basename "$dir")
      echo "$name" | grep -qE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || { log "跳过（非构建 UUID）: $base/$name"; continue; }
      echo "$keep" | grep -qx "$name" && continue
      find "$dir" -maxdepth 0 -mmin +"$MIN_AGE_MIN" | grep -q . || { log "跳过（太新）: $base/$name"; continue; }
      kb=$(du -sk "$dir" 2>/dev/null | cut -f1); kb=${kb:-0}
      if [ "$DRY_RUN" = "1" ]; then
        log "DRY_RUN 会删除陈旧构建目录 $base/$name (${kb}KB)"
      else
        rm -rf "$dir" && log "删除陈旧构建目录 $base/$name (${kb}KB)"
      fi
      removed_dirs=$((removed_dirs+1)); freed_kb=$((freed_kb+kb))
    done
  done
}

report_cgroup
reap_vms
prune_scratch
log "完成: 回收孤儿 VM ${removed_vms} 个, 删除陈旧构建目录 ${removed_dirs} 个, 释放 $((freed_kb/1024))MB$([ "$DRY_RUN" = "1" ] && echo ' (DRY_RUN)')"
