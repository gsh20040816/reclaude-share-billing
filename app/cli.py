"""命令行运维入口（与 bot 共用同一套 core）。"""
from __future__ import annotations

import argparse
import json
import sys

from . import billing, enroll, evictor, store, syncer
from .crypto import encrypt
from .runtime import Runtime


def _rt(args) -> Runtime:
    return Runtime(db_path=getattr(args, "db", None))


def cmd_init(args) -> None:
    rt = _rt(args)
    print(f"DB ready: {store.DB_PATH if not args.db else args.db}")
    rt.close()


def cmd_account_add(args) -> None:
    rt = _rt(args)
    if store.account_row(rt.conn, args.id):
        sys.exit(f"账号 {args.id} 已存在")
    rt.conn.execute(
        """INSERT INTO rc_accounts(id, label, email, password_enc, status, created_at)
           VALUES (?,?,?,?, 'ok', ?)""",
        (args.id, args.id, args.email, encrypt(args.password) if args.password else "",
         store.iso()))
    rt.conn.commit()
    if args.start and args.end and args.cost:
        billing.add_period(rt.conn, args.id, billing.parse_date(args.start),
                           billing.parse_date(args.end), float(args.cost))
    try:
        info = rt.pool.login_now(args.id)
        print(json.dumps(info, ensure_ascii=False, indent=2))
    except Exception as exc:                     # noqa: BLE001
        print(f"登录失败：{exc}")
    rt.close()


def cmd_account_del(args) -> None:
    rt = _rt(args)
    rt.conn.execute("UPDATE rc_accounts SET enabled=0 WHERE id=?", (args.id,))
    rt.conn.commit()
    print(f"已停用 {args.id}")
    rt.close()


def cmd_period_add(args) -> None:
    rt = _rt(args)
    pid = billing.add_period(rt.conn, args.account, billing.parse_date(args.start),
                             billing.parse_date(args.end), float(args.cost),
                             idle_split=args.idle_split)
    print(f"period #{pid} 已记录")
    rt.close()


def cmd_rate(args) -> None:
    rt = _rt(args)
    if args.pattern == "default":
        store.set_setting(rt.conn, "default_multiplier", args.multiplier)
    else:
        rt.conn.execute(
            "INSERT INTO model_rates(pattern, multiplier, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(pattern) DO UPDATE SET multiplier=excluded.multiplier, updated_at=excluded.updated_at",
            (args.pattern, float(args.multiplier), store.iso()))
        rt.conn.commit()
    print(f"{args.pattern} → ×{args.multiplier}")
    rt.close()


def cmd_sync(args) -> None:
    rt = _rt(args)
    results = syncer.sync_all(rt.pool, rt.conn, only=[args.account] if args.account else None)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    rt.close()


def cmd_devices(args) -> None:
    rt = _rt(args)
    rows = rt.conn.execute(
        "SELECT * FROM devices" + (" WHERE user_id=?" if args.user else "") + " ORDER BY account_id, rc_device_id",
        (args.user,) if args.user else ()).fetchall()
    for r in rows:
        print(f"{r['account_id']}:{r['rc_device_id']}  user={r['user_id']}  state={r['state']}  "
              f"last_used={r['last_used_at']}  {r['name']}")
    rt.close()


def cmd_revoke(args) -> None:
    rt = _rt(args)
    plan = evictor.evict(rt.pool, rt.conn, args.account, args.device, reason="cli",
                         actor="cli", dry_run=not args.yes, force=args.force)
    print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    rt.close()


def cmd_enroll(args) -> None:
    rt = _rt(args)
    try:
        plan = enroll.enroll(rt.pool, rt.conn, state=args.state, tg_user_id=args.user,
                             device_name=args.name or f"tg{args.user}", dry_run=not args.yes,
                             preferred_account=args.account, actor="cli")
    except enroll.NoSlot as exc:
        sys.exit(f"无空位：{exc}")
    print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    rt.close()


def cmd_report(args) -> None:
    rt = _rt(args)
    start = billing.parse_date(args.start) if args.start else None
    end = billing.parse_date(args.end) if args.end else None
    bill = billing.compute(rt.conn, start, end)
    if args.json:
        print(json.dumps(bill, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"账单 {bill['start']} ~ {bill['end']}")
        for u, amt in sorted(bill["users"].items(), key=lambda kv: -kv[1]):
            print(f"  tg:{u}  ¥{amt:,.2f}")
        print(f"  合计 ¥{sum(bill['users'].values()):,.2f}")
        for key, items in bill["anomalies"].items():
            if items:
                print(f"  [{key}] {items}")
    if args.save:
        ids = billing.create_invoices(rt.conn, bill)
        print(f"已落账单：{ids}")
    rt.close()


def cmd_health(args) -> None:
    rt = _rt(args)
    for a in store.enabled_accounts(rt.conn):
        wm = rt.conn.execute("SELECT * FROM watermarks WHERE account_id=?", (a["id"],)).fetchone()
        print(f"{a['id']}: status={a['status']} used={enroll.used_slots(rt.conn, a['id'])}"
              f"/{a['capacity']} last_sync={wm['last_synced_at'] if wm else None}"
              f" err={(a['last_error'] or '')[:80]}")
    print("requests:", rt.conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"],
          "unpriced:", rt.conn.execute("SELECT COUNT(*) c FROM requests WHERE cost_usd IS NULL").fetchone()["c"])
    rt.close()


def cmd_serve(args) -> None:
    from .bot import main as bot_main
    bot_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reclaude-billing")
    p.add_argument("--db", default=None, help="SQLite 路径（默认 $RC_BILLING_DB）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.set_defaults(fn=cmd_init)

    s = sub.add_parser("account-add")
    s.add_argument("--id", required=True); s.add_argument("--email", required=True)
    s.add_argument("--password", default="")
    s.add_argument("--start"); s.add_argument("--end"); s.add_argument("--cost")
    s.set_defaults(fn=cmd_account_add)

    s = sub.add_parser("account-del"); s.add_argument("--id", required=True)
    s.set_defaults(fn=cmd_account_del)

    s = sub.add_parser("period-add")
    s.add_argument("--account", required=True); s.add_argument("--start", required=True)
    s.add_argument("--end", required=True); s.add_argument("--cost", required=True, type=float)
    s.add_argument("--idle-split", default="none", choices=["none", "even"])
    s.set_defaults(fn=cmd_period_add)

    s = sub.add_parser("rate")
    s.add_argument("pattern"); s.add_argument("multiplier", type=float)
    s.set_defaults(fn=cmd_rate)

    s = sub.add_parser("sync"); s.add_argument("--account")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("devices"); s.add_argument("--user", type=int)
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("revoke")
    s.add_argument("--account", required=True); s.add_argument("--device", type=int, required=True)
    s.add_argument("--yes", action="store_true", help="真的撤销（默认演练）")
    s.add_argument("--force", action="store_true", help="忽略闲置门槛")
    s.set_defaults(fn=cmd_revoke)

    s = sub.add_parser("enroll")
    s.add_argument("--state", required=True); s.add_argument("--user", type=int, required=True)
    s.add_argument("--name"); s.add_argument("--account")
    s.add_argument("--yes", action="store_true", help="真的批准（默认演练）")
    s.set_defaults(fn=cmd_enroll)

    s = sub.add_parser("report")
    s.add_argument("--start"); s.add_argument("--end")
    s.add_argument("--json", action="store_true"); s.add_argument("--save", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("health"); s.set_defaults(fn=cmd_health)
    s = sub.add_parser("serve"); s.set_defaults(fn=cmd_serve)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
