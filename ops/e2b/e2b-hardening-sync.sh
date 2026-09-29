#!/bin/bash
# e2b-hardening-sync.sh — 把环境级加固从 git 源同步到本机并应用（服务器侧"自动拉取"的执行者）。
#
# 两条路径，互为兜底：
#   A. 推送触发：开发机把分支推到本机裸镜像（默认 /srv/git/aeval.git），
#      镜像的 post-receive 钩子调用本脚本 → 立即生效（无需任何凭据）。
#   B. 定时自主拉取：本脚本每 5 分钟（e2b-hardening-sync.timer）从远端 fetch 一次；
#      远端是私有仓库，所以只有当 /etc/e2b-hardening/git-token 存在时才尝试，
#      凭据经 0600 的 credential store 读取，绝不出现在命令行或日志里。
#
# 配置：/etc/e2b-hardening/sync.conf（由安装器写入默认值，可改）
# 日志：/var/log/e2b-hardening/sync.log
set -uo pipefail

CONF=${SYNC_CONF:-/etc/e2b-hardening/sync.conf}
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"
MIRROR=${MIRROR:-/srv/git/aeval.git}
BRANCH=${BRANCH:-eval0930}
REMOTE_URL=${REMOTE_URL:-}
TOKEN_FILE=${TOKEN_FILE:-/etc/e2b-hardening/git-token}
CRED_FILE=${CRED_FILE:-/etc/e2b-hardening/git-credentials}
CHECKOUT=${CHECKOUT:-/var/lib/e2b-hardening/checkout}
SUBDIR=${SUBDIR:-ops/e2b}
DEST=${DEST:-/opt/e2b-hardening}
LOG=${LOG:-/var/log/e2b-hardening/sync.log}
FORCE_APPLY=0
# 参数：--force 即使提交未变也重新应用；--branch=<名> 由推送钩子传入（推送哪个分支同步哪个）
for _a in "$@"; do
  case "$_a" in
    --force) FORCE_APPLY=1 ;;
    --branch=*) BRANCH="${_a#--branch=}" ;;
  esac
done

log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
mkdir -p "$(dirname "$LOG")" 2>/dev/null

# 本脚本可能被它自己触发的安装过程替换掉（install 会截断目标文件），
# 所以先把自己快照到临时文件再重入执行，保证后半段读到的还是同一份代码。
if [ -z "${E2B_SYNC_SNAPSHOT:-}" ]; then
  snap=$(mktemp /tmp/e2b-hardening-sync.XXXXXX.sh)
  cp "${BASH_SOURCE[0]}" "$snap"
  E2B_SYNC_SNAPSHOT=1 exec bash "$snap" "$@"
fi
trap 'rm -f "${BASH_SOURCE[0]}" 2>/dev/null' EXIT

# ---------- 1) 取源：远端 fetch（可选） ----------
fetch="n/a"
if [ -n "$REMOTE_URL" ] && [ -d "$MIRROR" ]; then
  if [ -s "$TOKEN_FILE" ]; then
    host=$(printf '%s' "$REMOTE_URL" | sed -E 's#^[a-z]+://([^/]+)/.*#\1#')
    umask 077
    printf 'https://oauth2:%s@%s\n' "$(tr -d '\r\n' < "$TOKEN_FILE")" "$host" > "$CRED_FILE"
    if GIT_TERMINAL_PROMPT=0 git -c "credential.helper=store --file=$CRED_FILE" \
         --git-dir="$MIRROR" fetch --quiet --prune "$REMOTE_URL" \
         "refs/heads/$BRANCH:refs/heads/$BRANCH" 2>>"$LOG"; then
      fetch="远端已拉取"
    else
      fetch="远端拉取失败（继续用镜像已有内容）"
    fi
  else
    fetch="跳过远端拉取（无凭据文件 $TOKEN_FILE）"
  fi
else
  fetch="仅用镜像内容（未配置 REMOTE_URL 或镜像不存在）"
fi

# ---------- 2) 从镜像取出 ops/e2b ----------
if [ ! -d "$MIRROR" ]; then
  log "镜像不存在: $MIRROR（等待开发机推送或安装器初始化）；$fetch"
  exit 0
fi
rev=$(git --git-dir="$MIRROR" rev-parse --verify --quiet "refs/heads/$BRANCH" || true)
if [ -z "${rev:-}" ]; then
  log "镜像里还没有分支 $BRANCH（等待推送）；$fetch"
  exit 0
fi

local_rev=""
[ -f "$CHECKOUT/.e2b-hardening-rev" ] && local_rev=$(cat "$CHECKOUT/.e2b-hardening-rev" 2>/dev/null || true)
if [ "$local_rev" = "$rev" ] && [ "$FORCE_APPLY" != "1" ]; then
  log "已是最新（$BRANCH@${rev:0:12}）；$fetch"
  exit 0
fi

if ! git --git-dir="$MIRROR" --work-tree="$CHECKOUT" checkout -f --quiet "$rev" -- "$SUBDIR" 2>>"$LOG"; then
  log "checkout 失败: $BRANCH@${rev:0:12}"
  exit 1
fi
if [ ! -f "$CHECKOUT/$SUBDIR/install-e2b-hardening.sh" ]; then
  log "取到的提交里没有 $SUBDIR/install-e2b-hardening.sh（跳过）"
  exit 0
fi

mkdir -p "$DEST"
rsync -a --delete "$CHECKOUT/$SUBDIR/" "$DEST/"
printf '%s\n' "$rev" > "$CHECKOUT/.e2b-hardening-rev"
log "已同步 $BRANCH@${rev:0:12} 的 $SUBDIR → $DEST；$fetch"

# ---------- 3) 应用 + 校验 ----------
if bash "$DEST/install-e2b-hardening.sh" --quiet --apply >>"$LOG" 2>&1; then
  log "加固已应用 ✓"
else
  log "应用失败 ✗（详见本日志上文）"
fi
if bash "$DEST/install-e2b-hardening.sh" --check --quiet >>"$LOG" 2>&1; then
  log "漂移检查: 干净 ✓"
else
  log "漂移检查: 仍有漂移 ✗（详见本日志上文）"
fi
exit 0
