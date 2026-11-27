#!/bin/zsh
# 每日早上 09:00 自动拉起 Trade Journal（8796）并弹出「晨间 K 线快照 + 今日交易计划」
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/.trade-journal/logs"
mkdir -p "$LOG_DIR"

# 读取用户 ~/.zshrc 中的 GOOGLE_API_KEY（若尚未在环境里）
if [[ -z "${GOOGLE_API_KEY:-}" && -f "$HOME/.zshrc" ]]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+(GOOGLE_API_KEY|GEMINI_API_KEY)=' "$HOME/.zshrc" 2>/dev/null || true)"
fi

PYTHON_BIN="/usr/local/bin/python3"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"

# 1. 确保 Trade Journal 8796 在线（绝不占用 8792）
if ! curl -fsS --max-time 2 "http://127.0.0.1:8796/api/bootstrap" >/dev/null 2>&1; then
  cd "$PROJECT_DIR"
  nohup env PYTHONPATH="$PROJECT_DIR/src" "$PYTHON_BIN" -m trade_journal.server serve --port 8796 \
    >>"$LOG_DIR/server.log" 2>&1 </dev/null &
  for _ in {1..15}; do
    sleep 0.5
    if curl -fsS --max-time 2 "http://127.0.0.1:8796/api/bootstrap" >/dev/null 2>&1; then
      break
    fi
  done
fi

# 2. 预热今日体温计 + 周线 K 线缓存，保证弹窗秒开
curl -fsS --max-time 45 "http://127.0.0.1:8796/api/morning/summary?force=true" >/dev/null 2>&1 || true
curl -fsS --max-time 45 "http://127.0.0.1:8796/api/morning/charts?interval=1wk&force=true" >/dev/null 2>&1 || true

# 3. 自动在桌面默认浏览器弹出晨间快照页 + 轻提醒
open "http://127.0.0.1:8796/?view=morning"
osascript -e 'display notification "先看大盘与板块周线，再花 30 秒签下今日交易计划（Do Nothing 也是计划）" with title "☀ Trade Journal · 晨间 K 线快照"' >/dev/null 2>&1 || true
