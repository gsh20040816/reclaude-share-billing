"""计费：模型倍率 → 加权用量 → 按账号租期成本分摊 → 账单。"""
from __future__ import annotations

import calendar
import fnmatch
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


# ------------------------------------------------------------------ 日期工具
def parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


def day_end_ms(d: date) -> int:
    return day_start_ms(d) + 86_400_000 - 1


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def month_bounds(d: date) -> tuple[date, date]:
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last)


# ------------------------------------------------------------------ 倍率
def load_rates(conn):
    rows = conn.execute("SELECT pattern, multiplier FROM model_rates").fetchall()
    exact, globs = {}, []
    for r in rows:
        pat = r["pattern"]
        if any(ch in pat for ch in "*?["):
            globs.append((pat, float(r["multiplier"])))
        else:
            exact[pat] = float(r["multiplier"])
    default = conn.execute("SELECT value FROM settings WHERE key='default_multiplier'").fetchone()
    return exact, globs, float(default["value"]) if default else 1.0


def multiplier_for(rates, model: str | None) -> tuple[float, bool]:
    """-> (倍率, 是否已配置)。精确 → 通配 → 默认。"""
    exact, globs, default = rates
    if model and model in exact:
        return exact[model], True
    for pat, mult in globs:
        if model and fnmatch.fnmatch(model, pat):
            return mult, True
    return default, False


# ------------------------------------------------------------------ 用量
def weighted_usage(conn, account_id: str, start_ms: int, end_ms: int) -> dict:
    """区间内按用户聚合加权用量。"""
    rates = load_rates(conn)
    rows = conn.execute(
        """SELECT user_id, model, cost_usd, COUNT(*) n FROM requests
           WHERE billing_unit_id=? AND created_at>=? AND created_at<=?
             AND cost_usd IS NOT NULL
             AND COALESCE(pricing_status,'') NOT IN ('ignored','not_billable')
           GROUP BY user_id, model, ROUND(cost_usd, 8)""",
        (account_id, start_ms, end_ms)).fetchall()
    per_user: dict = {}
    unrated: dict = {}
    unassigned_w = 0.0
    unassigned_n = 0
    for r in rows:
        mult, configured = multiplier_for(rates, r["model"])
        w = float(r["cost_usd"]) * r["n"] * mult
        if not configured:
            unrated[r["model"] or "?"] = unrated.get(r["model"] or "?", 0.0) + w
        uid = r["user_id"]
        if uid is None:
            unassigned_w += w
            unassigned_n += r["n"]
            continue
        slot = per_user.setdefault(uid, {"weighted": 0.0, "raw": 0.0, "requests": 0})
        slot["weighted"] += w
        slot["raw"] += float(r["cost_usd"]) * r["n"]
        slot["requests"] += r["n"]
    return {"per_user": per_user, "unassigned_weighted": unassigned_w,
            "unassigned_requests": unassigned_n, "unrated_models": unrated, "rates": rates}


