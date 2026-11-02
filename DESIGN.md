# ReClaude 多账号合租计费系统 — 设计文档

版本：v1.1（2026-11-02）
状态：设计定稿并已实现（见 README 与 `app/`）
形态：Telegram Bot（管理员 / 用户两种角色）+ SQLite + 定时采集器

**已确认的参数**：轮询 5 分钟、`MIN_IDLE` 30 分钟、倍率按模型全局配置、计费为租期成本按加权用量分摊（不加价）、入口只做 Telegram Bot。

---

## 1. 目标与边界

**要解决的问题**：少数几个 ReClaude 账号（每个账号有设备名额上限，实测独享账号每号 4 台），
分配给多个用户使用。系统负责：分配席位、按用量记账、名额满时把最久不用的设备挤下线。

**不做的**：
- 不代理流量、不做模型转发（ReClaude 本身是直连官方，本地 CLI 持有设备密钥）
- 不需要 ReClaude 发 API key（它没有给模型用的 key，只有只读的个人 key）
- 不做支付通道（账单出数即可，收款线下/另说）

**关键约束（实测得出，决定了整个架构）**：
1. 设备/用量/撤销/批准登录都在**后台会话 API**（cookie 鉴权）里，不是公开 API
2. 会话 cookie **每次响应都轮转**，必须串行 + 每次回写，不能并发复用
3. 请求流水带唯一 id，可以精确制导地幂等增量采集
4. 设备被撤销后从设备列表消失，但**请求流水与用量聚合仍然可查**
5. 请求的 `cost_usd` 是**异步定价**的：新记录 `pricing_status=pending`、cost 为 null

---

## 2. 角色与权限

| 角色 | 能力 |
|---|---|
| 用户 | 提交自己的 `reclaude login` 授权链接；查看自己的设备、用量、账单 |
| 管理员 | 用户全部能力 + 提交/删除 ReClaude 账号 + 撤销任意设备 + 手动触发采集 + 出全员账单 |

**入口限制**：Bot 只在白名单里的 chat_id 上响应（管理员群 + 各用户私聊）。非白名单消息一律丢弃并记一行日志，
不回复、不提示，避免暴露 bot 存在。

**凭据处理**：管理员提交的账号密码，Bot 收到后**立即删除该条消息**（`deleteMessage`），
加密后落库（Fernet，密钥在 `/opt/data/.env`，库文件 0600）。日志里永不出现明文密码。

---

## 3. 总体架构

```
                        ┌──────────────────────────────┐
   Telegram  ──────────▶│  bot（权限/对话状态机）       │
                        └───────┬──────────────┬───────┘
                                │              │
                    写操作(加/删账号/revoke)   读(用量/账单)
                                │              │
                        ┌───────▼──────────────▼───────┐
                        │  core                           │
                        │  ├ session pool（每账号会话）   │
                        │  ├ enroll（批准登录→登记设备）  │
                        │  ├ syncer（增量拉请求流水）      │
                        │  ├ evictor（LRU 抢占下线）       │
                        │  └ billing（聚合出账）           │
                        └───────┬──────────────┬─────────┘
                                │              │
                          ┌─────▼─────┐  ┌─────▼──────┐
                          │  SQLite   │  │ ReClaude   │
                          │ billing.db│  │ 后台 API   │
                          └───────────┘  └────────────┘
```

一个进程即可（bot 长轮询 + 后台线程跑采集循环），拆开也无妨。

### 3.1 会话池（session pool）

每个 ReClaude 账号一份状态：

- `email` / `password_enc`（Fernet 密文）
- `cookie`（当前会话 cookie，落盘 0600）
- `lock`（**每账号一把锁**，所有 API 调用串行；cookie 轮转决定了这是硬约束）
- `status`：`ok` / `cookie_expired` / `mfa_required` / `banned` / `error`

行为：
1. 调 API 前加锁 → 用当前 cookie 请求 → 响应后**立刻回写 cookie**
2. 收到 401 → 用存储的密码重新登录 → 成功则继续，失败则把 `status` 置为 `cookie_expired` 并通知管理员
3. 遇到 `mfa_required` → `status=mfa_required`，该账号不参与新分配，管理员需人工处理

> 为什么不只用 cookie：cookie 过期/丢失时需要静默恢复，所以必须存密码（加密）。
> 为什么不只用密码：每次登录都会让旧会话失效，频繁登录容易踩风控，也拖慢采集。

### 3.2 采集器（syncer）

