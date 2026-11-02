#!/usr/bin/env bash
# 启动 Telegram bot（长轮询 + 后台采集循环）。前台运行，Ctrl-C 退出。
set -euo pipefail
cd "$(dirname "$0")/.."
export RC_BILLING_CONFIG="${RC_BILLING_CONFIG:-/opt/data/state/reclaude-billing/config.json}"
export RC_BILLING_DB="${RC_BILLING_DB:-/opt/data/state/reclaude-billing/billing.db}"
exec python3 -m app.bot