def pending_stats(conn, account_id: str, start_ms: int, end_ms: int) -> dict:
    """未定价统计。ignored / not_billable 是官方明确不计费的，不能算作 pending。"""
    stale_hours = 24.0
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='stale_hours'").fetchone()
        if row:
            stale_hours = float(row["value"])
    except (TypeError, ValueError):
        pass
    stale_before = int(time.time() * 1000) - int(stale_hours * 3_600_000)
    row = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok,
                  COALESCE(SUM(CASE WHEN created_at<? THEN 1 ELSE 0 END),0) stale
           FROM requests
           WHERE billing_unit_id=? AND created_at>=? AND created_at<=?
             AND cost_usd IS NULL
             AND COALESCE(pricing_status,'pending')
                 NOT IN ('ignored','not_billable','priced')""",
        (stale_before, account_id, start_ms, end_ms)).fetchone()
    return {"requests": row["n"], "tokens": row["tok"], "stale": row["stale"]}


# ------------------------------------------------------------------ 租期
def periods(conn, account_id: str | None = None):
    if account_id:
        return conn.execute("SELECT * FROM account_periods WHERE billing_unit_id=? ORDER BY start_date",
                            (account_id,)).fetchall()
    return conn.execute("SELECT * FROM account_periods WHERE billing_unit_id IS NOT NULL ORDER BY billing_unit_id, start_date").fetchall()


BILLING_TZ = ZoneInfo("Asia/Shanghai")


def parse_boundary(value):
    if len(value) == 10:
        return parse_date(value)
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=BILLING_TZ) if result.tzinfo is None else result.astimezone(BILLING_TZ)


def boundary_ms(value, *, end=False):
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=BILLING_TZ) if value.tzinfo is None else value
        return int(dt.timestamp() * 1000)
    # Legacy date-only ends include the entire final day.
    return day_start_ms(value) + (86400000 if end else 0)


def period_bounds(p):
    return (boundary_ms(parse_boundary(p['start_date'])),
            boundary_ms(parse_boundary(p['end_date']), end=True))


def period_overlaps(p, start, end):
    lo, hi = period_bounds(p)
    return lo < boundary_ms(end, end=True) and hi > boundary_ms(start)


def add_period(conn, account_id: str, start: date, end: date, cost_cny: float,
               idle_split: str = "none", note: str | None = None, *, _commit=True) -> int:
    if not conn.execute("SELECT 1 FROM billing_units WHERE id=?", (account_id,)).fetchone():
        raise ValueError("请使用已发现的 计费单元ID（/units 查看），不能使用登录标签")
    if not math.isfinite(cost_cny) or cost_cny < 0:
        raise ValueError("成本必须为非负有限数值")
    if idle_split not in ("none", "even"):
        raise ValueError("idle_split 必须是 none 或 even")
    if boundary_ms(end, end=True) <= boundary_ms(start):
        raise ValueError("结束时间必须晚于开始时间")
    # Serialize overlap validation and insertion across SQLite connections.
    conn.execute("UPDATE settings SET value=value WHERE key='subscription_ledger_v1'")
    for p in periods(conn, account_id):
        if period_overlaps(p, start, end):
            raise ValueError(f"与已有租期重叠：{p['start_date']} ~ {p['end_date']}")
    cur = conn.execute(
        """INSERT INTO account_periods(billing_unit_id, account_id, start_date, end_date, cost_cny, idle_split,
                                       note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (account_id, account_id, start.isoformat(), end.isoformat(), float(cost_cny), idle_split,
         note, time.strftime("%Y-%m-%dT%H:%M:%S")))
    if _commit:
        conn.commit()
    return cur.lastrowid


def renew_period(conn, unit_id, days, cost_cny, *, message_key):
    if not isinstance(days, int) or not 1 <= days <= 3660:
        raise ValueError('续费天数必须是 1~3660 的整数')
    try:
        conn.execute("UPDATE settings SET value=value WHERE key='subscription_ledger_v1'")
        receipt = conn.execute('SELECT period_id FROM renewal_receipts WHERE message_key=?', (message_key,)).fetchone()
        if receipt:
            row = conn.execute('SELECT * FROM account_periods WHERE id=?', (receipt[0],)).fetchone()
            conn.commit()
            return row
        previous = periods(conn, unit_id)
        if not previous:
            raise ValueError('尚未配置首期成本；请先 /period add 计费单元ID auto auto 金额，或填写首期时间')
        latest = max(previous, key=lambda p: period_bounds(p)[1])
        start_ms = period_bounds(latest)[1]
        start = datetime.fromtimestamp(start_ms / 1000, BILLING_TZ)
        end = start + timedelta(days=days)
        pid = add_period(conn, unit_id, start, end, cost_cny,
                         idle_split=latest['idle_split'], note='管理员确认续费', _commit=False)
        conn.execute('INSERT INTO renewal_receipts VALUES (?,?)', (message_key, pid))
        conn.commit()
        return conn.execute('SELECT * FROM account_periods WHERE id=?', (pid,)).fetchone()
    except Exception:
        conn.rollback()
        raise


