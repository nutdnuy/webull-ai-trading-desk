"""Tests that keep the backtest honest. Run: python3 -m unittest discover -s tests -v

Never delete, skip or weaken a test in this file. If one fails, fix the code
or report the failure.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import backtest as bt  # noqa: E402

CONFIG = dict(bt.DEFAULT_CONFIG)


def weekdays(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def flat_bars(count=60, price=100.0, start=date(2024, 1, 1)) -> list[dict]:
    """Every bar: open 100, high 101, low 99, close 100 -> ATR = 2.0."""
    return [{"date": d, "open": price, "high": price + 1, "low": price - 1, "close": price}
            for d in weekdays(start, count)]


def signal(bars, index, ticker="TEST"):
    return {"ticker": ticker, "public_date": bars[index]["date"]}


class TradeRules(unittest.TestCase):
    def test_entry_is_the_open_after_the_public_date(self):
        bars = flat_bars()
        trade = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        self.assertEqual(trade["entry_date"], bars[31]["date"].isoformat())
        self.assertGreater(trade["entry_date"], trade["public_date"])

    def test_no_look_ahead_future_bars_cannot_change_entry_or_stop(self):
        bars = flat_bars()
        base = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        changed = [dict(b) for b in bars]
        for bar in changed[32:]:  # rewrite everything after the entry day
            bar.update(open=250.0, high=260.0, low=240.0, close=255.0)
        after = bt.simulate_trade(signal(changed, 30), changed, CONFIG)
        self.assertEqual((after["entry"], after["stop"]), (base["entry"], base["stop"]))

    def test_entry_day_bar_is_not_in_the_atr(self):
        bars = flat_bars()
        bars[31].update(high=150.0, low=99.5)  # a wild entry day must not widen the stop
        trade = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        self.assertAlmostEqual(trade["stop"], 100.0 - 2 * 2.0)

    def test_time_exit_after_five_trading_days(self):
        bars = flat_bars()
        trade = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        self.assertEqual(trade["exit_reason"], "time")
        self.assertEqual(trade["exit_date"], bars[36]["date"].isoformat())

    def test_costs_are_charged_on_both_sides(self):
        bars = flat_bars()
        trade = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        # flat price: the only result is two lots of 0.1% slippage
        self.assertAlmostEqual(trade["return_pct"], (0.999 / 1.001 - 1) * 100, places=3)
        self.assertLess(trade["r"], 0)
        free = bt.simulate_trade(signal(bars, 30), bars, dict(CONFIG, slippage_per_side=0.0))
        self.assertAlmostEqual(free["return_pct"], 0.0)

    def test_stop_is_hit_intraday(self):
        bars = flat_bars()
        bars[33].update(low=95.0)  # stop is 96
        trade = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        self.assertEqual((trade["exit_reason"], trade["exit"]), ("stop", 96.0))
        self.assertAlmostEqual(trade["r"], -1.0, delta=0.06)

    def test_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        bars = flat_bars()
        bars[33].update(open=90.0, high=91.0, low=89.0, close=90.0)
        trade = bt.simulate_trade(signal(bars, 30), bars, CONFIG)
        self.assertEqual((trade["exit_reason"], trade["exit"]), ("stop (gap)", 90.0))
        self.assertLess(trade["r"], -2.0)  # worse than -1R: gaps are not hidden

    def test_signals_that_cannot_be_priced_are_reported_not_dropped(self):
        bars = flat_bars()
        early = signal(bars, 3)
        self.assertIsNone(bt.simulate_trade(early, bars, CONFIG))
        self.assertIn("ATR", early["skip"])
        late = signal(bars, 57)
        self.assertIsNone(bt.simulate_trade(late, bars, CONFIG))
        self.assertIn("past the end", late["skip"])


class HoldoutRules(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.prices = self.root / "prices"
        self.prices.mkdir()
        self.bars = flat_bars(count=600, start=date(2023, 1, 2))
        with open(self.prices / "TEST.csv", "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for bar in self.bars:
                writer.writerow([bar["date"], bar["open"], bar["high"], bar["low"], bar["close"], 1000000])
        self.signals_file = self.root / "signals.csv"
        with open(self.signals_file, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ticker", "public_date"])
            for index in range(40, 590, 25):
                writer.writerow(["TEST", self.bars[index]["date"]])
            writer.writerow(["GONE", self.bars[100]["date"]])  # delisted: no price file

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_and_holdout_never_share_a_signal(self):
        signals = bt.load_signals(self.signals_file)
        build = bt.run(signals, self.prices, CONFIG, "build")
        holdout = bt.run(signals, self.prices, CONFIG, "holdout")
        cutoff = build["holdout_starts"]
        self.assertTrue(build["trades"] and holdout["trades"])
        self.assertTrue(all(t["exit_date"] < cutoff for t in build["trades"]))
        self.assertTrue(all(t["public_date"] >= cutoff for t in holdout["trades"]))
        build_keys = {(t["ticker"], t["public_date"]) for t in build["trades"]}
        holdout_keys = {(t["ticker"], t["public_date"]) for t in holdout["trades"]}
        self.assertFalse(build_keys & holdout_keys)

    def test_build_cannot_see_holdout_prices(self):
        signals = bt.load_signals(self.signals_file)
        before = bt.run(signals, self.prices, CONFIG, "build")
        cutoff = bt.parse_date(before["holdout_starts"])
        with open(self.prices / "TEST.csv", newline="") as fh:
            rows = list(csv.reader(fh))
        with open(self.prices / "TEST.csv", "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(rows[0])
            for row in rows[1:]:
                if bt.parse_date(row[0]) >= cutoff:
                    row[1:5] = ["500", "510", "490", "505"]
                writer.writerow(row)
        after = bt.run(signals, self.prices, CONFIG, "build", as_of=bt.parse_date(before["data_as_of"]))
        self.assertEqual(before["trades"], after["trades"])

    def test_unpriced_signals_are_counted(self):
        result = bt.run(bt.load_signals(self.signals_file), self.prices, CONFIG, "build")
        self.assertGreaterEqual(result["unpriced_signals"], 1)
        self.assertTrue(any(s["ticker"] == "GONE" for s in result["skipped"]))

    def test_holdout_runs_once(self):
        command = [sys.executable, str(HERE / "backtest.py"), "holdout", "--signals", str(self.signals_file),
                   "--prices", str(self.prices), "--out", str(self.root / "results")]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(second.returncode, 2)
        self.assertIn("REFUSED", second.stderr)
        lock = json.loads((self.root / "results" / "holdout_lock.json").read_text())
        self.assertIn(CONFIG["strategy_id"], lock)

    def test_build_can_be_rerun(self):
        command = [sys.executable, str(HERE / "backtest.py"), "build", "--signals", str(self.signals_file),
                   "--prices", str(self.prices), "--out", str(self.root / "results")]
        for _ in range(2):
            self.assertEqual(subprocess.run(command, capture_output=True, text=True).returncode, 0)


class Metrics(unittest.TestCase):
    def test_numbers(self):
        trades = [
            {"ticker": "A", "r": 2.0, "return_pct": 4.0, "exit_date": "2024-01-10", "entry_date": "2024-01-03", "exit_reason": "time"},
            {"ticker": "B", "r": -1.0, "return_pct": -2.0, "exit_date": "2024-01-11", "entry_date": "2024-01-04", "exit_reason": "stop"},
            {"ticker": "C", "r": -1.0, "return_pct": -2.0, "exit_date": "2024-01-12", "entry_date": "2024-01-05", "exit_reason": "stop"},
        ]
        m = bt.metrics(trades, CONFIG)
        self.assertEqual(m["trades"], 3)
        self.assertAlmostEqual(m["win_rate"], 0.3333, places=4)
        self.assertAlmostEqual(m["avg_r"], 0.0)
        self.assertAlmostEqual(m["total_return_pct"], (1.02 * 0.99 * 0.99 - 1) * 100, places=3)
        self.assertAlmostEqual(m["max_drawdown_pct"], (1 - 0.99 * 0.99) * 100, places=3)
        self.assertEqual(m["stopped_out"], 2)

    def test_months_before_handles_month_ends(self):
        self.assertEqual(bt.months_before(date(2024, 3, 31), 1), date(2024, 2, 29))
        self.assertEqual(bt.months_before(date(2025, 1, 15), 12), date(2024, 1, 15))


if __name__ == "__main__":
    unittest.main()
