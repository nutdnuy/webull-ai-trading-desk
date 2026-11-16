#!/usr/bin/env python3
"""Command line for the AI trading desk. Standard library only.

    python3 desk/tools/desk.py <command> [options]

Every command works on the desk folder that holds this script (override with
--desk or $TRADING_DESK_DIR). No command talks to the broker: orders go
through the Webull MCP tools, and the plugin's order gate checks them against
the tickets this script writes.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import desklib as dl  # noqa: E402


def resolve_desk(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("TRADING_DESK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent
    if here.name == "tools" and (here.parent / "limits.json").is_file():
        return here.parent
    found = dl.find_desk()
    if found is None:
        raise dl.DeskError("desk folder not found; run the onboarding skill or pass --desk")
    return found


def resolve_today(arg: str | None) -> date:
    text = arg or os.environ.get("DESK_TODAY")
    return dl.parse_date(text) if text else dl.today_ny()


def show_ticket(ticket: dict) -> str:
    return (
        f"#{ticket['id']} {ticket['symbol']} [{ticket['status']}] "
        f"BUY {ticket['shares']:g} LIMIT {ticket['limit_price']:.2f} DAY | "
        f"stop {ticket['stop_price']:.2f} | risk ${ticket['risk_usd']:,.2f} | "
        f"cost ${ticket['notional_usd']:,.2f} | exit due {ticket['exit_due']}"
    )


# ---------------------------------------------------------------- commands

def cmd_init(args) -> int:
    """Create a desk folder from the template. Never overwrites limits.json."""
    desk = Path(args.path).expanduser().resolve()
    root = Path(__file__).resolve().parent.parent
    template = root / "desk-template"
    if not template.is_dir():
        raise dl.DeskError("desk-template not found; run init from the plugin's scripts/desk.py")
    for sub in ("inbox", "account", "journal", "data/bars", "tools"):
        (desk / sub).mkdir(parents=True, exist_ok=True)
    created, kept = [], []
    for src in sorted(template.rglob("*")):
        if src.is_dir() or "__pycache__" in src.parts:
            continue
        dest = desk / src.relative_to(template)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            kept.append(str(dest.relative_to(desk)))
            continue
        shutil.copy2(src, dest)
        created.append(str(dest.relative_to(desk)))
    for name in ("desk.py", "desklib.py"):  # tools always track the installed version
        shutil.copy2(Path(__file__).resolve().parent / name, desk / "tools" / name)
    shutil.copy2(template / "PROTOCOL.md", desk / "PROTOCOL.md")
    print(f"Desk ready at {desk}")
    print(f"  created: {', '.join(created) or 'nothing new'}")
    if kept:
        print(f"  kept as is: {', '.join(kept)}")
    print(f"  tools refreshed to version {dl.VERSION}")
    return 0


def cmd_status(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    limits = dl.load_limits(desk)
    broker = dl.load_broker(desk)
    print(f"Desk: {desk}")
    print(f"Today (New York): {today.isoformat()}  trading day: {'yes' if dl.is_trading_day(today) else 'NO'}")
    print(f"Flag: {dl.blocking_flag(desk) or 'none'}")
    print(f"Broker: mode={broker.get('mode', 'not set')} environment={broker.get('environment', 'not set')}")
    print("Limits: " + ", ".join(f"{k}={v}" for k, v in limits.items()))
    tickets = dl.list_tickets(desk, today)
    print(f"Tickets today: {len(tickets)}")
    for ticket in tickets:
        print("  " + show_ticket(ticket))
    return 0


def cmd_broker(args) -> int:
    """Record how this desk reaches Webull. Holds no keys and no account numbers."""
    desk = resolve_desk(args.desk)
    broker = dl.load_broker(desk)
    if args.mode:
        broker["mode"] = args.mode
    if args.environment:
        broker["environment"] = args.environment
    if broker.get("mode") == "cloud":
        broker["environment"] = "prod"  # Webull Cloud MCP has no sandbox
    for key in ("region", "order_tool", "stop_method"):
        value = getattr(args, key)
        if value:
            broker[key] = value
    if args.mode or args.environment or args.region or args.order_tool or args.stop_method:
        broker["updated"] = dl.now_ny().isoformat(timespec="seconds")
        dl.write_json(desk / "broker.json", broker)
    print(json.dumps(broker, indent=2))
    limits = dl.load_limits(desk)
    live = broker.get("environment") not in ("uat", "paper", "sandbox")
    if limits["paper_only"] and live:
        print("NOTE: limits.json has paper_only=true and this broker setting is not a sandbox, "
              "so every BUY ticket will fail risk-check. That is the safe default.")
    if not limits["paper_only"] and live:
        print("WARNING: LIVE MONEY. paper_only is false and the broker environment is production.")
    return 0


def cmd_snapshot(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise dl.DeskError(f"snapshot is not valid JSON: {exc}") from exc
    snap = dl.save_snapshot(desk, data, today)
    limits = dl.load_limits(desk)
    print(f"Snapshot saved: {dl.snapshot_path(desk, today)}")
    print(f"  equity ${snap['equity_usd']:,.2f} | buying power ${snap['buying_power_usd']:,.2f} | "
          f"{len(snap['positions'])} position(s) | {len(snap['open_orders'])} open order(s)")
    for symbol in dl.missing_stops(snap):
        print(f"  MISSING STOP: {symbol} has no open sell stop covering the full position")
    loss = dl.daily_loss_hit(snap, limits)
    if loss is not None:
        (desk / "PAUSE").write_text(f"daily loss limit hit: {loss:.2f}%\n", encoding="utf-8")
        print(f"  DAILY LOSS LIMIT HIT ({loss:.2f}%). PAUSE created: no new tickets.")
    return 0


def cmd_bars(args) -> int:
    bars = dl.load_bars(Path(args.file))
    last = bars[-1]
    print(f"{len(bars)} bars, {bars[0]['date']} .. {last['date']}")
    print(f"last_close={last['close']:.4f}")
    print(f"atr14={dl.atr(bars):.4f}")
    adv = dl.average_dollar_volume(bars)
    print(f"avg_dollar_volume_20d={adv:,.0f}")
    if args.min_adv is not None:
        ok = adv >= args.min_adv
        print(f"liquidity {'PASS' if ok else 'FAIL'} (minimum {args.min_adv:,.0f})")
        return 0 if ok else 1
    return 0


def cmd_ticket(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    flag = dl.blocking_flag(desk)
    if flag:
        raise dl.DeskError(f"{flag} is active: no new tickets")
    limits = dl.load_limits(desk)
    snapshot = dl.load_snapshot(desk, today)
    if args.bars:
        bars = dl.load_bars(Path(args.bars))
        if bars[-1]["date"] >= today and not args.allow_today_bar:
            raise dl.DeskError("the last bar is from today; a ticket is built from the last completed close")
        last_close, atr_value = bars[-1]["close"], dl.atr(bars)
    elif args.last_close is not None and args.atr is not None:
        last_close, atr_value = args.last_close, args.atr
    else:
        raise dl.DeskError("give --bars FILE, or both --last-close and --atr")
    notes = {}
    for item in args.note or []:
        key, _, value = item.partition("=")
        notes[key.strip()] = value.strip()
    ticket = dl.build_ticket(desk, args.symbol, last_close, atr_value, snapshot, limits, today, notes)
    results = dl.risk_check(desk, ticket, today, limits=limits, snapshot=snapshot)
    ticket["checks"] = [{"ok": ok, "rule": rule, "detail": detail} for ok, rule, detail in results]
    if ticket["shares"] <= 0:
        status = "SIZE_ZERO"
    else:
        status = "CHECKED" if dl.check_passed(results) else "FAILED_RISK_CHECK"
    dl.set_status(desk, ticket, status)
    print(show_ticket(ticket))
    print(f"  sized by: {ticket['sized_by']} | last close {last_close:.2f} | ATR(14) {atr_value:.4f}")
    print(dl.format_checks(results))
    print(f"RESULT: {'PASS' if status == 'CHECKED' else 'FAIL'} ({status})")
    return 0 if status == "CHECKED" else 1


def cmd_risk_check(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    ticket = dl.get_ticket(desk, today, args.number)
    results = dl.risk_check(desk, ticket, today)
    print(show_ticket(ticket))
    print(dl.format_checks(results))
    passed = dl.check_passed(results)
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def cmd_tickets(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    tickets = dl.list_tickets(desk, None if args.all else today)
    if not tickets:
        print("no tickets")
    for ticket in tickets:
        print(f"{ticket['date']} " + show_ticket(ticket))
    return 0


def cmd_approve(args) -> int:
    """Record the PM's approval. Run only after the PM named these ticket numbers."""
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    code = 0
    for number in args.numbers:
        ticket = dl.get_ticket(desk, today, number)
        if ticket["status"] not in ("CHECKED", "APPROVED"):
            print(f"#{number}: cannot approve from status {ticket['status']}")
            code = 1
            continue
        results = dl.risk_check(desk, ticket, today)
        if not dl.check_passed(results):
            dl.set_status(desk, ticket, "FAILED_RISK_CHECK", "failed at approval time")
            print(f"#{number}: FAILED risk-check at approval time\n{dl.format_checks(results)}")
            code = 1
            continue
        dl.set_status(desk, ticket, "APPROVED", "approved by PM")
        print("APPROVED " + show_ticket(ticket))
    return code