**按账号全局时间窗拉，不按设备拉** —— 这样设备在两次轮询之间被撤销也不丢账。

```
for account in accounts(status=ok):
    since = max(watermark.last_synced_at - OVERLAP, now - BACKFILL_MAX)
    until = now
    page  = history?limit=200&since=..&until=..
    loop:
        INSERT OR IGNORE INTO requests(...)        # 幂等：id 唯一
        cursor = next_cursor; if none: break
    更新 watermark（last_synced_at / last_request_id）

    # 归属：每次采集都尝试把未归属的请求挂到人头上
    UPDATE requests SET user_id = (
        SELECT user_id FROM devices WHERE devices.rc_device_id = requests.rc_device_id
    ) WHERE user_id IS NULL

    # 价格回填（异步定价）
    for row in requests where pricing_status='pending' and created_at > now - PRICE_BACKFILL_WINDOW:
        重新拉该时间窗 → UPDATE 已存在行的 cost_usd / pricing_status / deduction_status
```

参数建议：
- 轮询间隔 `SYNC_INTERVAL = 5min`
- `OVERLAP = 10min`（兜住乱序落库/延迟可见）
- `BACKFILL_MAX = 24h`（水位丢失时的最大回补，防止一次拉爆）
- `PRICE_BACKFILL_WINDOW = 24h`，超过仍 pending 的记录标记 `stale`，账单里单列

**幂等性**：请求 `id` 是主键，重复拉取不会重复计费。用户要求"拉到本地后不再重复拉取"——
按 id 去重即满足；但 `pending → priced` 的价格回填必须重读同一批 id，这是同一条记录的**状态更新**，不是重复计费。

**失败策略**：尽力而为。单个账号失败不影响其它账号；失败记 `events`，下一轮自然重试。
连续失败 N 次（默认 3）通知管理员。

### 3.3 设备与归属

`devices` 表是"device_id → 人"的唯一映射来源。

- 用户登录成功（approve 之后）**立刻**登记：`INSERT INTO devices(rc_device_id, account_id, user_id, name, sk_prefix, ...)`
- 采集器每轮刷新 `last_used_at`（来自 `GET /api/app/devices`）
- 设备从设备列表消失（被撤销/删除）→ 本地 `state='revoked'`，**记录不删**，历史请求继续归到这个人头上

> 为什么必须登录时登记：设备一旦消失就无法回填。实测 `54254`、`54243` 已经不在设备列表里，
> 但它们的请求（6 条 / 2 条）仍能在流水和用量聚合里查到 —— 归属只能靠撤销前写在本地的那一行。

### 3.4 开通器（enroll）

```
用户私聊 bot：/login <reclaude login 打印的授权链接 或 只给 state>
  ↓
parse state → POST /api/cli/auth/describe {state}
  → {hostname, os, arch, client_ip_masked, client_user_agent, expires_at, org_candidates, reuse_by_org}
  ↓
选账号 pick_account()：
  1) active 账号中，own_org 名额 used < max 的，取 used 最小
  2) 没有 → 找 min_idle 满足的 LRU 候选，先下线再分配
  3) 都没有 → 排队，告诉用户当前满员，空出后自动批准（state 有效期通常很短，实际做法是请用户稍后重发链接）
  ↓
POST /api/cli/auth/approve {state, device_name=f"{user_handle}", org_id=account.own_org_id}
  ↓
轮询 devices（最多 30s）找到新记录（name 匹配 + created_at > now-5min）→ 登记 device_id → 回复用户
```

**组织选择**：固定取该 ReClaude 账号**自己的那个 org**（`/api/app/orgs/` 里 `current_account.status=bound` 的那条，
即该账号订阅所挂的组织），即使这个账号加入过别的组织、在别的组织里也有绑定。
这样名额与用量都落在同一个可预期的池子里，计费口径干净。

### 3.5 下线器（evictor）

**触发**：只在 `pick_account` 发现某账号名额已满、且需要给新登录腾位置时触发。
也支持管理员手动 `/revoke <device_id>`。

**选受害者**：
```
候选 = 该账号 state='active' 的设备的
       AND now - last_used_at >= MIN_IDLE      # 用户要求：至少闲置一定时间
       AND pinned = 0
       AND rc_device_id != 本次登录将要新建的设备
victim = 候选按 last_used_at 升序的第一个（最久不用）
无候选 → 拒绝分配（不硬挤正在干活的人）
```

`MIN_IDLE` 默认 **30 分钟**（可配）。

