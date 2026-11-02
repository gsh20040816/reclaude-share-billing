# reclaude-share-billing

Self-hosted seat sharing + usage billing for a handful of [ReClaude](https://reclaude.ai) accounts
shared by several people.

ReClaude hands each user a *real* Anthropic subscription account and lets the Claude Code CLI
connect to it directly — there is no model-facing API key to resell. What you *do* get is a pool
of device slots per account (4 devices on a dedicated plan) and a per-request usage log in the
web dashboard. This project turns that into a fair carpool: assign seats, meter usage per person,
evict the least-recently-used device when slots run out, and split the monthly account cost by
weighted usage.

> Status: **design stage.** `DESIGN.md` is the spec; `rcapi.py` is a working client for the
> dashboard API (verified read-only against a live account). The collector, evictor and Telegram
> bot are the next step (see §9 of the design).

## Why it is not trivial

- The dashboard API is **undocumented**. Endpoints here were recovered from the web bundle and
  confirmed with live calls; they can change without notice.
- Session cookies **rotate on every response**, so all calls for one account must be serialized
  and the cookie written back after each response.
- Device records disappear from the device list once revoked, but the request log keeps their
  `device_id` — so attribution has to be stored locally *before* a device goes away.
- `cost_total_usd` is priced **asynchronously** (`pricing_status=pending` → `null` for fresh
  requests), so a second pass backfills prices.

## Design highlights

- **Meter**: per-request rows keyed by ReClaude's request id → natural idempotency, no double billing.
- **Weighted usage**: `cost_usd × multiplier(model)` — some models consume hidden quota beyond list price.
- **Cost split**: `unit_price = account_period_cost / Σ weighted_usage`, so every user's share sums
  exactly to what the account actually cost. No markup, no leakage.
- **Eviction**: only when a new login needs a slot; victim must be idle ≥ `MIN_IDLE` (30 min) and is
  read (usage flushed to the ledger) *before* revoke.
- **Entrypoint**: Telegram bot, admin + user roles, chat-id allowlist.

See [`DESIGN.md`](DESIGN.md) for the full specification and the API appendix.

## Layout

```
DESIGN.md     specification (Chinese)
rcapi.py      dashboard API client: session cookie, devices, usage, request log, revoke, approval
```

## Legal / ToS

ReClaude's own terms govern the accounts. This repo is an accounting layer for accounts you
legitimately control; it does not bypass quotas, proxy traffic, or share accounts across more
devices than a plan allows at once.
