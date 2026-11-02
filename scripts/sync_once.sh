#!/usr/bin/env bash
# 只跑一轮采集（适合交给 cron / 定时任务）。输出 JSON 结果。
set -euo pipefail
cd "$(dirname "$0")/.."
export RC_BILLING_DB="${RC_BILLING_DB:-/opt/data/state/reclaude-billing/billing.db}"
exec python3 -m app.cli sync "$@"