**执行顺序（fail-closed，绝不先下线后记账）**：
```
1. 定向采集该设备：history?since=设备水位&device_id=victim  → 入库（尽力而为）
2. 立即再拉一次 until=now，确认已入账到"下线前一刻"
3. 本地标记 state='evicting'（幂等，避免并发重复撤销）
4. POST /api/app/devices/{id}/revoke
5. state='revoked'，写 events（谁、何时、用量快照、触发原因）
任一步骤 1/2 失败 → 仍继续 revoke（用户的诉求是腾位置），但事件里标 `billing_gap=true`
   —— 因为设备撤销后流水仍可查，下一轮全局采集会补齐，不存在真丢账
```

> 这与"下线前读用量"的诉求一致：第 1、2 步就是下线前的读；而第 3 步之后的兜底（撤销后流水仍可查）
> 让"尽力而为"不会变成"漏账"。

---

## 4. 数据模型（SQLite）

```sql
-- 账号
CREATE TABLE rc_accounts (
  id            TEXT PRIMARY KEY,          -- 短标签，如 "ultra-a"
  email         TEXT NOT NULL,
  password_enc  TEXT,                      -- Fernet 密文
  cookie        TEXT,                      -- 当前会话 cookie
  status        TEXT NOT NULL DEFAULT 'ok',-- ok|cookie_expired|mfa_required|error
  own_org_id    INTEGER,                   -- 该账号自己的组织（分配时固定用这个）
  tier          TEXT,                      -- max_20x / max_5x / ...
  capacity      INTEGER,                   -- 设备名额上限（来自 devices/quota.max）
  last_login_at TEXT,
  last_error    TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);

-- 设备（device_id → 人 的唯一映射）
CREATE TABLE devices (
  account_id    TEXT NOT NULL,
  rc_device_id  INTEGER NOT NULL,
  user_id       INTEGER,                   -- 登录时登记
  name          TEXT,
  sk_prefix     TEXT,
  org_id        INTEGER,
  platform      TEXT,
  client_version TEXT,
  first_seen_at TEXT,
  last_used_at  INTEGER,                   -- 毫秒时间戳，来自 API
  state         TEXT NOT NULL DEFAULT 'active', -- active|evicting|revoked|missing
  pinned        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, rc_device_id)
);

-- 请求流水（唯一真相）
CREATE TABLE requests (
  id             INTEGER PRIMARY KEY,      -- ReClaude 请求 id
  account_id     TEXT NOT NULL,
  rc_device_id   INTEGER NOT NULL,
  user_id        INTEGER,                  -- 先留空，采集时按 devices 回填
  org_id         INTEGER,
  created_at     INTEGER NOT NULL,         -- 毫秒
  model          TEXT,
  request_kind   TEXT,
  status         INTEGER,
  is_stream      INTEGER,
  duration_ms    INTEGER,
  input_tokens   INTEGER, output_tokens INTEGER,
  cache_create_5m_tokens INTEGER, cache_create_1h_tokens INTEGER,
  cache_read_input_tokens INTEGER, total_tokens INTEGER,
  cost_usd       REAL,                     -- pending 时为 NULL
  pricing_status TEXT,                     -- priced|pending|ignored|not_billable
  deduction_status TEXT,
  tracking_id    TEXT,
  first_seen_at  TEXT NOT NULL,
  priced_at      TEXT
);
CREATE INDEX idx_req_user_time ON requests(user_id, created_at);
CREATE INDEX idx_req_device ON requests(rc_device_id);

-- 采集水位
CREATE TABLE watermarks (
  account_id       TEXT PRIMARY KEY,
  last_synced_at   INTEGER,                -- 毫秒
  last_request_id  INTEGER,
  updated_at       TEXT
);

-- 模型倍率（加权用量用）
CREATE TABLE model_rates (
  pattern    TEXT PRIMARY KEY,             -- 精确模型名 or 通配 claude-opus-*
  multiplier REAL NOT NULL DEFAULT 1.0,    -- 隐藏倍率：真实额度消耗 / 标价成本
  note       TEXT,
  updated_at TEXT NOT NULL
);

-- 账号租期与成本（管理员配置）
CREATE TABLE account_periods (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id  TEXT NOT NULL REFERENCES rc_accounts(id),
  start_date  TEXT NOT NULL,               -- YYYY-MM-DD，含当天
  end_date    TEXT NOT NULL,               -- YYYY-MM-DD，含当天
  cost_cny    REAL NOT NULL,               -- 该期总成本（人民币）
  idle_split  TEXT NOT NULL DEFAULT 'none',-- 期内零用量时的处理：none|even
  note        TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE(account_id, start_date)           -- 同期不可重复；重叠由应用层校验
);

-- 账单（出账即快照，改配置不回溯）
CREATE TABLE invoices (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL,
  period_start TEXT NOT NULL,
  period_end   TEXT NOT NULL,
  total_cny    REAL NOT NULL,
  status       TEXT NOT NULL,              -- provisional|final
  created_at   TEXT NOT NULL,
  note         TEXT
);
CREATE TABLE invoice_lines (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id      INTEGER NOT NULL REFERENCES invoices(id),
  account_id      TEXT NOT NULL,
  weighted_usd    REAL NOT NULL,           -- Σ(cost_usd × multiplier)
  raw_usd         REAL NOT NULL,           -- Σ cost_usd（未加权，便于对账）
  requests        INTEGER NOT NULL,
  unit_price_cny  REAL NOT NULL,           -- C / Σw（该账号该期）
  amount_cny      REAL NOT NULL,
  note            TEXT
);

-- 用户 / 权限
CREATE TABLE users (
  tg_user_id INTEGER PRIMARY KEY,
  handle     TEXT,
  role       TEXT NOT NULL DEFAULT 'user', -- user|admin
  status     TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

-- 登录会话（待批准的 state）
CREATE TABLE login_states (
  state       TEXT PRIMARY KEY,
  tg_user_id  INTEGER NOT NULL,
  account_id  TEXT,
  status      TEXT NOT NULL,               -- pending|approved|expired|failed
  hostname    TEXT, os TEXT, arch TEXT, client_ip TEXT,
  expires_at  INTEGER,
  created_at  TEXT NOT NULL,
  note        TEXT
);

-- 审计
CREATE TABLE events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  actor     TEXT,                          -- tg:123 / system
  kind      TEXT NOT NULL,                 -- account_add|account_del|approve|revoke|sync|error
  account_id TEXT, rc_device_id INTEGER, user_id INTEGER,
  detail    TEXT                           -- JSON
);
```

