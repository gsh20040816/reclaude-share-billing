"""计费：模型倍率 → 加权用量 → 按账号租期成本分摊 → 账单。"""
from __future__ import annotations

import calendar
import fnmatch
import time
from datetime import date, datetime, timedelta


# ------------------------------------------------------------------ 日期工具
def parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def day_start_ms(d: date) -> int:
    return int(time.mktime(datetime(d.year, d.month, d.day, 0, 0, 0).timetuple()) * 1000)


def day_end_ms(d: date) -> int:
    return day_start_ms(d) + 86_400_000 - 1


def today() -> date:
    return date.today()


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
           WHERE account_id=? AND created_at>=? AND created_at<=?
             AND cost_usd IS NOT NULL AND COALESCE(pricing_status,'')!='ignored'
           GROUP BY user_id, model, ROUND(cost_usd, 8)""",
        (account_id, start_ms, end_ms)).fetchall()
    per_user: dict = {}
    unrated: dict = {}
    unassigned_w = 0.0
    for r in rows:
        mult, configured = multiplier_for(rates, r["model"])
        w = float(r["cost_usd"]) * r["n"] * mult
        if not configured:
            unrated[r["model"] or "?"] = unrated.get(r["model"] or "?", 0.0) + w
        uid = r["user_id"]
        if uid is None:
            unassigned_w += w
            continue
        slot = per_user.setdefault(uid, {"weighted": 0.0, "raw": 0.0, "requests": 0})
        slot["weighted"] += w
        slot["raw"] += float(r["cost_usd"]) * r["n"]
        slot["requests"] += r["n"]
    return {"per_user": per_user, "unassigned_weighted": unassigned_w,
            "unrated_models": unrated, "rates": rates}


def pending_stats(conn, account_id: str, start_ms: int, end_ms: int) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) tok FROM requests
           WHERE account_id=? AND created_at>=? AND created_at<=?
             AND (cost_usd IS NULL OR COALESCE(pricing_status,'')!='priced')""",
        (account_id, start_ms, end_ms)).fetchone()
    return {"requests": row["n"], "tokens": row["tok"]}


# ------------------------------------------------------------------ 租期
def periods(conn, account_id: str | None = None):
    if account_id:
        return conn.execute("SELECT * FROM account_periods WHERE account_id=? ORDER BY start_date",
                            (account_id,)).fetchall()
    return conn.execute("SELECT * FROM account_periods ORDER BY account_id, start_date").fetchall()


def period_overlaps(p, start: date, end: date) -> bool:
    return not (parse_date(p["end_date"]) < start or parse_date(p["start_date"]) > end)


