#!/usr/bin/env bash
# Best-effort Telegram alert. Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from
# the environment, else from the repo .env (grepped, NOT sourced — .env holds
# broker secrets with inline comments). Silently does nothing when unset, and
# NEVER fails the caller: a dead-man's switch must not itself kill the wrapper.
#
# Usage: ./scripts/notify_telegram.sh "message text"
# Deliberately no `set -e`.

msg="${1:-algobots alert}"

token="${TELEGRAM_BOT_TOKEN:-}"
chat="${TELEGRAM_CHAT_ID:-}"

env_file="$(dirname "$0")/../.env"
if { [ -z "$token" ] || [ -z "$chat" ]; } && [ -f "$env_file" ]; then
  [ -z "$token" ] && token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$env_file" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '"'\' | awk '{print $1}')"
  [ -z "$chat" ]  && chat="$(grep -E '^TELEGRAM_CHAT_ID='  "$env_file" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '"'\' | awk '{print $1}')"
fi

if [ -z "$token" ] || [ -z "$chat" ]; then
  # Alerting not configured — no-op success.
  exit 0
fi

curl -sS -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
  -d "chat_id=${chat}" \
  --data-urlencode "text=${msg}" >/dev/null 2>&1 || true
exit 0