---

## 5. 计费：模型倍率 + 账号租期成本分摊

### 5.1 模型倍率（加权用量）

ReClaude 给的 `cost_usd` 是 Anthropic 的**标价**，但订阅额度的**实际消耗不等于标价**：
部分模型在额度上有隐藏倍率（新模型、长上下文、缓存命中率低的请求尤其明显）。
所以计费单位不是美元成本，而是**加权用量**：

```
w(req) = cost_usd × multiplier(model)
```

倍率解析顺序：精确匹配 `model` → 通配 `claude-opus-*` → `default`（默认 1.0）。
**倍率按模型全局配置，不区分账号/套餐**（已确认）。
未配置的模型用 default，但在报告里单列「未配置倍率的模型」，提示管理员补 —— 否则它会被低估。

实测出现的模型（7 天采样 2000 条，未跑完）：

- `claude-fable-5-1`：1404 条，$313.13
- `claude-opus-5-5`：433 条，$70.01
- `claude-haiku-5-5`：141 条，$0.061
- `claude-haiku-4-5-20251001`：22 条，$0.000

> `cost_usd` 里已经含了 input/output/缓存分项的价差，所以倍率乘在 `cost_usd` 上即可，
> 不需要再对 token 分项分别乘，也不需要在采集时落倍率 —— 倍率**只在上账时参与计算**，
> 改倍率不用重采、不用回填历史记录。

### 5.2 账号租期与成本

管理员为每个账号配置租期与当期成本（支持续租、支持同账号多期历史）：

```
account_id × [start_date, end_date] × cost_cny
```

期是分摊的边界：每期单独算单价，跨期请求按 `created_at` 落到所属期。

### 5.3 分摊公式

对每个账号期：

```
p   = cost_cny / (Σ w(已归属) + Σ w(未归属))     # 分母含未归属用量
用户账单 += Σ_{该账号该期} w(该用户) × p
待认领   += Σ w(未归属) × p                       # 单独挂账，不计入任何个人
```

性质：

- 该期已归属用户金额之和 + 待认领份额 **恰好等于账号成本**（不多不少），这是合租分摊而不是加价转售
- 未归属（`user_id IS NULL`）的用量同样进分母，其份额作为「待认领」挂在账单上，**不转嫁给已归属用户**
- 倍率改变的是「谁承担多少」，不改变总额 —— 所以倍率配错只会让人头之间失衡，不会让总额错
- 空转成本按在场的人按比例承担；若希望"没人用时成本不摊"，该期 `idle_split='even'` 改成人均