def add_period(conn, account_id: str, start: date, end: date, cost_cny: float,
               idle_split: str = "none", note: str | None = None) -> int:
    if end < start:
        raise ValueError("结束日期早于开始日期")
    for p in periods(conn, account_id):
        if period_overlaps(p, start, end):
            raise ValueError(f"与已有租期重叠：{p['start_date']} ~ {p['end_date']}")
    cur = conn.execute(
        """INSERT INTO account_periods(account_id, start_date, end_date, cost_cny, idle_split,
                                       note, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (account_id, start.isoformat(), end.isoformat(), float(cost_cny), idle_split,
         note, time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ 出账
def _split_cost(total_cny: float, shares: dict[int, float]) -> dict[int, float]:
    """分到分，最大份额吃掉舍入余数。shares: user -> 权重。"""
    if not shares:
        return {}
    total_w = sum(shares.values())
    if total_w <= 0:
        return {}
    cents_total = round(total_cny * 100)
    cents = {u: int(round(total_cny * 100 * (w / total_w))) for u, w in shares.items()}
    diff = cents_total - sum(cents.values())
    if diff:
        biggest = max(shares, key=lambda u: shares[u])
        cents[biggest] += diff
    return {u: c / 100 for u, c in cents.items()}


def compute(conn, start: date | None = None, end: date | None = None) -> dict:
    """按「账号 × 租期」出账。区间只用于筛租期；分摊始终按整期的 Σw 计算。"""
    start = start or month_bounds(today())[0]
    end = end or month_bounds(today())[1]
    accounts = {r["id"]: r for r in conn.execute("SELECT * FROM rc_accounts").fetchall()}
    result = {"start": start.isoformat(), "end": end.isoformat(), "periods": [],
              "users": {}, "anomalies": {"incomplete": [], "uncovered": [], "stale": [],
                                         "unrated_models": {}, "unassigned": []}}
    covered_ranges: dict[str, list[tuple[int, int]]] = {}
    for aid, acc in accounts.items():
        for p in periods(conn, aid):
            if not period_overlaps(p, start, end):
                continue
            p_start, p_end = parse_date(p["start_date"]), parse_date(p["end_date"])
            s_ms, e_ms = day_start_ms(p_start), day_end_ms(p_end)
            covered_ranges.setdefault(aid, []).append((s_ms, e_ms))
            usage = weighted_usage(conn, aid, s_ms, e_ms)
            for m, w in usage["unrated_models"].items():
                slot = result["anomalies"]["unrated_models"].setdefault(m, {"weighted": 0.0, "accounts": []})
                slot["weighted"] += w
                slot["accounts"].append(aid)
            total_w = sum(v["weighted"] for v in usage["per_user"].values())
            cost = float(p["cost_cny"])
            status = "provisional" if p_end >= today() else "final"
            note = []
            if total_w <= 0:
                if p["idle_split"] == "even":
                    seat_users = [r["user_id"] for r in conn.execute(
                        "SELECT DISTINCT user_id FROM devices WHERE account_id=? AND user_id IS NOT NULL",
                        (aid,)).fetchall()]
                    if seat_users:
                        shares = {u: 1.0 for u in seat_users}
                        amounts = _split_cost(cost, shares)
                        for u, amt in amounts.items():
                            result["periods"].append({
                                "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                                "end": p["end_date"], "cost_cny": cost, "total_weighted": 0.0,
                                "unit_price": 0.0, "user_id": u, "weighted": 0.0, "raw_usd": 0.0,
                                "requests": 0, "amount_cny": amt, "status": status,
                                "note": "idle_split=even（整期零用量，按席位均摊）"})
                            result["users"][u] = result["users"].get(u, 0.0) + amt
                        continue
                note.append("整期零用量，未分摊")
                result["periods"].append({
                    "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                    "end": p["end_date"], "cost_cny": cost, "total_weighted": 0.0,
                    "unit_price": 0.0, "user_id": None, "weighted": 0.0, "raw_usd": 0.0,
                    "requests": 0, "amount_cny": 0.0, "status": status,
                    "note": " / ".join(note)})
                continue
            unit = cost / total_w
            amounts = _split_cost(cost, {u: v["weighted"] for u, v in usage["per_user"].items()})
            for uid, v in sorted(usage["per_user"].items(), key=lambda kv: -kv[1]["weighted"]):
                result["periods"].append({
                    "account_id": aid, "period_id": p["id"], "start": p["start_date"],
                    "end": p["end_date"], "cost_cny": cost, "total_weighted": total_w,
                    "unit_price": unit, "user_id": uid, "weighted": v["weighted"],
                    "raw_usd": v["raw"], "requests": v["requests"],
                    "amount_cny": amounts.get(uid, 0.0), "status": status,
                    "note": ""})
                result["users"][uid] = result["users"].get(uid, 0.0) + amounts.get(uid, 0.0)
            if usage["unassigned_weighted"] > 0:
                result["anomalies"]["unassigned"].append({
                    "account_id": aid, "period_id": p["id"],
                    "weighted": usage["unassigned_weighted"],
                    "amount_cny": usage["unassigned_weighted"] * unit})
            pend = pending_stats(conn, aid, s_ms, e_ms)
            if pend["requests"]:
                result["anomalies"]["stale"].append({
                    "account_id": aid, "period": f"{p['start_date']}~{p['end_date']}",
                    "requests": pend["requests"], "tokens": pend["tokens"]})

    # 没配租期的账号却有请求 → incomplete
    for aid, acc in accounts.items():
        ranges = covered_ranges.get(aid, [])
        row = conn.execute("SELECT COUNT(*) n FROM requests WHERE account_id=?", (aid,)).fetchone()
        if row["n"] and not periods(conn, aid):
            result["anomalies"]["incomplete"].append(
                {"account_id": aid, "requests": row["n"], "reason": "账号没有配置租期"})

    # 落在所有租期之外、但在报告区间内的请求 → uncovered
    s_ms, e_ms = day_start_ms(start), day_end_ms(end)
    for aid in accounts:
        ranges = covered_ranges.get(aid, [])
        rows = conn.execute("SELECT created_at FROM requests WHERE account_id=? AND created_at>=? AND created_at<=?",
                            (aid, s_ms, e_ms)).fetchall()
        outside = [r["created_at"] for r in rows
                   if not any(lo <= r["created_at"] <= hi for lo, hi in ranges)]
        if outside:
            result["anomalies"]["uncovered"].append(
                {"account_id": aid, "requests": len(outside),
                 "reason": "请求时间不在任何已配置租期内"})
    for uid, amt in result["users"].items():
        result["users"][uid] = round(amt, 2)
    return result


def create_invoices(conn, bill: dict, note: str | None = None) -> list[int]:
    """把 compute() 结果落成 invoices / invoice_lines（快照）。"""
    ids = []
    by_user: dict[int, list[dict]] = {}
    for line in bill["periods"]:
        if line["user_id"] is None:
            continue
        by_user.setdefault(line["user_id"], []).append(line)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
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
                """INSERT INTO invoice_lines(invoice_id, account_id, weighted_usd, raw_usd, requests,
                                             unit_price_cny, amount_cny, note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (inv_id, l["account_id"], l["weighted"], l["raw_usd"], l["requests"],
                 l["unit_price"], l["amount_cny"], l["note"]))
    conn.commit()
    return ids
