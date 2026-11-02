# reclaude-share-billing

多人合租几个 [ReClaude](https://reclaude.ai) 账号的**席位分配 + 用量计费**系统：
选账号、批准登录、记 device_id、按请求流水记账、名额满了把最久不用的设备挤下线、按租期成本分摊出账。

Seat sharing + usage billing for shared ReClaude accounts: assign devices, meter per-request usage,
evict the least-recently-used device when slots run out, split the account cost by weighted usage.

形态：Telegram Bot（管理员 / 用户）+ SQLite + 定时采集器。零第三方框架依赖（只有 `cryptography`）。

```
用户 ──/login 授权链接──▶ Bot ──approve──▶ ReClaude ──▶ 设备绑定到人
                            │
                            └── 每 5 分钟 ──拉请求流水──▶ SQLite ──▶ 账单
```

## 为什么不是"中转计费"

ReClaude 不发模型 API key：它给每个人分配真实的 Anthropic 订阅账号，Claude Code 直连官方。
能用来计费的只有两样东西 —— **设备名额**（每账号 4 台）和**后台的请求流水**（每条带唯一 id、token 分项和美元成本）。

## 已实现的能力

- **账号池**：管理员提交邮箱+密码，密码 Fernet 加密落库；每账号一把锁串行调用，会话 cookie 每次响应回写，401 自动用密码重登
- **开通**：用户发 `reclaude login` 的授权链接 → `describe` 取机器信息 → 选账号 → `approve`（设备名=用户）→ 登记 `device_id`；设备延迟出现时批准后自动认领，历史 `unknown` 设备不占席位
- **抢占**：名额满时选「闲置 ≥ 30 分钟」且最久未用的设备，**先定向采集它的用量**再 revoke（fail-closed）
- **采集**：按账号时间窗翻页拉请求流水，请求 id 主键幂等；每轮把未归因请求回填到人；`pending` 定价自动补价
- **计费**：`加权用量 = cost_usd × 模型倍率`，`单价 = 租期成本 ÷ Σ加权（含未归属用量）`；已归属用户金额之和 + 待认领份额 = 账号成本，未归属用量不转嫁给其他人
- **Bot**：管理员 + 用户命令，白名单 chat，密码消息即时删除，写操作默认 `dry_run`

## 实测对账

拿一个真实账号跑 7 天窗口：**11,241 条请求，57 页**，本库按设备汇总与后台 `usage/stats` 的 `total_usd` **逐台完全一致**（差 0.0000），
包括两台**已 revoke、已从设备列表消失**的设备（54254 / 54243）—— 设备撤销后流水仍可查，账不会丢。

## 快速开始

```bash
pip install cryptography            # 唯一外部依赖

# 1) 配置
mkdir -p /opt/data/state/reclaude-billing
cp config.example.json /opt/data/state/reclaude-billing/config.json
#    填 bot_token（@BotFather 新建 bot）、admin_ids、allowed_chat_ids

# 2) 加账号（交互式也可在 bot 里 /account_add）
python3 -m app.cli account-add --id ultra-a --email you@example.com --password '***' \
    --start 2026-11-01 --end 2026-11-30 --cost 2200

# 3) 配置模型倍率
python3 -m app.cli rate 'claude-opus-*' 1.5
python3 -m app.cli rate default 1.0

# 4) 跑一轮采集 + 出账
python3 -m app.cli sync
python3 -m app.cli report --start 2026-11-01 --end 2026-11-30

# 5) 起 bot（长轮询 + 后台采集循环）
scripts/run_bot.sh
```

## Bot 命令

用户：`/login <授权链接>` `/me` `/devices` `/bill [起 止]` `/help`

管理员：`/accounts` `/account_add` `/account_del` `/period add` `/periods` `/rate` `/rates`
`/revoke <账号:设备id>` `/bind <账号:设备id> <tg_user_id>` `/sync` `/report [起 止] [save]` `/health` `/dryrun on|off`

## 计费口径

```
每条请求     w = cost_usd × multiplier(model)       # 隐藏倍率，按模型全局配置
每个账号期   p = 租期成本 ÷ Σw                       # 分母含未归属（user_id 为空）的 w
每人账单     Σ_账号 ( 自己的 Σw × p )
待认领       Σw_未归属 × p                           # 单独挂账，不进个人账单
```

- 总额 = 账号成本（已归属用户之和 + 待认领份额）；倍率只改变"谁承担多少"
- 未归属用量按同一单价折算成「待认领」，不摊到已归属用户头上
- 未配倍率的模型按默认值计，并在 `/rates` 和账单里单列
- `ignored` / `not_billable` 的请求不计费，也不计入未定价/超时统计；`pending` 超窗口仍在账单里单列
- 同一区间重新出账会覆盖旧快照，不会出两套账单
- 期未结束 → `provisional`；账号没配租期 → `incomplete`；请求落在租期外 → `uncovered`

## 已知边界

- 首次自动采集从 `since=0` 开始，拉取上游仍保留的全部历史；完成后才转入水位增量采集。升级后会一次性重置旧采集水位和续页位置，自动补齐过去只采 24 小时的缺口；已有流水、归属和账单保留，按请求 ID 去重。

- Bot 与采集线程使用独立的 SQLite 连接和事务；同账号的采集、撤销、会话操作共用锁。独立进程之间不共享这把锁，不要同时启动多个 bot/CLI 采集进程操作同一账号。
- 登录使用每次授权的随机设备名标记，只自动认领标记一致、批准前不存在的活跃设备。上游改写名称、旧版本遗留待认领记录或等待超过 10 分钟时，需要管理员核对实际设备并 `/bind`；`/health` 会列出待核对用户。
- 全局采集达到页数上限时，将固定时间窗和下一页 cursor 保存到 `sync_progress`，下轮或重启后继续；全部读完才推进水位。升级启动时自动创建此表。
- 自动撤销会重新读取上游设备活跃时间并结合本地最新流水检查；无法确认闲置时取消操作。管理员显式强制撤销仍可绕过闲置检查。

- 后台 API **没有官方文档**（`DESIGN.md` 附录是逆向 + 实测结果），随时可能变；解析是防御式的
- 采集粒度：增量轮询 5 分钟一次；首次回补 7 天约 3.5 分钟（上游延迟，每账号串行）
- 账号开 MFA 时无法自动重登，账号会标记 `mfa_required` 等管理员处理
- 撤销后的设备记录仍在流水里可查，但 `device_id → 人` 的映射必须在撤销前登记（`/bind` 可补）

## 布局

```
DESIGN.md            设计文档（含非公开 API 附录）
rcapi.py             后台 API 客户端
app/store.py         schema / 设置项
app/crypto.py        Fernet 密码加密
app/sessions.py      会话池（锁 + cookie 回写 + 重登）
app/syncer.py        增量采集 / 归因 / 补价
app/enroll.py        选账号 + approve + 登记设备
app/evictor.py       闲置门槛 + LRU 下线 + 下线前采集
app/billing.py       加权用量 + 租期分摊 + 账单
app/bot.py           Telegram Bot
app/cli.py           CLI（运维 + 演练）
tests/               多个离线测试套件（临时库 + FakeClient，不碰真实账号）
```

## 安全

- 密码 Fernet 加密，key 与库分离（`secret.key` 0600）；Bot 收到密码消息后立即删除
- 所有写操作（approve / revoke）默认 `dry_run`，`/dryrun off` 才真实执行
- Bot 只响应白名单 chat，非白名单消息只记一行日志、不回复

## Legal / ToS

ReClaude 的条款管辖这些账号。本项目只是**给你合法持有的账号**做账，不绕过配额、不代理流量、
不在同一时刻超出套餐允许的设备数。