### 5.4 边界与规则

- **期未结束** → 出 `provisional` 账单（按当前 Σw 分摊），期末重算覆盖
- **Σw = 0**（期内完全没用）→ 单价无定义：`idle_split='none'` 该期不摊，`'even'` 按席位人均
- **账号没配租期** → 其用量无法定价，账单标 `incomplete` 并列出缺配置的账号（不静默按 0 算）
- **期重叠** → 拒绝写入；**期之间的空档**里的请求 → 进「未覆盖」清单，不猜归属
- **出账即快照**：`invoice_lines` 存当时的加权用量、单价、金额。之后改倍率/改租期**不回溯**已出账期；
  同一区间重复出账覆盖旧快照，不产生第二套账单
- **未定价请求**：`pending` 超 24h 的按 tokens 估（若配了 token 单价）或标 `stale` 单列；
  `ignored` / `not_billable` 的请求直接排除（官方明确不计费），也不计入未定价/超时统计
- **出账前**调一次 `POST /api/app/account/usage/refresh`，把官方 /usage 快照刷到最新


---

## 6. Bot 接口

**用户**（私聊）
- `/login <授权链接|state>` — 登记本机
- `/me` — 我的设备、本月用量（USD / 请求数 / 未定价条数）
- `/devices` — 我的设备列表（名称、最近使用、状态）
- `/help`

**管理员**
- `/accounts` — 账号列表：状态、名额 used/max、会话健康、租期与成本、最近采集时间
- `/account_add` — 交互式：邮箱 → 密码（消息即时删除）→ **租期起止 + 当期成本** → 落库 → 立即尝试登录验证
- `/account_del <id>` — 删除账号（保留历史流水与已出账单，只停用；真删需二次确认）
- `/period add <账号> <起> <止> <¥>` — 追加/续租一期
- `/periods [账号]` — 查看租期与成本，标出重叠/空档/未配置
- `/rate <模型|通配> <倍率>` — 配置模型隐藏倍率；`/rate default 1.0` 设兜底
- `/rates` — 倍率表 + **实际出现但未配置倍率的模型**（按用量排序）
- `/revoke <rc_device_id>` — 任意 revoke（同样走下线的定向采集流程）
- `/sync [account]` — 手动触发采集
- `/report [起] [止]` — 全员账单（含 incomplete / 未覆盖 / 未定价 三张清单）
- `/queue` — 当前排队等席位的人


---

## 7. 失败模式与边界

| 情况 | 处理 |
|---|---|
| cookie 失效 (401) | 用密文密码重登录；连续失败 → 账号 `cookie_expired`，通知管理员 |
| 账号开了 MFA | 登录返回 `mfa_required` → 账号停用，管理员人工处理（本期不做 TOTP 自动化） |
| 账号被换号/封号 | 设备与 org 可能变化，`/accounts` 显示异常，需管理员确认；流水不受影响 |
| 设备被意外撤销 | 设备从列表消失，本地标 `revoked`，历史请求继续归人；全局采集照常补账 |
| 请求 pending 太久 | 超过 24h 标 `stale`，账单单列，不阻塞出账 |
| API 变更 | 防御式解析（字段缺失走默认），原始响应存 `events.detail` 便于回归；4xx/5xx 报警 |
| 并发撤销同一设备 | `state='evicting'` + 每账号锁，幂等 |
| 采集被限流 | 每账号串行、5 分钟一次，远低于 1500/窗口；遇 429 退避 |
| org 用量 > 活跃设备之和 | 已知现象（实测 0.23 差额），账单以请求流水为准，差额不进个人账 |

---

## 8. 非公开 API 附录（实测 2026-11-02）

> **这些接口没有任何官方文档**。来源：`www.reclaude.ai` 前端 bundle 分析 + 用真实账号黑盒实测。
> 随时可能变更，必须防御式解析。

鉴权：`POST /api/auth/login {email,password}` → 会话 cookie（`rc_sid`），**每次响应轮转**。