def extend_period(conn, period_id, end):
    try:
        conn.execute("UPDATE settings SET value=value WHERE key='subscription_ledger_v1'")
        p = conn.execute('SELECT * FROM account_periods WHERE id=?', (period_id,)).fetchone()
        if not p or not p['billing_unit_id']:
            raise ValueError('未找到已归属的租期')
        if boundary_ms(end, end=True) <= period_bounds(p)[1]:
            raise ValueError('补偿延期必须晚于原结束时间')
        start = parse_boundary(p['start_date'])
        for other in periods(conn, p['billing_unit_id']):
            if other['id'] != period_id and period_overlaps(other, start, end):
                raise ValueError('延期与已有租期重叠，请先核对续费周期')
        conn.execute('UPDATE account_periods SET end_date=? WHERE id=?', (end.isoformat(), period_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def automatic_boundaries(conn, unit_id):
    row = conn.execute('SELECT s.* FROM org_subscriptions s JOIN billing_units u ON u.org_id=s.org_id '
                       'WHERE u.id=?', (unit_id,)).fetchone()
    if not row or not row['started_at'] or not row['expires_at']:
        raise ValueError('未能确认该组织首期开始时间，请填写 YYYY-MM-DDTHH:MM；到期时间可在 /units 查看')
    return tuple(datetime.fromtimestamp(row[k] / 1000, BILLING_TZ) for k in ('started_at', 'expires_at'))


# ------------------------------------------------------------------ 出账
def _split_cost(total_cny: float, shares: dict[int, float]) -> dict[int, float]:
    """分到分，最大份额吃掉舍入余数。shares: user -> 权重。"""
    if not shares:
        return {}
    total_w = sum(shares.values())
    if total_w <= 0:
        return {}
    cents_total = round(total_cny * 100)
    cents = {u: round(total_cny * 100 * (w / total_w)) for u, w in shares.items()}
    diff = cents_total - sum(cents.values())
    if diff:
        biggest = max(shares, key=lambda u: shares[u])
        cents[biggest] += diff
    return {u: c / 100 for u, c in cents.items()}


def compute(conn, start: date | None = None, end: date | None = None) -> dict:
    """按「账号 × 租期」出账。区间只用于筛租期；分摊始终按整期的 Σw 计算。"""
    start = start or month_bounds(today())[0]
    end = end or month_bounds(today())[1]
    accounts = {r["id"]: r for r in conn.execute("SELECT * FROM billing_units").fetchall()}
    result = {"start": start.isoformat(), "end": end.isoformat(), "periods": [],
              "users": {}, "anomalies": {"incomplete": [], "uncovered": [], "stale": [],
                                         "unrated_models": {}, "unassigned": []}}
    result["anomalies"]["legacy_periods"] = [dict(r) for r in conn.execute(
        "SELECT * FROM account_periods WHERE billing_unit_id IS NULL ORDER BY id")]
    # Reports select whole rental periods, even if the requested range is only
    # one day. Never silently exclude unresolved historical rows from a snapshot.
    result["anomalies"]["unresolved_requests"] = conn.execute(
        "SELECT COUNT(*) FROM requests WHERE billing_unit_id IS NULL").fetchone()[0]
    covered_ranges: dict[str, list[tuple[int, int]]] = {}
    for aid in accounts:
        for p in periods(conn, aid):
            if not period_overlaps(p, start, end):
                continue
            s_ms, exclusive_end = period_bounds(p)
            e_ms = exclusive_end - 1
            covered_ranges.setdefault(aid, []).append((s_ms, e_ms))
            usage = weighted_usage(conn, aid, s_ms, e_ms)
            for m, w in usage["unrated_models"].items():
                slot = result["anomalies"]["unrated_models"].setdefault(m, {"weighted": 0.0, "accounts": []})
                slot["weighted"] += w
                slot["accounts"].append(aid)
            total_w = sum(v["weighted"] for v in usage["per_user"].values())
            unassigned_w = usage["unassigned_weighted"]
            # 分摊分母必须包含未归属用量，否则未归属那份会被转嫁给已归属用户
            denom = total_w + unassigned_w
            cost = float(p["cost_cny"])
            status = "provisional" if exclusive_end > int(time.time() * 1000) else "final"
            # 未定价统计先做：整期只有 pending 时 denom=0，若放在后面的分摊分支里会被跳过
            pend = pending_stats(conn, aid, s_ms, e_ms)
            if pend["requests"]:
                result["anomalies"]["stale"].append({
                    "account_id": aid, "period": f"{p['start_date']}~{p['end_date']}",
                    "requests": pend["requests"], "tokens": pend["tokens"],
                    "stale": pend["stale"]})
            note = []
            if denom <= 0:
                if p["idle_split"] == "even":
                    seat_users = [r["user_id"] for r in conn.execute(
                        "SELECT DISTINCT user_id FROM requests WHERE billing_unit_id=? AND user_id IS NOT NULL "
                        "AND created_at>=? AND created_at<=?",
                        (aid, s_ms, e_ms)).fetchall()]
                    if seat_users:
                        shares = {u: 1.0 for u in seat_users}
                        amounts = _split_cost(cost, shares)
                        for u, amt in amounts.items():
                            result["periods"].append({
                                "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                                "end": p["end_date"], "cost_cny": cost, "total_weighted": 0.0,
                                "unit_price": 0.0, "user_id": u, "weighted": 0.0, "raw_usd": 0.0,
                                "requests": 0, "amount_cny": amt, "status": status,
                                "note": "idle_split=even（按本订阅本期已归属请求的用户均摊）"})
                            result["users"][u] = result["users"].get(u, 0.0) + amt
                        continue
                note.append("整期零用量，未分摊")
                if pend["requests"]:
                    note.append(f"另有 {pend['requests']} 条未定价")
                result["periods"].append({
                    "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                    "end": p["end_date"], "cost_cny": cost, "total_weighted": 0.0,
                    "unit_price": 0.0, "user_id": None, "weighted": 0.0, "raw_usd": 0.0,
                    "requests": 0, "amount_cny": 0.0, "status": status,
                    "note": " / ".join(note)})
                continue
            unit = cost / denom
            shares = {u: v["weighted"] for u, v in usage["per_user"].items()}
            if unassigned_w > 0:
                shares[None] = unassigned_w          # 未归属份额：保留在待认领，不转嫁
            amounts = _split_cost(cost, shares)
            unassigned_amount = amounts.pop(None, 0.0)
            for uid, v in sorted(usage["per_user"].items(), key=lambda kv: -kv[1]["weighted"]):
                result["periods"].append({
                    "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                    "end": p["end_date"], "cost_cny": cost, "total_weighted": denom,
                    "unit_price": unit, "user_id": uid, "weighted": v["weighted"],
                    "raw_usd": v["raw"], "requests": v["requests"],
                    "amount_cny": amounts.get(uid, 0.0), "status": status,
                    "note": ""})
                result["users"][uid] = result["users"].get(uid, 0.0) + amounts.get(uid, 0.0)
            if unassigned_w > 0:
                result["anomalies"]["unassigned"].append({
                    "account_id": aid, "period_id": p["id"],
                    "weighted": unassigned_w,
                    "requests": usage["unassigned_requests"],
                    "amount_cny": unassigned_amount})
                result["periods"].append({
                    "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                    "end": p["end_date"], "cost_cny": cost, "total_weighted": denom,
                    "unit_price": unit, "user_id": None, "weighted": unassigned_w,
                    "raw_usd": 0.0, "requests": usage["unassigned_requests"],
                    "amount_cny": unassigned_amount, "status": status,
                    "note": "未归属用量（待认领，未计入个人账单）"})

    # 没配租期的账号却有请求 → incomplete
    for aid in accounts:
        ranges = covered_ranges.get(aid, [])
        row = conn.execute("SELECT COUNT(*) n FROM requests WHERE billing_unit_id=?", (aid,)).fetchone()
        if row["n"] and not periods(conn, aid):
            result["anomalies"]["incomplete"].append(
                {"account_id": aid, "requests": row["n"], "reason": "账号没有配置租期"})

    # 落在所有租期之外、但在报告区间内的请求 → uncovered
    s_ms, e_ms = day_start_ms(start), day_end_ms(end)
    for aid in accounts:
        ranges = covered_ranges.get(aid, [])
        rows = conn.execute("SELECT created_at FROM requests WHERE billing_unit_id=? AND created_at>=? AND created_at<=?",
                            (aid, s_ms, e_ms)).fetchall()
        outside = [r["created_at"] for r in rows
                   if not any(lo <= r["created_at"] <= hi for lo, hi in ranges)]
        if outside:
            result["anomalies"]["uncovered"].append(
                {"account_id": aid, "requests": len(outside),
                 "reason": "请求时间不在任何已配置租期内"})
    for uid, amt in result["users"].items():
        result["users"][uid] = round(amt, 2)
    for line in result["periods"]:
        line["billing_unit_id"] = line["account_id"]
    return result


def assign_legacy_period(conn, period_id: int, unit_id: str):
    row = conn.execute("SELECT * FROM account_periods WHERE id=? AND billing_unit_id IS NULL", (period_id,)).fetchone()
    if row is None:
        raise ValueError("未找到待迁移的旧租期")
    if not conn.execute("SELECT 1 FROM billing_units WHERE id=?", (unit_id,)).fetchone():
        raise ValueError("未知计费账号，使用 /units 查看")
    for p in periods(conn, unit_id):
        if period_overlaps(p, parse_boundary(row["start_date"]), parse_boundary(row["end_date"])):
            raise ValueError("目标计费账号已有重叠租期")
    conn.execute("UPDATE account_periods SET account_id=?,billing_unit_id=? WHERE id=?",
                 (unit_id, unit_id, period_id))
    conn.commit()


def create_invoices(conn, bill: dict, note: str | None = None) -> list[int]:
    """把 compute() 结果落成 invoices / invoice_lines（快照）。"""
    if bill.get("anomalies", {}).get("unresolved_requests"):
        raise ValueError("仍有流水缺少上游订阅归属，请完成历史回补后再保存账单")
    if bill.get("anomalies", {}).get("legacy_periods"):
        raise ValueError("旧成本尚未指定计费账号，请先 /period assign")
    ids = []
    by_user: dict[int, list[dict]] = {}
    for line in bill["periods"]:
        if line["user_id"] is None:
            continue
        by_user.setdefault(line["user_id"], []).append(line)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    # 同一区间重复出账时覆盖旧快照，避免 /report save 点两次就出两套账单
    old = conn.execute("SELECT id FROM invoices WHERE period_start=? AND period_end=? AND status!='legacy'",
                       (bill["start"], bill["end"])).fetchall()
    for r in old:
        conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (r["id"],))
        conn.execute("DELETE FROM invoices WHERE id=?", (r["id"],))
    for uid, lines in by_user.items():
        total = round(sum(l["amount_cny"] for l in lines), 2)
        cur = conn.execute(
            """INSERT INTO invoices(user_id, period_start, period_end, total_cny, status, created_at, note)
               VALUES (?,?,?,?,?,?,?)""",
            (uid, bill["start"], bill["end"], total,
             "provisional" if any(l["status"] == "provisional" for l in lines) else "final",
             stamp, note))
        inv_id = cur.lastrowid
        ids.append(inv_id)
        for l in lines:
            conn.execute(
                """INSERT INTO invoice_lines(invoice_id, billing_unit_id, account_id, weighted_usd, raw_usd, requests,
                                             unit_price_cny, amount_cny, note)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (inv_id, l["billing_unit_id"], l["account_id"], l["weighted"], l["raw_usd"], l["requests"],
                 l["unit_price"], l["amount_cny"], l["note"]))
    conn.commit()
    return ids
