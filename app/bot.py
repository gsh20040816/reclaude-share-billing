"""Telegram Bot：管理员 / 用户两种角色，白名单 chat，原生 Bot API（零依赖）。"""
from __future__ import annotations

import html
import json
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from concurrent.futures import ThreadPoolExecutor

from . import billing, enroll, evictor, store, syncer, ledger, activity
from .runtime import Runtime

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3800


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def money(x: float) -> str:
    return f"¥{x:,.2f}"


def usd(x: float) -> str:
    return f"${x:,.2f}"


class Telegram:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, **params) -> dict:
        data = json.dumps(params).encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": exc.read().decode("utf-8", "replace")[:400]}
        except Exception as exc:                 # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def send(self, chat_id: int, text: str, thread_id: int | None = None,
             markup: dict | None = None) -> dict:
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN] + "\n…（已截断）"
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if thread_id:
            params["message_thread_id"] = thread_id
        if markup:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)

    def delete(self, chat_id: int, message_id: int) -> dict:
        return self.call("deleteMessage", chat_id=chat_id, message_id=message_id)


class Bot:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.cfg = rt.cfg
        self.conn = rt.conn
        self.pool = rt.pool
        self.tg = Telegram(self.cfg["bot_token"])
        self.admins = {int(x) for x in self.cfg.get("admin_ids", [])}
        self.allowed = {int(x) for x in self.cfg.get("allowed_chat_ids", [])}
        self.flows: dict[int, dict] = {}
        self._fail_counts: dict[str, int] = {}
        self._stop = threading.Event()
        self._workers = {
            'read': ThreadPoolExecutor(max_workers=4, thread_name_prefix='bot-read'),
            'sync': ThreadPoolExecutor(max_workers=1, thread_name_prefix='bot-sync'),
            'write': ThreadPoolExecutor(max_workers=1, thread_name_prefix='bot-write'),
        }
        self._pending = threading.BoundedSemaphore(64)


    # ---------------------------------------------------------------- 权限
    def is_admin(self, uid: int) -> bool:
        return uid in self.admins

    def allowed_chat(self, chat: dict, uid: int) -> bool:
        if chat.get("type") == "private":
            return not self.allowed or chat["id"] in self.allowed or uid in self.admins
        return chat["id"] in self.allowed

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:
        from .menu import configure
        try:
            configure(self)
        except Exception as exc:
            # A temporary menu API failure must not stop billing/long polling.
            print(f"menu setup failed ({type(exc).__name__}); retry on restart", flush=True)
        threading.Thread(target=self.sync_loop, daemon=True).start()
        offset = None
        while not self._stop.is_set():
            params = {"timeout": 25, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            res = self.tg.call("getUpdates", **params)
            if not res.get("ok"):
                time.sleep(5)
                continue
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                self.dispatch(upd)

    @staticmethod
    def command_lane(upd):
        parts = ((upd.get('message') or {}).get('text') or '').split()
        command = parts[0].split('@')[0].lower() if parts else ''
        if command == '/sync':
            return 'sync'
        if command in {'/start', '/help', '/me', '/devices', '/bill', '/units',
                       '/accounts', '/periods', '/rates', '/health'}:
            return 'read'
        if command == '/report' and not {'save', 'refresh'} & {p.lower() for p in parts[1:]}:
            return 'read'
        return 'write'

    def dispatch(self, upd):
        # Bounded backpressure rather than unbounded queued commands.
        while not self._stop.is_set():
            if self._pending.acquire(timeout=.2):
                break
        else:
            return None
        try:
            return self._workers[self.command_lane(upd)].submit(self._run_update, upd)
        except BaseException:
            self._pending.release()
            raise

    def _run_update(self, upd):
        try:
            self.handle(upd)
        except Exception as exc:
            self.conn.rollback()
            self.reply_error(upd, exc)
        finally:
            self.conn.close()  # transaction/connection belongs to this worker only
            self._pending.release()

    def stop(self) -> None:
        self._stop.set()
        for worker in self._workers.values():
            worker.shutdown(wait=True)

    def reply_error(self, upd: dict, exc: Exception) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            self.tg.send(chat["id"], f"⚠️ 处理失败：<code>{esc(str(exc)[:300])}</code>",
                         msg.get("message_thread_id"))

    def handle(self, upd: dict) -> None:
        msg = upd.get("message")
        if not msg or not msg.get("text"):
            return
        chat, user = msg["chat"], msg["from"]
        uid = user["id"]
        if not self.allowed_chat(chat, uid):
            store.event(self.conn, "rejected_chat", actor=f"tg:{uid}",
                        detail={"chat_id": chat["id"], "type": chat.get("type")})
            return
        text = msg["text"].strip()
        thread = msg.get("message_thread_id")

        # 首次接触自动登记用户
        row = self.conn.execute("SELECT tg_user_id FROM users WHERE tg_user_id=?", (uid,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT OR IGNORE INTO users(tg_user_id, handle, role, status, created_at) VALUES (?,?,?, 'active', ?)",
                (uid, ("@" + user["username"]) if user.get("username") else (user.get("first_name") or str(uid)),
                 "admin" if uid in self.admins else "user", store.iso()))
            self.conn.commit()

        # Read-only and sync commands do not mutate multi-step conversation state.
        # All flow steps and state-changing commands share one ordered writer.
        if self.command_lane(upd) != 'write':
            self.command(chat, msg, uid, text, thread)
            return

        # 多步流程优先
        if uid in self.flows and not text.startswith("/"):
            if self.flow_step(chat, msg, uid, text):
                return
        if text.startswith("/"):
            self.flows.pop(uid, None)
            self.command(chat, msg, uid, text, thread)
        elif uid in self.flows:
            self.flow_step(chat, msg, uid, text)

    # ---------------------------------------------------------------- 命令
    def command(self, chat, msg, uid, text, thread) -> None:
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        handler = {
            "/start": self.cmd_help, "/help": self.cmd_help,
            "/cancel": self.cmd_cancel,
            "/me": self.cmd_me, "/devices": self.cmd_devices, "/bill": self.cmd_bill,
            "/login": self.cmd_login,
            "/renew": self.cmd_renew, "/unit_link": self.cmd_unit_link, "/units": self.cmd_units, "/accounts": self.cmd_accounts, "/account_add": self.cmd_account_add,
            "/account_del": self.cmd_account_del, "/period": self.cmd_period,
            "/periods": self.cmd_periods, "/rate": self.cmd_rate, "/rates": self.cmd_rates,
            "/revoke": self.cmd_revoke, "/bind": self.cmd_bind, "/sync": self.cmd_sync,
            "/report": self.cmd_report, "/health": self.cmd_health,
            "/dryrun": self.cmd_dryrun,
        }.get(cmd)
        if handler is None:
            self.tg.send(chat["id"], "未知命令，发 /help 看用法。", thread)
            return
        if cmd in ("/renew", "/unit_link", "/units", "/accounts", "/account_add", "/account_del", "/period", "/periods",
                   "/rate", "/rates", "/revoke", "/bind", "/sync", "/report", "/health",
                   "/dryrun") \
                and not self.is_admin(uid):
            self.tg.send(chat["id"], "⛔ 仅管理员可用。", thread)
            return
        handler(chat, msg, uid, args, thread)

    # ---------------------------------------------------------------- 用户命令
    def cmd_cancel(self, chat, msg, uid, args, thread) -> None:
        self.flows.pop(uid, None)
        self.tg.send(chat["id"], "已取消当前操作。", thread)

    def cmd_help(self, chat, msg, uid, args, thread) -> None:
        lines = ["<b>ReClaude 合租</b>", "",
                 "<b>用户</b>",
                 "• <code>/login &lt;授权链接&gt;</code> 登记本机（reclaude login 打印的链接）",
                 "• <code>/me</code> 我的设备与本月账单",
                 "• <code>/devices</code> 我的设备列表",
                 "• <code>/bill [起] [止]</code> 我的账单明细",
                 "• <code>/help</code> 本帮助",
                 "• <code>/cancel</code> 取消当前操作"]
        if self.is_admin(uid):
            lines += ["", "<b>管理员</b>",
                      "• <code>/accounts</code> 登录账号池状态",
                      "• <code>/devices</code> 活跃设备；<code>/devices all</code> 含历史；<code>/devices mine</code> 个人设备",
                      "• <code>/units</code> 组织下的计费账号（计费单元ID）",
                      "• <code>/account_add</code> 添加登录凭据（邮箱→密码→标签）",
                      "• <code>/account_del &lt;账号&gt;</code> 停用账号",
                      "• <code>/period add &lt;计费单元ID&gt; &lt;起&gt; &lt;止&gt; &lt;¥&gt; [none|even]</code>",
                      "• <code>/periods [账号]</code> 租期与成本",
                      "• <code>/renew 计费单元ID 天数 金额</code> 接续上一期，精确到分钟",
                      "• 首期自动时间：<code>/period add 计费单元ID auto auto 金额</code>",
                      "• 手动时间：YYYY-MM-DDTHH:MM（北京时间，结束时刻不包含）",
                      "• <code>/rate &lt;模型|通配&gt; &lt;倍率&gt;</code> 设置隐藏倍率",
                      "• <code>/rates</code> 倍率表 + 未配置的模型",
                      "• <code>/revoke &lt;账号:设备id&gt;</code> 下线设备",
                      "• <code>/bind &lt;账号:设备id&gt; &lt;tg_user_id&gt;</code> 手工归因",
                      "• <code>/sync [账号]</code> 立即采集",
                      "• <code>/report [起] [止] [save]</code> 全员账单",
                      "• <code>/dryrun on|off</code> 写操作演练开关",
                      "• <code>/health</code> 账号/采集健康"]
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def _user_devices(self, uid):
        return self.conn.execute(
            "SELECT * FROM devices WHERE user_id=? ORDER BY COALESCE(last_used_at,0) DESC",
            (uid,)).fetchall()

    def cmd_me(self, chat, msg, uid, args, thread) -> None:
        devs = self._user_devices(uid)
        start, end = billing.month_bounds(date.today())
        s_ms, e_ms = billing.day_start_ms(start), billing.day_end_ms(end)
        rows = self.conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) usd,
                      COALESCE(SUM(total_tokens),0) tok
               FROM requests WHERE user_id=? AND created_at>=? AND created_at<=?
                 AND cost_usd IS NOT NULL""", (uid, s_ms, e_ms)).fetchone()
        bill = billing.compute(self.conn, start, end)
        amount = bill["users"].get(uid, 0.0)
        lines = [f"<b>我</b> · tg:<code>{uid}</code>", "",
                 f"本月请求 {rows['n']} 条 · {usd(rows['usd'])} · {rows['tok']:,} tokens",
                 f"本月分摊 <b>{money(amount)}</b>（provisional）", "",
                 f"<b>设备</b>（{len(devs)}）"]
        for d in devs[:10]:
            state = {"active": "在线", "missing": "已消失", "revoked": "已下线",
                     "evicting": "下线中", "unknown": "未认领"}.get(d["state"], d["state"])
            last = time.strftime("%m-%d %H:%M", time.localtime((activity.latest(self.conn, d) or 0) / 1000)) \
                if activity.latest(self.conn, d) else "从未"
            lines.append(f"• <code>{d['account_id']}:{d['rc_device_id']}</code> {state} · 最近 {last}")
        if not devs:
            lines.append("• 还没有设备，发 /login &lt;授权链接&gt; 登记")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_devices(self, chat, msg, uid, args, thread) -> None:
        if args and args not in (['mine'], ['all']):
            self.tg.send(chat['id'], '用法：/devices 查看活跃设备；/devices all 查看全部（管理员）；/devices mine 查看自己的活跃设备。', thread)
            return
        if args == ['all'] and not self.is_admin(uid):
            self.tg.send(chat['id'], '⛔ 仅管理员可查看全部设备。', thread)
            return
        include_history = args == ['all']
        show_all = self.is_admin(uid) and args != ['mine']
        if show_all:
            # One physical device may be visible through multiple credentials.
            # Prefer its owning organization login for the actionable /bind key.
            rows = self.conn.execute("""SELECT d.*, a.own_org_id FROM devices d
                LEFT JOIN rc_accounts a ON a.id=d.account_id
                ORDER BY CASE WHEN d.org_id=a.own_org_id THEN 0 ELSE 1 END,
                         d.account_id,d.rc_device_id""").fetchall()
            unique = {}
            for d in rows:
                unique.setdefault((d['org_id'], d['rc_device_id']), d)
            devs = sorted(unique.values(), key=lambda d: (
                d['state'] != 'active', d['org_id'] or 0, d['rc_device_id']))
        else:
            devs = self._user_devices(uid)
        if not include_history:
            devs = [d for d in devs if d['state'] == 'active']
        if not devs:
            text = ('没有符合条件的设备；可 /sync 刷新，或 /devices all 查看历史记录。' if show_all else
                    '没有绑定到你的设备；已登录设备可能尚未认领，请联系管理员绑定，无需重新登录。')
            self.tg.send(chat['id'], text, thread)
            return
        title = ('全部设备' if include_history else '活跃设备') if show_all else '我的活跃设备'
        suffix = '，含历史记录' if include_history else ''
        lines = [f'<b>{title}</b>（{len(devs)} 台{suffix}）']
        if show_all:
            lines.append('数据来自最近采集；/sync 刷新。active 表示登录有效，不代表正在使用。')
        for d in devs:
            last = time.strftime('%m-%d %H:%M', time.localtime((activity.latest(self.conn, d) or 0) / 1000)) if activity.latest(self.conn, d) else '无记录'
            block = [f"• <code>{esc(d['account_id'])}:{d['rc_device_id']}</code> {esc(d['name'])}",
                     f"   组织 {d['org_id'] or '待回补'} · 状态 {esc(d['state'])} · 最近使用 {last}"]
            if show_all:
                block.append(f"   归属：tg:{d['user_id']}" if d['user_id'] is not None else '   归属：未绑定')
            if len('\n'.join(lines + block)) > 3200:
                self.tg.send(chat['id'], '\n'.join(lines), thread)
                lines = [f'<b>{title}（续）</b>']
            lines.extend(block)
        if show_all:
            lines.append('绑定归属：/bind 账号:设备ID Telegram用户ID\n只修改归属，不会撤销设备。')
        self.tg.send(chat['id'], '\n'.join(lines), thread)

    def cmd_bill(self, chat, msg, uid, args, thread) -> None:
        start, end = self._parse_range(args)
        bill = billing.compute(self.conn, start, end)
        lines = [f"<b>我的账单</b> {start} ~ {end}"]
        mine = [p for p in bill["periods"] if p["user_id"] == uid]
        if not mine:
            lines.append("这段时间没有用量。")
        total = 0.0
        for p in mine:
            total += p["amount_cny"]
            lines.append(f"• {p['account_id']} {p['start']}~{p['end']} · "
                         f"加权 {p['weighted']:.2f} · 单价 {money(p['unit_price'])} → "
                         f"<b>{money(p['amount_cny'])}</b> [{p['status']}]")
        lines += ["", f"合计 <b>{money(total)}</b>"]
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_login(self, chat, msg, uid, args, thread) -> None:
        if not args:
            self.tg.send(chat["id"],
                         "用法：在本机跑 <code>reclaude login</code>，把终端打印的授权链接发给我：\n"
                         "<code>/login https://www.reclaude.ai/cli/auth?state=...</code>", thread)
            return
        raw = " ".join(args)
        m = re.search(r"state=([A-Za-z0-9_\-]+)", raw)
        state = m.group(1) if m else raw.strip()
        if not state or len(state) < 8:
            self.tg.send(chat["id"], "没解析出 state，请把完整链接发来。", thread)
            return
        dry = bool(self.cfg.get("dry_run", True))
        self.tg.send(chat["id"], "⏳ 正在分配席位…", thread)
        try:
            plan = enroll.enroll(self.pool, self.conn, state=state, tg_user_id=uid,
                                 device_name=self._display_name(chat, msg, uid),
                                 dry_run=dry, actor=f"tg:{uid}")
        except enroll.NoSlot as exc:
            self.tg.send(chat["id"], f"⛔ 暂时没有空位：{esc(exc)}\n"
                                     "等有人下线后再发一次链接（授权链接几分钟就过期）。", thread)
            return
        if dry:
            self.tg.send(chat["id"],
                         "🧪 <b>演练模式</b>（未真正批准）\n"
                         f"• 账号 <code>{plan['account_id']}</code>\n"
                         f"• org <code>{plan['org_id']}</code>\n"
                         f"• 设备名 {esc(plan['device_name'])}\n"
                         f"• 机器 {esc(plan.get('hostname'))} / {esc(plan.get('os'))}\n"
                         f"• 需下线：<code>{plan.get('victim') or '无'}</code>", thread)
            return
        if plan.get("pending"):
            self.tg.send(chat["id"], f"✅ 已批准（账号 <code>{plan['account_id']}</code>），"
                                     "回终端完成登录即可。设备归属将在 10 分钟内尝试确认；"
                                     "若仍未登记，请联系管理员核对设备并 /bind。", thread)
        else:
            self.tg.send(chat["id"],
                         f"✅ 已登记\n• 账号 <code>{plan['account_id']}</code>\n"
                         f"• 设备 <code>{plan['account_id']}:{plan['device_id']}</code>\n"
                         f"• 机器 {esc(plan.get('hostname'))}\n\n"
                         "回终端继续，Claude Code 现在应该能跑起来了。", thread)

    def _display_name(self, chat, msg, uid) -> str:
        u = msg.get("from") or {}
        name = u.get("username") or u.get("first_name") or f"tg{uid}"
        return f"{name}@{uid}"

    # ---------------------------------------------------------------- 管理员：账号
    def cmd_accounts(self, chat, msg, uid, args, thread) -> None:
        accs = store.enabled_accounts(self.conn)
        if not accs:
            self.tg.send(chat["id"], "还没有账号。用 /account_add 添加。", thread)
            return
        lines = ["<b>账号池</b>"]
        for a in accs:
            used = enroll.used_slots(self.conn, a["id"])
            cap = a["capacity"] or 4
            wm = self.conn.execute("SELECT last_synced_at FROM watermarks WHERE account_id=?",
                                   (a["id"],)).fetchone()
            synced = time.strftime("%m-%d %H:%M", time.localtime((wm["last_synced_at"] or 0) / 1000)) \
                if wm and wm["last_synced_at"] else "从未"
            lines.append(f"\n<b>{esc(a['id'])}</b> · {esc(a['label'] or '')} · {esc(a['tier'] or '?')}")
            lines.append(f"• 状态 {a['status']} · 名额 {used}/{cap} · 组织 {a['own_org_id'] or '-'}")
            lines.append("• 成本按订阅账号配置，使用 /units 查看")
            lines.append(f"• 最近采集 {synced}")
            if a["last_error"]:
                lines.append(f"• ⚠️ {esc(a['last_error'][:120])}")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_unit_link(self, chat, msg, uid, args, thread):
        if len(args) != 2:
            raise ValueError('用法：/unit_link 计费单元ID 上游账号ID')
        ledger.link_account(self.conn, args[0], int(args[1]))
        self.conn.commit()
        self.tg.send(chat['id'], '已关联到同一席位，保留原成本和历史流水。', thread)

    def cmd_units(self, chat, msg, uid, args, thread) -> None:
        rows = self.conn.execute("SELECT * FROM billing_units ORDER BY org_id,upstream_account_id").fetchall()
        lines = ["<b>计费账号（计费单元ID）</b>"]
        for unit in rows:
            n = self.conn.execute("SELECT COUNT(*) FROM requests WHERE billing_unit_id=?", (unit["id"],)).fetchone()[0]
            sources = self.conn.execute("SELECT login_account_id FROM billing_unit_access WHERE unit_id=?", (unit["id"],)).fetchall()
            lines.append(f"• <code>{unit['id']}</code> · {esc(unit['org_name'] or '')} · {esc(unit['tier'] or '?')} · {n} 条")
            lines.append("  登录入口：" + (", ".join(esc(r[0]) for r in sources) or "历史订阅"))
            sub = self.conn.execute('SELECT * FROM org_subscriptions WHERE org_id=?', (unit['org_id'],)).fetchone()
            if sub and sub['expires_at']:
                lines.append(f"  上游到期：{store.iso(sub['expires_at'])}（自动观测）")
                if sub['started_at']:
                    lines.append(f"  上游开始：{store.iso(sub['started_at'])}（已核对本组织）")
                else:
                    lines.append("  开始时间待核实")
                lines.append("  到期延长不自动新增成本或修改已配置租期")
            aliases = self.conn.execute('SELECT upstream_account_id FROM upstream_assignments WHERE unit_id=?', (unit['id'],)).fetchall()
            lines.append("  历史上游账号：" + ', '.join(str(r[0]) for r in aliases))
            ps = billing.periods(self.conn, unit["id"])
            for p in ps:
                lines.append(f"  租期 #{p['id']}：{p['start_date']}~{p['end_date']} {money(p['cost_cny'])}")
            if not ps:
                lines.append("  未配置租期成本")
        pending = self.conn.execute('SELECT org_id,upstream_account_id,COUNT(*) FROM requests '
                                    'WHERE billing_unit_id IS NULL GROUP BY org_id,upstream_account_id').fetchall()
        for org_id, upstream_id, count in pending:
            lines.append(f"  ⚠️ 待核对：组织 {org_id} / 上游账号 {upstream_id}，{count} 条")
        if not rows:
            lines.append("尚未发现订阅账号，请先添加登录账号并 /sync。")
        lines.append("多席位换号或历史归属待核对：/unit_link 计费单元ID 上游账号ID")
        lines.append("设置成本：/period add 计费单元ID 起 止 金额")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_account_add(self, chat, msg, uid, args, thread) -> None:
        self.flows[uid] = {"cmd": "account_add", "step": "email", "data": {}}
        self.tg.send(chat["id"],
                     "添加登录账号 1/3：发 <b>邮箱</b>。\n（随时发 /cancel 取消）", thread)

    def cmd_account_del(self, chat, msg, uid, args, thread) -> None:
        if not args:
            self.tg.send(chat["id"], "用法：<code>/account_del &lt;账号id&gt;</code>", thread)
            return
        aid = args[0]
        row = store.account_row(self.conn, aid)
        if not row:
            self.tg.send(chat["id"], f"没有账号 {esc(aid)}", thread)
            return
        self.conn.execute("UPDATE rc_accounts SET enabled=0 WHERE id=?", (aid,))
        self.conn.commit()
        store.event(self.conn, "account_del", actor=f"tg:{uid}", account_id=aid)
        self.tg.send(chat["id"], f"已停用 <code>{esc(aid)}</code>（历史流水与账单保留）。", thread)

    # ---------------------------------------------------------------- 管理员：租期
    def cmd_renew(self, chat, msg, uid, args, thread):
        if len(args) != 3:
            self.tg.send(chat['id'], '用法：/renew 计费单元ID 天数 金额\n例：/renew 25693:seat1 30 850\n从上一期结束时刻接续（北京时间，结束时刻不包含）。', thread)
            return
        p = billing.renew_period(self.conn, args[0], int(args[1]), float(args[2]),
                                 message_key=f"{chat['id']}:{msg['message_id']}")
        self.tg.send(chat['id'], f"✅ 续费租期 #{p['id']}：{p['start_date']} ~ {p['end_date']} {money(p['cost_cny'])}\n结束时刻不包含；同一条消息重试不会重复续费。", thread)

    def cmd_period(self, chat, msg, uid, args, thread) -> None:
        if args and args[0] == 'extend':
            if len(args) != 3:
                raise ValueError('用法：/period extend 租期ID YYYY-MM-DDTHH:MM（补偿延期，不增加成本）')
            billing.extend_period(self.conn, int(args[1]), billing.parse_boundary(args[2]))
            self.tg.send(chat['id'], '✅ 已延长原周期，原成本不变；下次续费从延长后的结束时刻接续。', thread)
            return
        if args and args[0] == "assign":
            if len(args) != 3:
                self.tg.send(chat["id"], "用法：/period assign 旧租期ID 计费单元ID", thread)
                return
            billing.assign_legacy_period(self.conn, int(args[1]), args[2])
            self.tg.send(chat["id"], "✅ 旧成本已指定到计费账号（未复制费用）。", thread)
            return
        if not args or args[0] != "add":
            self.tg.send(chat["id"],
                         "用法：<code>/period add &lt;计费单元ID&gt; &lt;起&gt; &lt;止&gt; &lt;¥&gt; [none|even]</code>\n"
                         "例：<code>/period add 25693:seat1 2026-11-01 2026-11-30 2200</code>", thread)
            return
        if len(args) < 5:
            self.tg.send(chat["id"], "参数不够。", thread)
            return
        aid, s, e, cost = args[1], args[2], args[3], args[4]
        split = args[5] if len(args) > 5 else "none"
        try:
            if s == 'auto' and e == 'auto':
                start, end = billing.automatic_boundaries(self.conn, aid)
            else:
                start, end = billing.parse_boundary(s), billing.parse_boundary(e)
            pid = billing.add_period(self.conn, aid, start, end,
                                     float(cost), idle_split=split)
        except Exception as exc:                 # noqa: BLE001
            self.tg.send(chat["id"], f"❌ {esc(exc)}", thread)
            return
        s, e = start.isoformat(), end.isoformat()
        store.event(self.conn, "period_add", actor=f"tg:{uid}", account_id=aid,
                    detail={"id": pid, "start": s, "end": e, "cost": cost})
        self.tg.send(chat["id"], f"✅ 已记录租期 #{pid}：{esc(aid)} {s}~{e} {money(float(cost))}", thread)

    def cmd_periods(self, chat, msg, uid, args, thread) -> None:
        aid = args[0] if args else None
        rows = billing.periods(self.conn, aid)
        lines = ["<b>租期与成本（按计费账号）</b>"]
        for p in rows:
            lines.append(f"• #{p['id']} <code>{p['billing_unit_id']}</code> {p['start_date']}~{p['end_date']} "
                         f"{money(p['cost_cny'])} · idle={p['idle_split']}")
        for p in self.conn.execute("SELECT * FROM account_periods WHERE billing_unit_id IS NULL"):
            lines.append(f"⚠️ 待指定 #{p['id']} · 原登录 {esc(p['account_id'])} · {p['start_date']}~{p['end_date']} · {money(p['cost_cny'])}")
        lines.append("旧成本指定：/period assign 旧租期ID 计费单元ID")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    # ---------------------------------------------------------------- 管理员：倍率
    def cmd_rate(self, chat, msg, uid, args, thread) -> None:
        if len(args) < 2:
            self.tg.send(chat["id"], "用法：<code>/rate claude-opus-* 1.5</code>", thread)
            return
        pat, mult = args[0], args[1]
        if pat == "default":
            store.set_setting(self.conn, "default_multiplier", float(mult))
        else:
            self.conn.execute(
                "INSERT INTO model_rates(pattern, multiplier, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(pattern) DO UPDATE SET multiplier=excluded.multiplier, "
                "updated_at=excluded.updated_at",
                (pat, float(mult), store.iso()))
            self.conn.commit()
        store.event(self.conn, "rate_set", actor=f"tg:{uid}", detail={"pattern": pat, "mult": mult})
        self.tg.send(chat["id"], f"✅ {esc(pat)} → ×{mult}", thread)

    def cmd_rates(self, chat, msg, uid, args, thread) -> None:
        rates = self.conn.execute("SELECT * FROM model_rates ORDER BY pattern").fetchall()
        default = store.get_setting(self.conn, "default_multiplier", "1.0")
        lines = [f"<b>模型倍率</b>（默认 ×{default}）"]
        for r in rates:
            lines.append(f"• <code>{esc(r['pattern'])}</code> ×{r['multiplier']}")
        if not rates:
            lines.append("• 还没配置")
        rows = self.conn.execute(
            """SELECT model, COUNT(*) n, SUM(cost_usd) usd FROM requests
               WHERE cost_usd IS NOT NULL GROUP BY model ORDER BY usd DESC LIMIT 30""").fetchall()
        exact, globs, _ = billing.load_rates(self.conn)
        missing = []
        for r in rows:
            _, configured = billing.multiplier_for((exact, globs, 1.0), r["model"])
            if not configured:
                missing.append(f"• <code>{esc(r['model'])}</code> {r['n']} 条 · {usd(r['usd'] or 0)}")
        if missing:
            lines += ["", "<b>未配置倍率的模型</b>（按默认值计）"] + missing
        self.tg.send(chat["id"], "\n".join(lines), thread)

    # ---------------------------------------------------------------- 管理员：设备
    def _resolve_device(self, spec: str):
        if ":" in spec:
            aid, did = spec.split(":", 1)
            return aid, int(did)
        row = self.conn.execute("SELECT account_id, rc_device_id FROM devices WHERE rc_device_id=?",
                                (int(spec),)).fetchone()
        if not row:
            raise ValueError(f"找不到设备 {spec}")
        return row["account_id"], row["rc_device_id"]

    def cmd_revoke(self, chat, msg, uid, args, thread) -> None:
        if not args:
            self.tg.send(chat["id"], "用法：<code>/revoke &lt;账号:设备id&gt;</code>", thread)
            return
        try:
            aid, did = self._resolve_device(args[0])
        except Exception as exc:                 # noqa: BLE001
            self.tg.send(chat["id"], f"❌ {esc(exc)}", thread)
            return
        dry = bool(self.cfg.get("dry_run", True))
        try:
            plan = evictor.evict(self.pool, self.conn, aid, did, reason="admin", actor=f"tg:{uid}",
                                 dry_run=dry, force=True)
        except Exception as exc:                 # noqa: BLE001
            self.tg.send(chat["id"], f"❌ {esc(exc)}", thread)
            return
        head = "🧪 演练（未撤销）" if dry else "✅ 已撤销"
        lines = [f"{head} <code>{aid}:{did}</code>",
                 f"• 下线前采集：{'成功' if not plan['flush'].get('error') else '失败 ' + esc(plan['flush']['error'])}"]
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_bind(self, chat, msg, uid, args, thread) -> None:
        if len(args) < 2:
            self.tg.send(chat["id"], "用法：<code>/bind &lt;账号:设备id&gt; &lt;tg_user_id&gt;</code>", thread)
            return
        try:
            aid, did = self._resolve_device(args[0])
            enroll.bind_device(self.conn, aid, did, int(args[1]))
        except Exception as exc:                 # noqa: BLE001
            self.tg.send(chat["id"], f"❌ {esc(exc)}", thread)
            return
        store.event(self.conn, "bind", actor=f"tg:{uid}", account_id=aid, rc_device_id=did,
                    user_id=int(args[1]))
        self.tg.send(chat["id"], f"✅ 已把 <code>{aid}:{did}</code> 归到 tg:<code>{args[1]}</code>"
                                 f"（历史请求也一并改归）", thread)

    # ---------------------------------------------------------------- 管理员：采集/账单
    def cmd_sync(self, chat, msg, uid, args, thread) -> None:
        only = [args[0]] if args else None
        self.tg.send(chat["id"], "⏳ 采集中…", thread)
        results = syncer.sync_all(self.pool, self.conn, only=only)
        lines = ["<b>采集结果</b>"]
        for r in results:
            if r.get("error"):
                lines.append(f"• <code>{r['account_id']}</code> ❌ {esc(r['error'][:160])}")
            else:
                pr = r.get("priced_after") or {}
                lines.append(f"• <code>{r['account_id']}</code> 新 {r['inserted']} / 更新 {r['updated']}"
                             f" / 归因 {r['attributed']} / 补价 {pr.get('priced', 0)}")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_report(self, chat, msg, uid, args, thread) -> None:
        flags = {a.lower() for a in args}
        save = "save" in flags
        refresh = "refresh" in flags
        args = [a for a in args if a.lower() not in ("save", "refresh")]
        start, end = self._parse_range(args)
        if refresh:
            self.tg.send(chat["id"], "⏳ 刷新官方用量快照…", thread)
            for acc in store.enabled_accounts(self.conn):
                try:
                    with self.pool.session(acc["id"]) as s:
                        s.post("/api/app/account/usage/refresh"
                               + (f"?org_id={acc['own_org_id']}" if acc["own_org_id"] else ""))
                except Exception:                # noqa: BLE001
                    pass
        bill = billing.compute(self.conn, start, end)
        lines = [f"<b>账单</b> {start} ~ {end}", ""]
        users = sorted(bill["users"].items(), key=lambda kv: -kv[1])
        if users:
            for u, amt in users:
                handle = self.conn.execute("SELECT handle FROM users WHERE tg_user_id=?",
                                           (u,)).fetchone()
                lines.append(f"• {esc(handle['handle']) if handle and handle['handle'] else 'tg:' + str(u)}"
                             f" · <b>{money(amt)}</b>")
            lines.append(f"合计 <b>{money(sum(a for _, a in users))}</b>")
        else:
            lines.append("这段时间没有可分摊的用量。")
        lines.append("")
        for p in bill["periods"]:
            if p["user_id"] is None:
                lines.append(f"⚠️ <code>{p['account_id']}</code> {p['start']}~{p['end']} {p['note']}")
        an = bill["anomalies"]
        if an.get("legacy_periods"):
            lines.append("⚠️ 旧成本尚未指定到订阅账号，请 /periods 查看并 /period assign。")
        if an.get("unresolved_requests"):
            lines.append(f"⚠️ {an['unresolved_requests']} 条流水的上游账号归属待回补，暂未计费。")
        if an["incomplete"]:
            lines.append("\n<b>缺租期</b>")
            for x in an["incomplete"]:
                lines.append(f"• <code>{x['account_id']}</code> {x['requests']} 条请求未定价")
        if an["uncovered"]:
            lines.append("\n<b>租期外请求</b>")
            for x in an["uncovered"]:
                lines.append(f"• <code>{x['account_id']}</code> {x['requests']} 条")
        stale_items = an.get("stale") or []
        if stale_items:
            stale_hours = store.get_int(self.conn, "stale_hours", 24)
            lines.append("\n<b>未定价</b>")
            for x in stale_items:
                if "stale" in x:
                    lines.append(f"• <code>{x['account_id']}</code> {x['period']} "
                                 f"{x['requests']} 条（其中超 {stale_hours}h {x['stale']} 条）")
                else:
                    lines.append(f"• <code>{x['account_id']}</code> {x['period']} {x['requests']} 条")
        if an["unrated_models"]:
            lines.append("\n<b>未配置倍率的模型</b>")
            for m, v in an["unrated_models"].items():
                lines.append(f"• <code>{esc(m)}</code> 加权 {v['weighted']:.2f}")
        if save:
            ids = billing.create_invoices(self.conn, bill)
            lines.append(f"\n已落账单 {len(ids)} 张（id {ids[0]}…{ids[-1]}）" if ids else "\n没有可落的账单")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_health(self, chat, msg, uid, args, thread) -> None:
        lines = ["<b>健康</b>"]
        for a in store.enabled_accounts(self.conn):
            wm = self.conn.execute("SELECT * FROM watermarks WHERE account_id=?", (a["id"],)).fetchone()
            age = (store.now_ms() - (wm["last_synced_at"] or 0)) // 60000 if wm else None
            lines.append(f"• <code>{a['id']}</code> {a['status']} · 名额 "
                         f"{enroll.used_slots(self.conn, a['id'])}/{a['capacity'] or 4}"
                         f" · 采集 {str(age) + ' 分钟前' if age is not None else '从未'}")
            if a["last_error"]:
                lines.append(f"   ⚠️ {esc(a['last_error'][:140])}")
        pend = self.conn.execute(
            "SELECT COUNT(*) n FROM requests WHERE cost_usd IS NULL "
            "AND COALESCE(pricing_status,'') NOT IN ('ignored','not_billable')").fetchone()["n"]
        total = self.conn.execute("SELECT COUNT(*) n FROM requests").fetchone()["n"]
        unresolved = self.conn.execute("SELECT COUNT(*) FROM requests WHERE billing_unit_id IS NULL").fetchone()[0]
        legacy = self.conn.execute("SELECT COUNT(*) FROM account_periods WHERE billing_unit_id IS NULL").fetchone()[0]
        lines += ["", f"请求流水 {total} 条 · 未定价 {pend} 条",
                  f"订阅归属待回补 {unresolved} 条 · 旧成本待指定 {legacy} 期",
                  f"演练模式：{'开' if self.cfg.get('dry_run', True) else '关'}"]
        pending = self.conn.execute(
            "SELECT state, account_id, tg_user_id FROM login_states "
            "WHERE status IN ('approved_pending_device','needs_binding')").fetchall()
        if pending:
            lines.append("\n<b>待核对设备归属</b>（确认实际设备后使用 /bind）")
            for row in pending:
                lines.append(f"• {esc(row['account_id'])} · tg:{row['tg_user_id']}")
        self.tg.send(chat["id"], "\n".join(lines), thread)

    def cmd_dryrun(self, chat, msg, uid, args, thread) -> None:
        if not args or args[0] not in ("on", "off"):
            self.tg.send(chat["id"], "用法：<code>/dryrun on|off</code>", thread)
            return
        self.cfg["dry_run"] = args[0] == "on"
        from .runtime import save_config
        save_config(self.cfg)
        store.event(self.conn, "dryrun", actor=f"tg:{uid}", detail={"value": self.cfg["dry_run"]})
        self.tg.send(chat["id"], f"演练模式 {'开启' if self.cfg['dry_run'] else '关闭（真实撤销/批准）'}",
                     thread)

    # ---------------------------------------------------------------- 多步流程
    def flow_step(self, chat, msg, uid, text) -> bool:
        flow = self.flows.get(uid)
        if not flow:
            return False
        if text.strip() in ("/cancel", "取消"):
            self.flows.pop(uid, None)
            self.tg.send(chat["id"], "已取消。")
            return True
        data = flow["data"]
        step = flow["step"]

        if flow["cmd"] == "account_add":
            prompts = {
                "email": ("password", "2/3：发 <b>密码</b>（这条消息我会立即删除）。"),
                "password": ("label", "3/3：给账号起个 <b>短标签</b>，如 <code>ultra-a</code>。"),
            }
            if step == "password":
                data["password"] = text
                self.tg.delete(chat["id"], msg["message_id"])   # 立刻删掉密码消息
            else:
                data[step] = text.strip()
            nxt = prompts.get(step)
            if nxt:
                flow["step"] = nxt[0]
                self.tg.send(chat["id"], nxt[1])
                return True
            if step == "label":
                return self._finish_account_add(chat, uid, data)
        return False

    def _finish_account_add(self, chat, uid, data) -> bool:
        from .crypto import encrypt
        self.flows.pop(uid, None)
        aid = data["label"].strip()
        if store.account_row(self.conn, aid):
            self.tg.send(chat["id"], f"❌ 账号 <code>{esc(aid)}</code> 已存在。")
            return True
        try:
            self.conn.execute(
                """INSERT INTO rc_accounts(id, label, email, password_enc, status, created_at)
                   VALUES (?,?,?,?, 'ok', ?)""",
                (aid, aid, data["email"].strip(), encrypt(data["password"]), store.iso()))
            self.conn.commit()
        except Exception as exc:                 # noqa: BLE001
            # 每线程独立连接，只回滚当前命令；账号行已 commit，另行清理半成品。
            self.conn.rollback()
            periods = self.conn.execute(
                "SELECT COUNT(*) n FROM account_periods WHERE account_id=?", (aid,)).fetchone()["n"]
            reqs = self.conn.execute(
                "SELECT COUNT(*) n FROM requests WHERE account_id=?", (aid,)).fetchone()["n"]
            if not periods and not reqs:
                self.conn.execute("DELETE FROM rc_accounts WHERE id=?", (aid,))
                self.conn.commit()
            self.tg.send(chat["id"],
                         f"❌ 添加账号失败：<code>{esc(str(exc)[:200])}</code>，已回滚。"
                         "重发 /account_add。")
            return True
        store.event(self.conn, "account_add", actor=f"tg:{uid}", account_id=aid,
                    detail={"email": data["email"]})
        self.tg.send(chat["id"], f"✅ 账号 <code>{esc(aid)}</code> 已加入，正在验证登录…")
        try:
            info = self.pool.login_now(aid)
            self.tg.send(chat["id"],
                         f"✅ 登录成功 · 组织 <code>{info['own_org_id']}</code> · "
                         f"名额 {info['capacity']} · {esc(info['email'])}\n使用 /units 查看计费账号，再用 /period add 配置成本。")
        except Exception as exc:                 # noqa: BLE001
            self.tg.send(chat["id"], f"⚠️ 已保存，但登录失败：<code>{esc(str(exc)[:200])}</code>\n"
                                     "检查密码/是否开了 MFA，修好后可让管理员重新添加或手工修库。")
        return True

    # ---------------------------------------------------------------- 后台采集
    def sync_loop(self) -> None:
        try:
            self._sync_loop()
        finally:
            self.conn.close()  # closes only this worker's connection

    def _sync_loop(self) -> None:
        while not self._stop.is_set():
            interval = store.get_int(self.conn, "sync_interval_seconds", 300)
            try:
                results = syncer.sync_all(self.pool, self.conn)
                self._handle_sync_results(results)
            except Exception as exc:             # noqa: BLE001
                self.conn.rollback()
                self._notify_admins(f"⚠️ 采集循环异常：<code>{esc(str(exc)[:200])}</code>")
            self._stop.wait(interval)

    def _handle_sync_results(self, results) -> None:
        """按账号跟踪连续失败：达到阈值只告警一次，恢复时再发一条。"""
        threshold = max(1, store.get_int(self.conn, "alert_after_failures", 3))
        for r in results:
            aid = r.get("account_id")
            if not aid:
                continue
            if r.get("error"):
                count = self._fail_counts.get(aid, 0) + 1
                self._fail_counts[aid] = count
                if count == threshold:
                    self._notify_admins(f"⚠️ 采集连续失败 {count} 次 <code>{esc(aid)}</code>\n"
                                        f"<code>{esc(str(r['error'])[:200])}</code>")
            elif aid in self._fail_counts:
                count = self._fail_counts.pop(aid)
                if count >= threshold:
                    self._notify_admins(f"✅ 采集已恢复 <code>{esc(aid)}</code>"
                                        f"（此前连续失败 {count} 次）")

    def _notify_admins(self, text: str) -> None:
        for aid in self.admins:
            self.tg.send(aid, text)

    def _parse_range(self, args) -> tuple[date, date]:
        dates = []
        for a in args[:2]:
            try:
                dates.append(billing.parse_date(a))
            except Exception:                    # noqa: BLE001
                pass
        if len(dates) == 2:
            return dates[0], dates[1]
        if len(dates) == 1:
            return billing.month_bounds(dates[0])
        return billing.month_bounds(date.today())


def main() -> None:
    rt = Runtime()
    if not rt.cfg.get("bot_token"):
        raise SystemExit("缺少 bot_token：写到 /opt/data/state/reclaude-billing/config.json "
                         '（{"bot_token": "...", "admin_ids": [123], "allowed_chat_ids": [123]}）')
    bot = Bot(rt)
    print("bot started")
    # Stop polling first, then drain accepted commands during service shutdown.
    signal.signal(signal.SIGTERM, lambda *_: bot._stop.set())
    signal.signal(signal.SIGINT, lambda *_: bot._stop.set())
    try:
        bot.run()
    finally:
        bot.stop()
        rt.close()


if __name__ == "__main__":
    main()