def cmd_set_status(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    day = dl.parse_date(args.date) if args.date else today
    ticket = dl.get_ticket(desk, day, args.number)
    allowed = {
        "reject": ("REJECTED", ("CHECKED", "APPROVED")),
        "sent": ("SENT", ("APPROVED",)),
        "reopen": ("APPROVED", ("SENT",)),
        "not-filled": ("NOT_FILLED", ("APPROVED", "SENT")),
        "fill": ("FILLED", ("APPROVED", "SENT")),
        "close": ("CLOSED", ("FILLED",)),
    }
    status, sources = allowed[args.command]
    if ticket["status"] not in sources:
        raise dl.DeskError(f"#{args.number} is {ticket['status']}; '{args.command}' needs {' or '.join(sources)}")
    if args.command == "reopen" and day != today:
        raise dl.DeskError("only today's ticket can be reopened")
    if args.command == "fill":
        fill_day = dl.parse_date(args.fill_date) if args.fill_date else today
        ticket["fill_price"] = args.price
        ticket["fill_qty"] = args.qty if args.qty is not None else ticket["shares"]
        ticket["fill_date"] = fill_day.isoformat()
        ticket["exit_due"] = dl.add_trading_days(fill_day, int(dl.load_limits(desk)["hold_days"])).isoformat()
    if args.command == "close":
        ticket["exit_price"] = args.price
        ticket["exit_date"] = (dl.parse_date(args.exit_date) if args.exit_date else today).isoformat()
        ticket["exit_reason"] = args.reason
        qty = ticket.get("fill_qty", ticket["shares"])
        entry = ticket.get("fill_price", ticket["limit_price"])
        risk_per_share = ticket["limit_price"] - ticket["stop_price"]
        pnl = (args.price - entry) * qty
        ticket["pnl_usd"] = round(pnl, 2)
        ticket["pnl_r"] = round((args.price - entry) / risk_per_share, 4)
        dl.append_journal(desk, {
            "date": ticket["date"], "ticker": ticket["symbol"], "outcome": "FILLED",
            "ticket": ticket["id"], "entry": entry, "exit": args.price, "qty": qty,
            "pnl_usd": ticket["pnl_usd"], "pnl_r": ticket["pnl_r"], "note": args.reason,
        })
    if args.command in ("reject", "not-filled"):
        dl.append_journal(desk, {
            "date": ticket["date"], "ticker": ticket["symbol"],
            "outcome": "REJECTED BY ME" if args.command == "reject" else "NOT FILLED",
            "ticket": ticket["id"], "entry": ticket["limit_price"], "qty": ticket["shares"],
            "note": getattr(args, "note", "") or "",
        })
    dl.set_status(desk, ticket, status, getattr(args, "note", "") or "")
    print(show_ticket(ticket))
    return 0


def cmd_exits_due(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    due = dl.exits_due(desk, today)
    if not due:
        print("No exits due today.")
    for ticket in due:
        qty = ticket.get("fill_qty", ticket["shares"])
        print(f"EXIT DUE: {ticket['symbol']} qty {qty:g} (ticket {ticket['date']} #{ticket['id']}, "
              f"filled {ticket.get('fill_date')}, due {ticket['exit_due']}): cancel its stop, then sell at market")
    try:
        snapshot = dl.load_snapshot(desk, today)
    except dl.DeskError as exc:
        print(f"Stop check skipped: {exc}")
        return 0
    for symbol in dl.missing_stops(snapshot):
        print(f"MISSING STOP: {symbol}")
    return 0


def cmd_flag(args) -> int:
    desk = resolve_desk(args.desk)
    if args.command == "pause":
        (desk / "PAUSE").write_text("paused by PM\n", encoding="utf-8")
        print("PAUSE created. No new tickets and no new BUY orders until RESUME.")
    elif args.command == "resume":
        if (desk / "PAUSE").exists():
            (desk / "PAUSE").unlink()
        print("PAUSE cleared." + (" KILL is still active." if (desk / "KILL").exists() else ""))
    else:  # kill
        (desk / "KILL").write_text("killed by PM\n", encoding="utf-8")
        print("KILL created. No new BUY orders can pass the gate.")
        print("Next: cancel every open order with the broker's cancel tool. Open orders in the last snapshot:")
        snaps = sorted((desk / "account").glob("????-??-??.json"))
        orders = dl.read_json(snaps[-1]).get("open_orders", []) if snaps else []
        for order in orders:
            print(f"  {order.get('side')} {order.get('quantity')} {order.get('symbol')} "
                  f"{order.get('order_type')} id={order.get('client_order_id')}")
        if not orders:
            print("  (none recorded; take a fresh snapshot to be sure)")
        print("Only the PM removes KILL, by deleting the file by hand.")
    return 0


def cmd_log(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    dl.append_journal(desk, {
        "date": args.date or today.isoformat(), "ticker": args.ticker, "outcome": args.outcome,
        "ticket": args.ticket, "entry": args.entry, "exit": args.exit, "qty": args.qty,
        "pnl_usd": args.pnl_usd, "pnl_r": args.pnl_r, "note": args.note,
    })
    print(f"logged {args.ticker.upper()} {args.outcome.upper()}")
    return 0


def cmd_review(args) -> int:
    desk = resolve_desk(args.desk)
    backtest_avg_r = args.backtest_avg_r
    if backtest_avg_r is None:
        result_file = desk / "quant" / "results" / "build.json"
        if result_file.is_file():
            backtest_avg_r = dl.to_float(dl.read_json(result_file).get("metrics", {}).get("avg_r"))
    print(json.dumps(dl.review(desk, backtest_avg_r), indent=2))
    return 0


def cmd_health(args) -> int:
    desk, today = resolve_desk(args.desk), resolve_today(args.today)
    problems: list[str] = []
    try:
        dl.load_limits(desk)
    except dl.DeskError as exc:
        problems.append(str(exc))
    broker = dl.load_broker(desk)
    if not broker.get("mode"):
        problems.append("broker.json has no mode; run the onboarding skill")
    for name in ("lessons.md", "tools/desk.py", "tools/desklib.py"):
        if not (desk / name).is_file():
            problems.append(f"missing {name}")
    stamp = today.isoformat()
    if dl.is_trading_day(today):
        for seat in ("scout", "analyst", "skeptic"):
            if not (desk / "inbox" / f"{stamp}-{seat}.md").is_file():
                problems.append(f"CHAIN BROKEN: inbox/{stamp}-{seat}.md is missing")
        if not dl.snapshot_path(desk, today).is_file():
            problems.append(f"CHAIN BROKEN: account/{stamp}.json is missing")
    flag = dl.blocking_flag(desk)
    print(f"Desk {desk} on {stamp} (trading day: {'yes' if dl.is_trading_day(today) else 'no'})")
    print(f"Flag: {flag or 'none'} | tickets today: {len(dl.list_tickets(desk, today))}")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    print("HEALTH: " + ("OK" if not problems else f"{len(problems)} problem(s)"))
    return 0 if not problems else 1


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--desk", help="desk folder (default: the folder holding tools/)")
    common.add_argument("--today", help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="desk.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a desk folder from the template")
    p.add_argument("path")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", parents=[common], help="limits, flags and today's tickets")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("broker", parents=[common], help="show or set how the desk reaches Webull")
    p.add_argument("--mode", choices=("cloud", "local"))
    p.add_argument("--environment", choices=("uat", "prod"))
    p.add_argument("--region")
    p.add_argument("--order-tool", dest="order_tool", help="e.g. place_stock_order or place_stock_instruction")
    p.add_argument("--stop-method", dest="stop_method", choices=("oto", "after_fill"))
    p.set_defaults(func=cmd_broker)

    p = sub.add_parser("snapshot", parents=[common], help="validate and save today's account snapshot")
    p.add_argument("file", help="JSON file, or - for stdin")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("bars", help="check a daily bars CSV; print last close, ATR(14), dollar volume")
    p.add_argument("file")
    p.add_argument("--min-adv", type=float, help="fail if 20-day average dollar volume is below this")
    p.set_defaults(func=cmd_bars)

    p = sub.add_parser("ticket", parents=[common], help="size a BUY ticket and risk-check it")
    p.add_argument("symbol")
    p.add_argument("--bars", help="daily bars CSV ending at the last completed close")
    p.add_argument("--last-close", type=float)
    p.add_argument("--atr", type=float)
    p.add_argument("--allow-today-bar", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--note", action="append", help="key=value, e.g. scout=inbox/2026-01-05-scout.md")
    p.set_defaults(func=cmd_ticket)

    p = sub.add_parser("risk-check", parents=[common], help="re-check one of today's tickets")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_risk_check)

    p = sub.add_parser("tickets", parents=[common], help="list tickets")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_tickets)

    p = sub.add_parser("approve", parents=[common], help="record the PM's approval of ticket numbers")
    p.add_argument("numbers", type=int, nargs="+")
    p.set_defaults(func=cmd_approve)

    for name, text in (
        ("reject", "PM did not approve the ticket"),
        ("sent", "the order was sent to the broker"),
        ("reopen", "a SENT ticket whose order did not go through; back to APPROVED"),
        ("not-filled", "the DAY order expired unfilled"),
    ):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("number", type=int)
        p.add_argument("--date", help="ticket date (default today)")
        p.add_argument("--note", default="")
        p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("fill", parents=[common], help="record the entry fill")
    p.add_argument("number", type=int)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--qty", type=float)
    p.add_argument("--fill-date")
    p.add_argument("--date", help="ticket date (default today)")
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("close", parents=[common], help="record the exit and journal the trade")
    p.add_argument("number", type=int)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--reason", default="day-5 exit", help="day-5 exit | stop | manual")
    p.add_argument("--exit-date")
    p.add_argument("--date", help="ticket date (default today)")
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("exits-due", parents=[common], help="positions due to exit and positions with no stop")
    p.set_defaults(func=cmd_exits_due)

    for name, text in (("pause", "stop new tickets"), ("resume", "clear PAUSE"), ("kill", "stop everything")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.set_defaults(func=cmd_flag)

    p = sub.add_parser("log", parents=[common], help="add a journal row (killed, rejected, off-plan names)")
    p.add_argument("ticker")
    p.add_argument("outcome", help="; ".join(dl.OUTCOMES))
    p.add_argument("--date")
    p.add_argument("--ticket")
    p.add_argument("--entry", type=float)
    p.add_argument("--exit", type=float)
    p.add_argument("--qty", type=float)
    p.add_argument("--pnl-usd", type=float)
    p.add_argument("--pnl-r", type=float)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("review", parents=[common], help="live results vs the backtest; DECAYING check")
    p.add_argument("--backtest-avg-r", type=float)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("health", parents=[common], help="is today's chain complete?")
    p.set_defaults(func=cmd_health)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except dl.DeskError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"ERROR: file not found: {exc.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
