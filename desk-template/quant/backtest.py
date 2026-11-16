#!/usr/bin/env python3
"""Event backtest for the desk's starter strategy. Standard library only.

Rule being tested: after a signal becomes public, buy at the next session's
open, place a stop at entry - N x ATR(14), and sell at the close of the H-th
trading day after entry (or at the stop, whichever comes first).

    python3 backtest.py build   --signals signals.csv --prices ../data/bars
    python3 backtest.py holdout --signals signals.csv --prices ../data/bars

Guards built into the code, so a result cannot be made to look better by accident:
  * no look-ahead: entry is strictly after the public date, and ATR uses only
    bars before the entry day;
  * costs: slippage is charged on both sides;
  * holdout: the last 12 months are invisible to `build`, and `holdout` runs
    ONCE per strategy id. A second run is refused.

signals.csv columns: ticker, public_date (YYYY-MM-DD, the day the filing became
public). Extra columns are kept in the trade list.
Price files: <prices>/<TICKER>.csv with date,open,high,low,close,volume.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

DEFAULT_CONFIG = {
    "strategy_id": "insider-buy-5d",
    "hold_days": 5,
    "atr_period": 14,
    "stop_atr_mult": 2.0,
    "slippage_per_side": 0.001,
    "risk_per_trade_pct": 1.0,
    "holdout_months": 12,
    "benchmark": "SPY",
}


def parse_date(text: str) -> date:
    return datetime.strptime(str(text).strip()[:10], "%Y-%m-%d").date()


def months_before(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def load_prices(path: Path) -> list[dict]:
    bars = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): v for k, v in row.items() if k}
            bars.append({
                "date": parse_date(row["date"]),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
            })
    bars.sort(key=lambda b: b["date"])
    return bars


def load_signals(path: Path) -> list[dict]:
    signals = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            row["ticker"] = row["ticker"].upper()
            row["public_date"] = parse_date(row["public_date"])
            signals.append(row)
    signals.sort(key=lambda s: (s["public_date"], s["ticker"]))
    return signals


def wilder_atr(bars: list[dict], period: int) -> float | None:
    """ATR over the bars given. Pass only bars the trader could already see."""
    if len(bars) < period + 1:
        return None
    ranges = [
        max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"]))
        for prev, cur in zip(bars, bars[1:])
    ]
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def simulate_trade(signal: dict, bars: list[dict], config: dict) -> dict | None:
    """One trade, or None with signal['skip'] set to the reason."""
    entry_index = next((i for i, bar in enumerate(bars) if bar["date"] > signal["public_date"]), None)
    if entry_index is None:
        signal["skip"] = "no price bar after the public date"
        return None
    atr_value = wilder_atr(bars[:entry_index], config["atr_period"])  # bars before entry only
    if atr_value is None or atr_value <= 0:
        signal["skip"] = "not enough history before entry for ATR"
        return None
    exit_index = entry_index + config["hold_days"]
    if exit_index >= len(bars):
        signal["skip"] = "holding period runs past the end of the data"
        return None

    entry_bar = bars[entry_index]
    entry = entry_bar["open"]
    stop = entry - config["stop_atr_mult"] * atr_value
    if stop <= 0:
        signal["skip"] = "stop would be at or below zero"
        return None

    exit_price, exit_date, reason = None, None, None
    for index in range(entry_index, exit_index + 1):
        bar = bars[index]
        if index > entry_index and bar["open"] <= stop:  # gapped through the stop
            exit_price, exit_date, reason = bar["open"], bar["date"], "stop (gap)"
            break
        if bar["low"] <= stop:
            exit_price, exit_date, reason = stop, bar["date"], "stop"
            break
    if exit_price is None:
        exit_price, exit_date, reason = bars[exit_index]["close"], bars[exit_index]["date"], "time"

    slip = config["slippage_per_side"]
    entry_net = entry * (1 + slip)
    exit_net = exit_price * (1 - slip)
    return {
        "ticker": signal["ticker"],
        "public_date": signal["public_date"].isoformat(),
        "entry_date": entry_bar["date"].isoformat(),
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "exit_date": exit_date.isoformat(),
        "exit": round(exit_price, 4),
        "exit_reason": reason,
        "return_pct": round((exit_net / entry_net - 1) * 100, 4),
        "r": round((exit_net - entry_net) / (entry - stop), 4),
    }


def split_signals(signals: list[dict], cutoff: date, mode: str) -> list[dict]:
    """build sees only signals before the cutoff; holdout only those on or after it."""
    if mode == "build":
        return [s for s in signals if s["public_date"] < cutoff]
    return [s for s in signals if s["public_date"] >= cutoff]


def metrics(trades: list[dict], config: dict) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    wins = [t for t in trades if t["r"] > 0]
    losses = [t for t in trades if t["r"] <= 0]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for trade in sorted(trades, key=lambda t: (t["exit_date"], t["ticker"])):
        equity *= 1 + config["risk_per_trade_pct"] / 100.0 * trade["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    def avg(values):
        return round(sum(values) / len(values), 4) if values else None

    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "avg_r": avg(rs),
        "avg_win_r": avg([t["r"] for t in wins]),
        "avg_loss_r": avg([t["r"] for t in losses]),
        "avg_win_pct": avg([t["return_pct"] for t in wins]),
        "avg_loss_pct": avg([t["return_pct"] for t in losses]),
        "total_return_pct": round((equity - 1) * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "stopped_out": len([t for t in trades if t["exit_reason"].startswith("stop")]),
        "first_entry": min(t["entry_date"] for t in trades),
        "last_exit": max(t["exit_date"] for t in trades),
    }


def benchmark_return(prices_dir: Path, symbol: str, start: str, end: str) -> float | None:
    path = prices_dir / f"{symbol}.csv"
    if not path.is_file():
        return None
    bars = [b for b in load_prices(path) if start <= b["date"].isoformat() <= end]
    if len(bars) < 2:
        return None
    return round((bars[-1]["close"] / bars[0]["open"] - 1) * 100, 4)


def run(signals: list[dict], prices_dir: Path, config: dict, mode: str, as_of: date | None = None) -> dict:
    price_cache: dict[str, list[dict] | None] = {}

    def prices(ticker: str):
        if ticker not in price_cache:
            path = prices_dir / f"{ticker}.csv"
            price_cache[ticker] = load_prices(path) if path.is_file() else None
        return price_cache[ticker]

    if as_of is None:
        last_dates = [bars[-1]["date"] for bars in (prices(s["ticker"]) for s in signals) if bars]
        if not last_dates:
            raise SystemExit("no price files found for any signal")
        as_of = max(last_dates)
    cutoff = months_before(as_of, config["holdout_months"])
    selected = split_signals(signals, cutoff, mode)

    trades, skipped = [], []
    for signal in selected:
        bars = prices(signal["ticker"])
        if bars is None:
            skipped.append({"ticker": signal["ticker"], "public_date": signal["public_date"].isoformat(),
                            "reason": "no price file (delisted or missing)"})
            continue
        if mode == "build":  # build may not touch a single holdout bar
            bars = [b for b in bars if b["date"] < cutoff]
        trade = simulate_trade(signal, bars, config)
        if trade is None:
            skipped.append({"ticker": signal["ticker"], "public_date": signal["public_date"].isoformat(),
                            "reason": signal.get("skip", "unknown")})
        else:
            trades.append(trade)

    result = {
        "mode": mode,
        "strategy_id": config["strategy_id"],
        "config": config,
        "data_as_of": as_of.isoformat(),
        "holdout_starts": cutoff.isoformat(),
        "signals_in_scope": len(selected),
        "unpriced_signals": len(skipped),
        "metrics": metrics(trades, config),
        "trades": trades,
        "skipped": skipped,
        "note": "Price files for delisted stocks are usually missing, so treat this as a best case.",
    }
    if trades:
        m = result["metrics"]
        result["benchmark_return_pct"] = benchmark_return(
            prices_dir, config["benchmark"], m["first_entry"], m["last_exit"])
    return result


def git_commit(folder: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=folder,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "no-commit"
    except (OSError, subprocess.SubprocessError):
        return "no-git"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("build", "holdout"))
    parser.add_argument("--signals", required=True)
    parser.add_argument("--prices", required=True)
    parser.add_argument("--config", help="JSON file overriding the default config")
    parser.add_argument("--as-of", help="last data date (default: newest bar found)")
    parser.add_argument("--out", default=None, help="results folder (default: results/ beside this script)")
    args = parser.parse_args(argv)

    config = dict(DEFAULT_CONFIG)
    if args.config:
        config.update(json.loads(Path(args.config).read_text(encoding="utf-8")))
    here = Path(__file__).resolve().parent
    out_dir = Path(args.out) if args.out else here / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / "holdout_lock.json"
    locks = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {}

    if args.mode == "holdout" and config["strategy_id"] in locks:
        used = locks[config["strategy_id"]]
        print(f"REFUSED: the holdout for '{config['strategy_id']}' was already run on {used['run_at']} "
              f"(commit {used['commit']}). A holdout is used once. A new idea needs a new strategy_id "
              "and a fresh, logged reason.", file=sys.stderr)
        return 2

    signals = load_signals(Path(args.signals))
    result = run(signals, Path(args.prices), config, args.mode,
                 parse_date(args.as_of) if args.as_of else None)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    result.update({"run_at": datetime.now().isoformat(timespec="seconds"),
                   "commit": git_commit(here), "config_hash": config_hash})

    if args.mode == "holdout":
        locks[config["strategy_id"]] = {"run_at": result["run_at"], "commit": result["commit"],
                                        "config_hash": config_hash}
        lock_path.write_text(json.dumps(locks, indent=2) + "\n", encoding="utf-8")

    out_file = out_dir / f"{args.mode}.json"
    out_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    summary = {k: result[k] for k in ("mode", "strategy_id", "data_as_of", "holdout_starts",
                                      "signals_in_scope", "unpriced_signals", "metrics")}
    summary["benchmark_return_pct"] = result.get("benchmark_return_pct")
    print(json.dumps(summary, indent=2))
    print(f"saved: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
