"""Safety layer for the AI trading desk.

Standard library only. This module holds every rule that guards money:
position sizing, hard limits, the order gate and the market calendar.
The AI proposes; this code decides what may reach the broker.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    _NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - only on systems without tzdata
    _NY = None

VERSION = "0.1.0"

DEFAULT_LIMITS: dict[str, Any] = {
    "risk_per_trade_pct": 1.0,
    "max_open_positions": 5,
    "max_position_pct": 10.0,
    "max_daily_loss_pct": 3.0,
    "paper_only": True,
    "cash_only": True,
    "allow_fractional": False,
    "auto_allow_exits": False,
    "entry_buffer_pct": 0.5,
    "stop_atr_mult": 2.0,
    "hold_days": 5,
    "max_order_notional_usd": None,
}

_LIMIT_RANGES = {
    "risk_per_trade_pct": (0.01, 5.0),
    "max_open_positions": (1, 50),
    "max_position_pct": (0.1, 100.0),
    "max_daily_loss_pct": (0.1, 50.0),
    "entry_buffer_pct": (0.0, 5.0),
    "stop_atr_mult": (0.1, 10.0),
    "hold_days": (1, 60),
}

OPEN_STATUSES = ("APPROVED", "SENT", "FILLED")


class DeskError(Exception):
    """A rule was broken or an input is invalid."""


# --------------------------------------------------------------------------
# Time and market calendar
# --------------------------------------------------------------------------

def now_ny() -> datetime:
    if _NY is not None:
        return datetime.now(_NY)
    return datetime.now(timezone.utc) - timedelta(hours=5)


def today_ny() -> date:
    """Today's date in New York. Desk files are named by this date."""
    return now_ny().date()