| 端点 | 说明 |
|---|---|
| `GET /api/app/me` | 订阅、当前账号状态、`usage_snapshot.limits`（5h/周额度百分比与重置时间） |
| `GET /api/app/orgs/` | 组织列表、`default_org_id`、各 org 的绑定账号与套餐 |
| `GET /api/app/devices` | 设备列表：`id,name,org_id,sk_prefix,last_used_at,last_used_ip,revoked_at,machine_summary,created_at` |
| `GET /api/app/devices/quota` | `{max,used,account_max,account_used,by_platform[]}`，名额是**账号级** |
| `GET /api/app/usage/stats?range=&device_id=&org_id=` | `overview{sessions,messages,total_usd,total_tokens,各 token 分量,heatmap[按天]}`；`breakdown/models` 仅 org 维度有 |
| `GET /api/app/history/messages?limit=&since=&until=&cursor=&device_id=&request_kind=&status=&model=&tracking_id=&id=` | 请求流水 `{items[],next_cursor}` |
| `POST /api/app/devices/{id}/revoke` | 下线，立即失效 |
| `POST /api/cli/auth/describe {state}` | 登录授权详情（hostname/os/arch/ip/expires_at/org_candidates/reuse_by_org） |
| `POST /api/cli/auth/approve {state,device_name,org_id}` | 批准登录 = 占用一个设备名额 |
| `POST /api/app/account/usage/refresh?org_id=` | 刷新官方 /usage 快照（写操作，仅出账前调用） |
| `GET /api/v1/orgs`、`GET /api/v1/carpool/quota` | **个人 API key**（Bearer）可用的全部只读端点，不足以支撑本系统 |

请求流水字段：`id, tracking_id, device_id, device_name, account_id, org_id, created_at, model,
request_kind, status, is_stream, duration_ms, input_tokens, output_tokens, cache_create_5m_tokens,
cache_create_1h_tokens, cache_read_input_tokens, total_tokens, cost_*_usd, cost_total_usd,
pricing_status, deduction_status, platform, updated_at`

**坑位清单**：
- `limit` 上限 200（传 500 被截断），`offset` 被忽略
- 翻页只认响应里的 `next_cursor` 令牌；传 `id` 会静默回到第一页
- `device_id` 只接受单个数字；逗号拼接返回 400；`device_id[]=` 被忽略
- `cost_total_usd` 仅在 `pricing_status='priced'` 时存在；`pending` 为 null，`ignored` 不计价
- 设备撤销后 `/api/app/devices` 里没有它，但 `history`/`usage/stats` 按 `device_id` 仍可查（实测）

---

## 9. 实施分期

**P1（可用最小集）**
- 会话池、账号增删（含租期与成本录入）、用户提交链接 → 自动批准 → 登记设备
- 采集循环（请求流水 + 归属回填 + 价格回填）
- 模型倍率表 + 租期成本分摊出账（含 provisional / incomplete / 未覆盖 / 未定价 四类标记）
- LRU 下线（含下线前定向采集）
- `/me` `/accounts` `/revoke` `/report` `/rate` `/period` 六个命令


**P2**
- 排队机制（满员时记下 state，空出后提示用户重发）
- 出账前 `usage/refresh` 强制同步
- 异常告警（账号失效、连续采集失败、API 结构变化）

**P3**
- 网页自助（如果用户量上来，Telegram 输入链接变瓶颈）
- pending 记录的本地定价估算

---

## 10. 实现状态（v1.1）

| 设计条目 | 代码 |
|---|---|
| 会话池（锁 / cookie 回写 / 401 重登） | `app/sessions.py` |
| 增量采集 + 归因 + 补价 | `app/syncer.py` |
| 选账号 + approve + 登记设备 + `/bind` | `app/enroll.py` |
| 闲置门槛 + LRU 下线 + 下线前定向采集 | `app/evictor.py` |
| 模型倍率 + 租期成本分摊 + 账单快照 | `app/billing.py` |
| Telegram Bot（管理员/用户/白名单/演练） | `app/bot.py` |
| 运维 CLI（同一套 core） | `app/cli.py` |
| 离线测试（FakeClient / 临时库） | `tests/`（多个离线测试套件，全绿） |

**验收对账**：真实账号 7 天窗口采集 11,241 条请求；按设备汇总与后台 `usage/stats` 的
`total_usd` 逐台一致（差额 0.0000），含两台已撤销、已从设备列表消失的设备。

**未实现 / 有意留白**：MFA 账号自动登录；Telegram 满员排队（state 几分钟即过期，实际做法是让用户重发）；
全员账单的收款闭环；`pricing_status` 中 `ignored` / `not_billable` 的本地重算（现直接排除）。