def parse_date(text: str) -> date:
    return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    """Scheduled full-day NYSE closures. Unscheduled closures are not known."""
    days: set[date] = set()
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        days.add(date(year, 1, 2))
    elif new_year.weekday() != 5:  # a Saturday New Year is not observed
        days.add(new_year)
    days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    days.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    days.add(_easter(year) - timedelta(days=2))  # Good Friday
    days.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))  # Juneteenth
    days.add(_observed(date(year, 7, 4)))  # Independence Day
    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    days.add(_observed(date(year, 12, 25)))  # Christmas
    return {d for d in days if d is not None}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def add_trading_days(d: date, n: int) -> date:
    """The n-th trading day after d (n >= 0; n == 0 returns d)."""
    cur = d
    left = n
    while left > 0:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            left -= 1
    return cur


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def find_desk(start: str | os.PathLike | None = None) -> Path | None:
    """Locate the desk folder: $TRADING_DESK_DIR, then <project>/desk."""
    candidates: list[Path] = []
    env = os.environ.get("TRADING_DESK_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    for base in (os.environ.get("CLAUDE_PROJECT_DIR"), start, os.getcwd()):
        if base:
            base_path = Path(base).expanduser()
            candidates.append(base_path / "desk")
            candidates.append(base_path)
    for cand in candidates:
        if (cand / "limits.json").is_file() and (cand / "inbox").is_dir():
            return cand.resolve()
    return None


def load_limits(desk: Path) -> dict[str, Any]:
    path = desk / "limits.json"
    if not path.is_file():
        raise DeskError(f"limits.json not found in {desk}")
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise DeskError(f"limits.json is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise DeskError("limits.json must be a JSON object")
    limits = dict(DEFAULT_LIMITS)
    limits.update(raw)
    for key, (low, high) in _LIMIT_RANGES.items():
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DeskError(f"limits.json: {key} must be a number")
        if not low <= value <= high:
            raise DeskError(f"limits.json: {key}={value} is outside {low}..{high}")
    for key in ("paper_only", "cash_only", "allow_fractional", "auto_allow_exits"):
        if not isinstance(limits.get(key), bool):
            raise DeskError(f"limits.json: {key} must be true or false")
    cap = limits.get("max_order_notional_usd")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0):
        raise DeskError("limits.json: max_order_notional_usd must be a positive number or null")
    return limits


def load_broker(desk: Path) -> dict[str, Any]:
    path = desk / "broker.json"
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def blocking_flag(desk: Path) -> str | None:
    for flag in ("KILL", "PAUSE"):
        if (desk / flag).exists():
            return flag
    return None


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------

def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def floor_shares(value: float, fractional: bool) -> float:
    if value <= 0:
        return 0
    if fractional:
        return math.floor(value * 10000 + 1e-9) / 10000
    return float(math.floor(value + 1e-9))


# --------------------------------------------------------------------------
# Bars, ATR, liquidity
# --------------------------------------------------------------------------

def load_bars(path: Path) -> list[dict[str, Any]]:
    """Read a daily OHLCV CSV: date,open,high,low,close,volume (oldest first)."""
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = [f.strip().lower() for f in (reader.fieldnames or [])]
        needed = ["date", "open", "high", "low", "close", "volume"]
        missing = [n for n in needed if n not in fields]
        if missing:
            raise DeskError(f"{path.name}: missing column(s) {', '.join(missing)}")
        for line_no, raw in enumerate(reader, start=2):
            row = {k.strip().lower(): v for k, v in raw.items() if k}
            try:
                bar = {
                    "date": parse_date(row["date"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"] or 0),
                }
            except (TypeError, ValueError) as exc:
                raise DeskError(f"{path.name} line {line_no}: {exc}") from exc
            rows.append(bar)
    validate_bars(rows, path.name)
    return rows


def validate_bars(bars: list[dict[str, Any]], label: str = "bars") -> None:
    previous = None
    for bar in bars:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        if min(o, h, l, c) <= 0:
            raise DeskError(f"{label} {bar['date']}: prices must be positive")
        if h + 1e-9 < max(o, c) or l - 1e-9 > min(o, c) or h + 1e-9 < l:
            raise DeskError(
                f"{label} {bar['date']}: high/low do not contain open/close "
                "(columns may be in the wrong order)"
            )
        if previous is not None and bar["date"] <= previous:
            raise DeskError(f"{label}: dates must be oldest first with no repeats ({bar['date']})")
        previous = bar["date"]


def atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    """Wilder's Average True Range over all bars given."""
    if len(bars) < period + 1:
        raise DeskError(f"ATR({period}) needs at least {period + 1} daily bars, got {len(bars)}")
    true_ranges = []
    for prev, cur in zip(bars, bars[1:]):
        true_ranges.append(
            max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"]))
        )
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def average_dollar_volume(bars: list[dict[str, Any]], days: int = 20) -> float:
    window = bars[-days:]
    if not window:
        raise DeskError("no bars")
    return sum(b["close"] * b["volume"] for b in window) / len(window)


# --------------------------------------------------------------------------
# Account snapshot
# --------------------------------------------------------------------------

def snapshot_path(desk: Path, day: date) -> Path:
    return desk / "account" / f"{day.isoformat()}.json"


def normalize_snapshot(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DeskError("snapshot must be a JSON object")
    equity = to_float(data.get("equity_usd"))
    buying_power = to_float(data.get("buying_power_usd"))
    if equity is None or equity <= 0:
        raise DeskError("snapshot: equity_usd must be a positive number")
    if buying_power is None or buying_power < 0:
        raise DeskError("snapshot: buying_power_usd must be zero or more")
    positions = []
    for pos in data.get("positions") or []:
        symbol = str(pos.get("symbol", "")).strip().upper()
        qty = to_float(pos.get("quantity"))
        if not symbol or qty is None:
            raise DeskError("snapshot: every position needs symbol and quantity")
        if qty == 0:
            continue
        positions.append(
            {
                "symbol": symbol,
                "quantity": qty,
                "cost_price": to_float(pos.get("cost_price")),
                "last_price": to_float(pos.get("last_price")),
            }
        )
    orders = []
    for order in data.get("open_orders") or []:
        symbol = str(order.get("symbol", "")).strip().upper()
        if not symbol:
            raise DeskError("snapshot: every open order needs a symbol")
        orders.append(
            {
                "symbol": symbol,
                "side": str(order.get("side", "")).strip().upper(),
                "order_type": str(order.get("order_type", "")).strip().upper(),
                "quantity": to_float(order.get("quantity")),
                "limit_price": to_float(order.get("limit_price")),
                "stop_price": to_float(order.get("stop_price")),
                "client_order_id": order.get("client_order_id"),
            }
        )
    return {
        "as_of": str(data.get("as_of") or now_ny().isoformat(timespec="seconds")),
        "equity_usd": equity,
        "buying_power_usd": buying_power,
        "positions": positions,
        "open_orders": orders,
    }


def save_snapshot(desk: Path, data: Any, today: date) -> dict[str, Any]:
    snap = normalize_snapshot(data)
    snap["date"] = today.isoformat()
    path = snapshot_path(desk, today)
    prior = None
    if path.is_file():  # keep the first prior of the day
        try:
            prior = read_json(path).get("prior_equity_usd")
        except (OSError, ValueError, AttributeError):
            prior = None
    if prior is None:
        earlier = sorted(
            p for p in (desk / "account").glob("????-??-??.json") if p.stem < today.isoformat()
        )
        if earlier:
            try:
                prior = to_float(read_json(earlier[-1]).get("equity_usd"))
            except (OSError, ValueError, AttributeError):
                prior = None
    snap["prior_equity_usd"] = prior
    write_json(path, snap)
    return snap


def load_snapshot(desk: Path, today: date) -> dict[str, Any]:
    path = snapshot_path(desk, today)
    if not path.is_file():
        raise DeskError(
            f"no account snapshot for {today.isoformat()} (expected {path.name}); "
            "take a fresh snapshot first"
        )
    snap = read_json(path)
    if snap.get("date") != today.isoformat():
        raise DeskError("account snapshot is not from today")
    return snap


def held_symbols(snapshot: dict[str, Any]) -> dict[str, float]:
    return {p["symbol"]: p["quantity"] for p in snapshot.get("positions", []) if p.get("quantity")}


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------

def ticket_path(desk: Path, day: date, number: int) -> Path:
    return desk / "inbox" / f"{day.isoformat()}-ticket-{number}.json"


def list_tickets(desk: Path, day: date | None = None) -> list[dict[str, Any]]:
    pattern = f"{day.isoformat()}-ticket-*.json" if day else "????-??-??-ticket-*.json"
    tickets = []
    for path in sorted((desk / "inbox").glob(pattern)):
        try:
            ticket = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(ticket, dict) and "symbol" in ticket:
            ticket["_path"] = str(path)
            tickets.append(ticket)
    tickets.sort(key=lambda t: (t.get("date", ""), t.get("id", 0)))
    return tickets


def save_ticket(desk: Path, ticket: dict[str, Any]) -> Path:
    path = ticket_path(desk, parse_date(ticket["date"]), int(ticket["id"]))
    clean = {k: v for k, v in ticket.items() if not k.startswith("_")}
    write_json(path, clean)
    return path


def set_status(desk: Path, ticket: dict[str, Any], status: str, note: str = "") -> dict[str, Any]:
    ticket["status"] = status
    ticket.setdefault("history", []).append(
        {"status": status, "at": now_ny().isoformat(timespec="seconds"), "note": note}
    )
    save_ticket(desk, ticket)
    return ticket


def get_ticket(desk: Path, day: date, number: int) -> dict[str, Any]:
    path = ticket_path(desk, day, number)
    if not path.is_file():
        raise DeskError(f"ticket {number} for {day.isoformat()} not found")
    ticket = read_json(path)
    ticket["_path"] = str(path)
    return ticket


def size_position(entry: float, stop: float, snapshot: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    """shares = equity x risk% / (entry - stop), rounded down, then capped."""
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        raise DeskError("stop must be below entry")
    equity = snapshot["equity_usd"]
    fractional = limits["allow_fractional"]
    by_risk = equity * limits["risk_per_trade_pct"] / 100.0 / risk_per_share
    caps = {"risk": by_risk, "max_position_pct": equity * limits["max_position_pct"] / 100.0 / entry}
    if limits["cash_only"]:
        caps["buying_power"] = snapshot["buying_power_usd"] / entry
    if limits.get("max_order_notional_usd"):
        caps["max_order_notional_usd"] = limits["max_order_notional_usd"] / entry
    binding = min(caps, key=caps.get)
    shares = floor_shares(caps[binding], fractional)
    return {"shares": shares, "binding": binding, "risk_per_share": risk_per_share}


def build_ticket(
    desk: Path,
    symbol: str,
    last_close: float,
    atr_value: float,
    snapshot: dict[str, Any],
    limits: dict[str, Any],
    today: date,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        raise DeskError(f"bad symbol: {symbol!r}")
    if last_close <= 0 or atr_value <= 0:
        raise DeskError("last close and ATR must be positive")
    entry = round(last_close * (1 + limits["entry_buffer_pct"] / 100.0), 2)
    stop = round(entry - limits["stop_atr_mult"] * atr_value, 2)
    if stop <= 0:
        raise DeskError("stop price would be zero or negative")
    sizing = size_position(entry, stop, snapshot, limits)
    shares = sizing["shares"]
    existing = [t.get("id", 0) for t in list_tickets(desk, today)]
    ticket = {
        "id": max(existing, default=0) + 1,
        "date": today.isoformat(),
        "symbol": symbol,
        "side": "BUY",
        "order_type": "LIMIT",
        "time_in_force": "DAY",
        "limit_price": entry,
        "stop_price": stop,
        "shares": shares,
        "risk_usd": round(shares * (entry - stop), 2),
        "notional_usd": round(shares * entry, 2),
        "last_close": last_close,
        "atr14": round(atr_value, 4),
        "equity_usd": snapshot["equity_usd"],
        "sized_by": sizing["binding"],
        "exit_due": add_trading_days(today, int(limits["hold_days"])).isoformat(),
        "notes": notes or {},
        "status": "PROPOSED",
        "checks": [],
        "history": [],
    }
    return ticket


def pending_symbols(desk: Path, today: date, exclude_id: int | None = None) -> set[str]:
    """Symbols with an approved or live ticket today (count toward open positions)."""
    result = set()
    for ticket in list_tickets(desk, today):
        if ticket.get("id") == exclude_id:
            continue
        if ticket.get("status") in OPEN_STATUSES:
            result.add(ticket["symbol"])
    return result


def risk_check(
    desk: Path,
    ticket: dict[str, Any],
    today: date,
    limits: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> list[tuple[bool, str, str]]:
    """Check one ticket against every hard limit. Returns (ok, rule, detail) rows."""
    results: list[tuple[bool, str, str]] = []

    def add(ok: bool, rule: str, detail: str) -> None:
        results.append((bool(ok), rule, detail))

    flag = blocking_flag(desk)
    add(flag is None, "no KILL/PAUSE", f"{flag} file exists" if flag else "clear")

    try:
        limits = limits or load_limits(desk)
    except DeskError as exc:
        add(False, "limits.json valid", str(exc))
        return results
    try:
        snapshot = snapshot or load_snapshot(desk, today)
    except (DeskError, OSError, ValueError) as exc:
        add(False, "fresh account snapshot", str(exc))
        return results
    add(True, "fresh account snapshot", snapshot.get("as_of", ""))

    add(ticket.get("date") == today.isoformat(), "ticket is from today", str(ticket.get("date")))
    add(str(ticket.get("side", "")).upper() == "BUY", "side is BUY", str(ticket.get("side")))

    entry = to_float(ticket.get("limit_price"))
    stop = to_float(ticket.get("stop_price"))
    shares = to_float(ticket.get("shares"))
    if entry is None or stop is None or shares is None or not (entry > stop > 0):
        add(False, "prices valid", "need limit_price > stop_price > 0 and a share count")
        return results
    add(True, "prices valid", f"entry {entry} stop {stop}")

    if shares <= 0:
        add(False, "size above zero", "SIZE ZERO: account too small for this stop distance")
        return results
    if not limits["allow_fractional"] and abs(shares - round(shares)) > 1e-9:
        add(False, "whole shares", f"{shares} is fractional")
    equity = snapshot["equity_usd"]
    risk_usd = shares * (entry - stop)
    max_risk = equity * limits["risk_per_trade_pct"] / 100.0
    add(risk_usd <= max_risk + 0.005, "risk per trade",
        f"${risk_usd:,.2f} risked vs limit ${max_risk:,.2f} ({limits['risk_per_trade_pct']}% of equity)")
    notional = shares * entry
    max_notional = equity * limits["max_position_pct"] / 100.0
    add(notional <= max_notional + 0.005, "position size",
        f"${notional:,.2f} vs limit ${max_notional:,.2f} ({limits['max_position_pct']}% of equity)")
    if limits.get("max_order_notional_usd"):
        cap = limits["max_order_notional_usd"]
        add(notional <= cap + 0.005, "order notional cap", f"${notional:,.2f} vs ${cap:,.2f}")
    if limits["cash_only"]:
        bp = snapshot["buying_power_usd"]
        add(notional <= bp + 0.005, "buying power (cash only)", f"${notional:,.2f} vs ${bp:,.2f}")

    held = held_symbols(snapshot)
    symbol = str(ticket.get("symbol", "")).upper()
    add(symbol not in held, "no adding to a held position",
        f"{symbol} already held" if symbol in held else "not held")
    open_count = len(set(held) | pending_symbols(desk, today, exclude_id=ticket.get("id")))
    add(open_count + 1 <= limits["max_open_positions"], "max open positions",
        f"{open_count} open or pending + this one vs limit {limits['max_open_positions']}")

    prior = to_float(snapshot.get("prior_equity_usd"))
    if prior and prior > 0:
        loss_pct = (prior - equity) / prior * 100.0
        add(loss_pct < limits["max_daily_loss_pct"], "daily loss limit",
            f"{loss_pct:+.2f}% vs limit {limits['max_daily_loss_pct']}%")
    else:
        add(True, "daily loss limit", "no prior snapshot yet")

    if limits["paper_only"]:
        environment = str(load_broker(desk).get("environment", "")).lower()
        add(environment in ("uat", "paper", "sandbox"), "paper only",
            f"limits.json says paper_only but broker.json environment is {environment or 'not set'!r}")
    return results


def check_passed(results: Iterable[tuple[bool, str, str]]) -> bool:
    return all(ok for ok, _, _ in results)


def format_checks(results: Iterable[tuple[bool, str, str]]) -> str:
    return "\n".join(f"  [{'PASS' if ok else 'FAIL'}] {rule}: {detail}" for ok, rule, detail in results)


# --------------------------------------------------------------------------
# Order gate (called by the PreToolUse hook)
# --------------------------------------------------------------------------

_SYMBOL_KEYS = ("symbol", "ticker")
_SIDE_KEYS = ("side", "action", "order_side")
_QTY_KEYS = ("quantity", "qty", "total_quantity", "shares")
_LIMIT_KEYS = ("limit_price", "price")
_TYPE_KEYS = ("order_type", "type")

_BLOCKED_ASSET_WORDS = ("option", "futures", "crypto", "event", "algo")
_STOCK_ORDER_TOOLS = ("place_stock_order", "place_stock_combo_order", "place_stock_instruction")
_PASS_THROUGH_TOOLS = ("cancel_order", "revoke_instruction", "preview_stock_order")
_COMMUNITY_WRITE_TOOLS = ("create_community_post", "create_community_comment")


def base_tool_name(tool_name: str) -> str:
    """mcp__server__place_stock_order -> place_stock_order"""
    return tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name


_ORDER_TOOL = re.compile(r"^(place|replace)_(stock|equity|option|options|futures|crypto|event|algo)(_|$)")


def is_order_tool(tool_name: str) -> bool:
    """True for broker order tools and community-post tools, nothing else.

    Other servers have unrelated tools that start with "place_"; those are
    not the desk's business and must pass untouched.
    """
    base = base_tool_name(tool_name)
    return bool(_ORDER_TOOL.match(base)) or base in _COMMUNITY_WRITE_TOOLS


def _first(mapping: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def find_legs(tool_input: Any) -> list[dict[str, Any]]:
    """Every dict that looks like an order leg (has a symbol and a side)."""
    legs: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, str):
            text = node.strip()
            if text[:1] in "[{":
                try:
                    walk(json.loads(text), depth + 1)
                except ValueError:
                    pass
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, dict):
            symbol, side = _first(node, _SYMBOL_KEYS), _first(node, _SIDE_KEYS)
            if isinstance(symbol, str) and isinstance(side, str):
                legs.append(node)
            for value in node.values():
                if isinstance(value, (dict, list, str)):
                    walk(value, depth + 1)

    walk(tool_input)
    return legs


def _leg_view(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(_first(leg, _SYMBOL_KEYS)).strip().upper(),
        "side": str(_first(leg, _SIDE_KEYS)).strip().upper(),
        "quantity": to_float(_first(leg, _QTY_KEYS)),
        "order_type": str(_first(leg, _TYPE_KEYS) or "").strip().upper(),
        "limit_price": to_float(_first(leg, _LIMIT_KEYS)),
        "stop_price": to_float(leg.get("stop_price")),
        "time_in_force": str(leg.get("time_in_force") or "").strip().upper(),
        "cash_amount": to_float(leg.get("total_cash_amount")),
        "entrust_type": str(leg.get("entrust_type") or "").strip().upper(),
    }


def _decision(decision: str | None, reason: str) -> dict[str, Any]:
    return {"decision": decision, "reason": reason}


def gate(tool_name: str, tool_input: Any, desk: Path | None, today: date | None = None) -> dict[str, Any]:
    """Decide whether a broker tool call may proceed.

    Returns {"decision": "allow" | "ask" | "deny" | None, "reason": str}.
    None means "not our business": the normal permission flow applies.
    """
    base = base_tool_name(tool_name)
    if base in _PASS_THROUGH_TOOLS or not is_order_tool(tool_name):
        return _decision(None, "")
    if base in _COMMUNITY_WRITE_TOOLS:
        if desk is None:
            return _decision(None, "")
        return _decision("deny", "Desk gate: the trading desk never posts to the community under your account.")
    if desk is None:
        return _decision(
            "ask",
            "No trading desk folder found for this project, so this order is NOT checked "
            "against any desk limit. Approve only if you placed it on purpose.",
        )
    if any(word in base for word in _BLOCKED_ASSET_WORDS):
        return _decision("deny", f"Desk gate: {base} blocked. This desk trades whole stock positions only.")
    if base.startswith("replace_"):
        return _decision("deny", "Desk gate: orders are not modified in place. Cancel it and raise a new ticket.")
    if base not in _STOCK_ORDER_TOOLS:
        return _decision("deny", f"Desk gate: unknown order tool {base!r}; failing closed.")

    today = today or today_ny()
    legs = [_leg_view(leg) for leg in find_legs(tool_input)]
    if not legs:
        return _decision("deny", "Desk gate: could not read symbol and side from this order; failing closed.")
    try:
        limits = load_limits(desk)
    except DeskError as exc:
        return _decision("deny", f"Desk gate: {exc}")

    flag = blocking_flag(desk)
    buys = [leg for leg in legs if leg["side"] == "BUY"]
    sells = [leg for leg in legs if leg["side"] == "SELL"]
    if len(buys) + len(sells) != len(legs):
        return _decision("deny", "Desk gate: only BUY and SELL are allowed (no shorting).")
    for leg in legs:
        if leg["cash_amount"] or leg["entrust_type"] == "AMOUNT":
            return _decision("deny", "Desk gate: cash-amount orders are blocked; size by share count from the ticket.")
        if leg["quantity"] is None or leg["quantity"] <= 0:
            return _decision("deny", f"Desk gate: {leg['symbol']} has no valid quantity.")

    summaries: list[str] = []
    tickets_by_symbol: dict[str, dict[str, Any]] = {}

    if buys:
        if flag:
            return _decision("deny", f"Desk gate: {flag} is active. No new positions until the PM clears it.")
        try:
            snapshot = load_snapshot(desk, today)
        except (DeskError, OSError, ValueError) as exc:
            return _decision("deny", f"Desk gate: {exc}")
        todays = list_tickets(desk, today)
        for leg in buys:
            matches = [t for t in todays if t.get("symbol") == leg["symbol"]]
            approved = [t for t in matches if t.get("status") == "APPROVED"]
            if not approved:
                seen = ", ".join(f"#{t.get('id')} {t.get('status')}" for t in matches) or "none"
                return _decision(
                    "deny",
                    f"Desk gate: no APPROVED ticket today for {leg['symbol']} (tickets: {seen}). "
                    "A BUY needs a ticket that passed risk-check and that the PM approved.",
                )
            ticket = approved[0]
            if leg["order_type"] != "LIMIT":
                return _decision("deny", f"Desk gate: {leg['symbol']} BUY must be a LIMIT order, got {leg['order_type'] or 'none'}.")
            if leg["time_in_force"] and leg["time_in_force"] != "DAY":
                return _decision("deny", f"Desk gate: {leg['symbol']} BUY must be a DAY order.")
            if leg["limit_price"] is None or leg["limit_price"] > ticket["limit_price"] + 1e-6:
                return _decision("deny", f"Desk gate: {leg['symbol']} limit {leg['limit_price']} is above ticket limit {ticket['limit_price']}.")
            if leg["quantity"] > ticket["shares"] + 1e-9:
                return _decision("deny", f"Desk gate: {leg['symbol']} quantity {leg['quantity']} is above ticket size {ticket['shares']}.")
            results = risk_check(desk, ticket, today, limits=limits, snapshot=snapshot)
            if not check_passed(results):
                failed = "; ".join(f"{rule}: {detail}" for ok, rule, detail in results if not ok)
                return _decision("deny", f"Desk gate: ticket #{ticket['id']} fails risk-check now ({failed}).")
            tickets_by_symbol[leg["symbol"]] = ticket
            summaries.append(
                f"ticket #{ticket['id']} BUY {leg['quantity']:g} {leg['symbol']} limit {leg['limit_price']:g} "
                f"(stop {ticket['stop_price']:g}, risk ${ticket['risk_usd']:,.2f})"
            )

    closing_only = not buys
    if sells:
        held: dict[str, float] = {}
        try:
            held = held_symbols(load_snapshot(desk, today))
        except (DeskError, OSError, ValueError):
            held = {}
        live = {
            t["symbol"]: t for t in list_tickets(desk)
            if t.get("status") in ("SENT", "FILLED")
        }
        for leg in sells:
            symbol = leg["symbol"]
            ticket = tickets_by_symbol.get(symbol)
            if ticket is not None:  # protective stop attached to today's entry
                if "STOP" not in leg["order_type"] or leg["stop_price"] is None:
                    return _decision("deny", f"Desk gate: the SELL leg attached to {symbol} must be a stop order.")
                if leg["stop_price"] < ticket["stop_price"] - 0.011:
                    return _decision("deny", f"Desk gate: {symbol} stop {leg['stop_price']} is looser than ticket stop {ticket['stop_price']}.")
                limit_qty = ticket["shares"]
            elif symbol in held:
                limit_qty = held[symbol]
            elif symbol in live:
                # entry sent but not in the snapshot yet: only a protective stop may rest
                if "STOP" not in leg["order_type"] or leg["stop_price"] is None:
                    return _decision(
                        "deny",
                        f"Desk gate: SELL {symbol} blocked. Today's snapshot shows no {symbol} position; "
                        "take a fresh snapshot after the fill. Only a stop order may be placed before that.",
                    )
                limit_qty = live[symbol]["shares"]
            else:
                return _decision("deny", f"Desk gate: SELL {symbol} blocked. The desk holds no {symbol} (no shorting).")
            if leg["quantity"] > limit_qty + 1e-9:
                return _decision("deny", f"Desk gate: SELL {leg['quantity']:g} {symbol} is more than the {limit_qty:g} held.")
            kind = leg["order_type"] or "order"
            price = leg["stop_price"] or leg["limit_price"]
            summaries.append(f"SELL {leg['quantity']:g} {symbol} {kind}" + (f" @ {price:g}" if price else ""))

    text = "; ".join(summaries)
    if closing_only and limits["auto_allow_exits"]:
        return _decision("allow", f"Desk gate PASS (closing only): {text}")
    return _decision("ask", f"Desk gate PASS: {text}. Approve to send to Webull.")


def log_gate(desk: Path | None, tool_name: str, tool_input: Any, result: dict[str, Any]) -> None:
    if desk is None or result.get("decision") is None:
        return
    try:
        legs = [_leg_view(leg) for leg in find_legs(tool_input)]
        line = {
            "at": now_ny().isoformat(timespec="seconds"),
            "tool": base_tool_name(tool_name),
            "legs": [
                {k: leg[k] for k in ("symbol", "side", "quantity", "order_type", "limit_price", "stop_price")}
                for leg in legs
            ],
            "decision": result["decision"],
            "reason": result["reason"],
        }
        path = desk / "journal" / "gate.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass  # logging must never change the decision


def mark_sent(desk: Path, tool_name: str, tool_input: Any, today: date | None = None) -> list[int]:
    """After an order tool ran: move matching APPROVED tickets to SENT."""
    if base_tool_name(tool_name) not in _STOCK_ORDER_TOOLS:
        return []
    today = today or today_ny()
    marked = []
    symbols = {leg["symbol"] for leg in map(_leg_view, find_legs(tool_input)) if leg["side"] == "BUY"}
    for ticket in list_tickets(desk, today):
        if ticket.get("status") == "APPROVED" and ticket.get("symbol") in symbols:
            set_status(desk, ticket, "SENT", "order tool call completed")
            marked.append(ticket["id"])
    return marked


# --------------------------------------------------------------------------
# Protected files (called by the PreToolUse hook on edits)
# --------------------------------------------------------------------------

_PROTECTED_NAMES = ("limits.json", "broker.json", "KILL")
_NAME = (
    r"(?:limits\.json|broker\.json|desklib\.py|desk\.py|order_gate\.py|protect_files\.py|hooks\.json"
    r"|(?<![\w.])KILL(?![\w.]))"
)
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;\n|]")
_BASH_WRITE_PATTERNS = (
    # a file-changing command with a protected file among its arguments
    re.compile(r"\b(?:rm|mv|cp|sed|tee|dd|chmod|truncate|unlink|install|ln|touch)\b[^\n]*" + _NAME),
    # output redirected into a protected file
    re.compile(r">\s*[\"']?[^\s\"']*" + _NAME),
    # an inline script that opens a protected file for writing
    re.compile(_NAME + r"[^\n]*(?:open\(|write_text|\.write\(|json\.dump)"),
    re.compile(r"(?:open\(|write_text|\.write\(|json\.dump)[^\n]*" + _NAME),
)
_BROKER_COMMAND = re.compile(r"desk\.py[\"']?\s+broker\b")


def protected_reason(tool_name: str, tool_input: Any, desk: Path | None, plugin_root: str | None = None) -> str | None:
    """Why a file edit or shell command needs the human, or None if it does not."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        if _BROKER_COMMAND.search(command):
            return "This command changes the desk's broker settings (paper or live). Only the PM decides that."
        for segment in _SEGMENT_SPLIT.split(command):
            if any(pattern.search(segment) for pattern in _BASH_WRITE_PATTERNS):
                return (
                    "This command may change a protected trading-desk file (limits, broker settings, "
                    "the KILL switch or the safety scripts). Only the PM changes those."
                )
        return None
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return None
    try:
        path = Path(str(target)).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if plugin_root:
        try:
            path.relative_to(Path(plugin_root).resolve())
            return "This file belongs to the trading-desk plugin (safety code). Only the PM changes it."
        except ValueError:
            pass
    if desk is not None:
        try:
            relative = path.relative_to(desk)
        except ValueError:
            return None
        if relative.parts and (relative.parts[0] == "tools" or (len(relative.parts) == 1 and relative.name in _PROTECTED_NAMES)):
            return f"desk/{relative} is a protected trading-desk file. Only the PM changes it."
    return None


# --------------------------------------------------------------------------
# Open book: exits due and missing stops
# --------------------------------------------------------------------------

def exits_due(desk: Path, today: date) -> list[dict[str, Any]]:
    due = []
    for ticket in list_tickets(desk):
        if ticket.get("status") != "FILLED":
            continue
        exit_date = ticket.get("exit_due")
        if exit_date and parse_date(exit_date) <= today:
            due.append(ticket)
    return due


def missing_stops(snapshot: dict[str, Any]) -> list[str]:
    covered: dict[str, float] = {}
    for order in snapshot.get("open_orders", []):
        if order.get("side") == "SELL" and "STOP" in str(order.get("order_type", "")):
            covered[order["symbol"]] = covered.get(order["symbol"], 0) + (order.get("quantity") or 0)
    return [
        symbol for symbol, qty in held_symbols(snapshot).items()
        if qty > 0 and covered.get(symbol, 0) + 1e-9 < qty
    ]


def daily_loss_hit(snapshot: dict[str, Any], limits: dict[str, Any]) -> float | None:
    prior = to_float(snapshot.get("prior_equity_usd"))
    if not prior or prior <= 0:
        return None
    loss_pct = (prior - snapshot["equity_usd"]) / prior * 100.0
    return loss_pct if loss_pct >= limits["max_daily_loss_pct"] else None


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

JOURNAL_FIELDS = [
    "date", "ticker", "outcome", "ticket", "entry", "exit", "qty", "pnl_usd", "pnl_r", "note",
]
OUTCOMES = (
    "FILLED", "NOT FILLED", "REJECTED BY ME", "KILLED BY SKEPTIC",
    "FAILED RISK CHECK", "SIZE ZERO", "OFF-PLAN",
)


def journal_path(desk: Path) -> Path:
    return desk / "journal" / "trades.csv"


def append_journal(desk: Path, row: dict[str, Any]) -> None:
    outcome = str(row.get("outcome", "")).upper()
    if outcome not in OUTCOMES:
        raise DeskError(f"outcome must be one of: {', '.join(OUTCOMES)}")
    row = dict(row, outcome=outcome, ticker=str(row.get("ticker", "")).upper())
    path = journal_path(desk)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.is_file() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in JOURNAL_FIELDS})


def read_journal(desk: Path) -> list[dict[str, str]]:
    path = journal_path(desk)
    if not path.is_file():
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def review(desk: Path, backtest_avg_r: float | None = None) -> dict[str, Any]:
    rows = read_journal(desk)

    def stats(selected: list[dict[str, str]]) -> dict[str, Any]:
        rs = [r for r in (to_float(row.get("pnl_r")) for row in selected) if r is not None]
        pnl = [p for p in (to_float(row.get("pnl_usd")) for row in selected) if p is not None]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        names = [row for row in selected if not str(row.get("note", "")).startswith("hypothetical")]
        return {
            "count": len(names) if names else len(selected),
            "closed": len(rs),
            "win_rate": round(len(wins) / len(rs), 4) if rs else None,
            "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
            "avg_win_r": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loss_r": round(sum(losses) / len(losses), 4) if losses else None,
            "total_pnl_usd": round(sum(pnl), 2) if pnl else 0.0,
        }

    taken = [r for r in rows if r.get("outcome") == "FILLED"]
    killed = [r for r in rows if r.get("outcome") == "KILLED BY SKEPTIC"]
    rejected = [r for r in rows if r.get("outcome") == "REJECTED BY ME"]
    result: dict[str, Any] = {
        "taken": stats(taken),
        "killed_by_skeptic_hypothetical": stats(killed),
        "rejected_by_pm_hypothetical": stats(rejected),
        "off_plan": len([r for r in rows if r.get("outcome") == "OFF-PLAN"]),
        "backtest_avg_r": backtest_avg_r,
        "verdict": "TOO EARLY",
    }
    live = result["taken"]
    if backtest_avg_r is None:
        result["verdict"] = "NO BACKTEST TO COMPARE"
    elif live["closed"] >= 20 and live["avg_r"] is not None:
        result["verdict"] = "DECAYING" if live["avg_r"] < backtest_avg_r / 2.0 else "ON TRACK"
    return result
