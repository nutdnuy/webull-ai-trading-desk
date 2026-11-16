"""Tests for the desk's safety layer. Run: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import desklib as dl  # noqa: E402

TODAY = date(2026, 11, 16)  # a Monday
SERVER = "mcp__webull__"


def make_bars(n=40, start=100.0):
    bars, day, price = [], date(2026, 9, 1), start
    while len(bars) < n:
        if dl.is_trading_day(day):
            bars.append({"date": day, "open": price, "high": price + 1.5, "low": price - 1.5,
                         "close": price + 0.5, "volume": 1_000_000})
            price += 0.25
        day = date.fromordinal(day.toordinal() + 1)
    return bars


class DeskCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.desk = Path(self._tmp.name).resolve() / "desk"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "desk.py"), "init", str(self.desk)],
                       check=True, capture_output=True)
        dl.write_json(self.desk / "broker.json", {"mode": "local", "environment": "uat"})
        self.snapshot(100_000, 100_000)

    def tearDown(self):
        self._tmp.cleanup()

    def snapshot(self, equity, buying_power, positions=(), open_orders=(), prior=None):
        path = dl.snapshot_path(self.desk, TODAY)
        if path.exists():
            path.unlink()
        snap = dl.save_snapshot(self.desk, {
            "equity_usd": equity, "buying_power_usd": buying_power,
            "positions": list(positions), "open_orders": list(open_orders),
        }, TODAY)
        if prior is not None:
            snap["prior_equity_usd"] = prior
            dl.write_json(path, snap)
        return snap

    def limits(self, **changes):
        limits = dl.read_json(self.desk / "limits.json")
        limits.update(changes)
        dl.write_json(self.desk / "limits.json", limits)
        return dl.load_limits(self.desk)

    def ticket(self, symbol="AAPL", last_close=100.0, atr=2.0, status="APPROVED"):
        limits = dl.load_limits(self.desk)
        ticket = dl.build_ticket(self.desk, symbol, last_close, atr,
                                 dl.load_snapshot(self.desk, TODAY), limits, TODAY)
        dl.set_status(self.desk, ticket, status)
        return ticket

    def buy(self, ticket, **over):
        order = {"symbol": ticket["symbol"], "side": "BUY", "order_type": "LIMIT",
                 "time_in_force": "DAY", "quantity": ticket["shares"],
                 "limit_price": ticket["limit_price"]}
        order.update(over)
        return order

    def gate(self, tool, tool_input):
        return dl.gate(SERVER + tool, tool_input, self.desk, TODAY)


class CalendarTests(unittest.TestCase):
    def test_holidays_2026(self):
        holidays = dl.nyse_holidays(2026)
        for day in (date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                    date(2026, 11, 26), date(2026, 12, 25)):
            self.assertIn(day, holidays)
        self.assertEqual(len(holidays), 10)

    def test_trading_day_math_skips_weekend_and_holiday(self):
        self.assertFalse(dl.is_trading_day(date(2026, 11, 21)))
        # Mon 16 Nov + 5 trading days = Mon 23 Nov
        self.assertEqual(dl.add_trading_days(date(2026, 11, 16), 5), date(2026, 11, 23))
        # Mon 23 Nov + 5 skips Thanksgiving (26 Nov) -> Tue 1 Dec
        self.assertEqual(dl.add_trading_days(date(2026, 11, 23), 5), date(2026, 12, 1))

    def test_saturday_new_year_is_not_observed(self):
        self.assertNotIn(date(2021, 12, 31), dl.nyse_holidays(2021))
        self.assertNotIn(date(2022, 1, 1), dl.nyse_holidays(2022))


class BarsTests(unittest.TestCase):
    def test_atr_of_constant_range(self):
        self.assertAlmostEqual(dl.atr(make_bars()), 3.0, places=6)

    def test_atr_needs_enough_bars(self):
        with self.assertRaises(dl.DeskError):
            dl.atr(make_bars(14))

    def test_swapped_columns_are_rejected(self):
        bars = make_bars(20)
        bars[5]["high"], bars[5]["low"] = bars[5]["low"], bars[5]["high"]
        with self.assertRaises(dl.DeskError):
            dl.validate_bars(bars)

    def test_dollar_volume(self):
        bars = make_bars(20)
        expected = sum(b["close"] * b["volume"] for b in bars) / 20
        self.assertAlmostEqual(dl.average_dollar_volume(bars), expected)


class SizingTests(DeskCase):
    def test_size_formula(self):
        # entry 100.50, stop 96.50, risk/share 4.00; 1% of 100k = 1000 -> 250 shares,
        # but 10% position cap = 10,000 / 100.50 -> 99 shares
        ticket = self.ticket(status="PROPOSED")
        self.assertEqual(ticket["limit_price"], 100.5)
        self.assertEqual(ticket["stop_price"], 96.5)
        self.assertEqual(ticket["shares"], 99)
        self.assertEqual(ticket["sized_by"], "max_position_pct")

    def test_risk_formula_binds_when_stop_is_wide(self):
        self.limits(max_position_pct=100.0)
        ticket = self.ticket(last_close=100.0, atr=10.0, status="PROPOSED")
        # entry 100.50, stop 80.50, risk/share 20 -> 1000 / 20 = 50 shares
        self.assertEqual(ticket["shares"], 50)
        self.assertEqual(ticket["sized_by"], "risk")
        self.assertAlmostEqual(ticket["risk_usd"], 1000.0)

    def test_rounds_down(self):
        self.limits(max_position_pct=100.0)
        ticket = self.ticket(last_close=100.0, atr=3.3, status="PROPOSED")
        risk_per_share = ticket["limit_price"] - ticket["stop_price"]
        self.assertEqual(ticket["shares"], int(1000 / risk_per_share))
        self.assertLessEqual(ticket["shares"] * risk_per_share, 1000.0)

    def test_size_zero_for_small_account(self):
        self.snapshot(300, 300)
        ticket = self.ticket(last_close=500.0, atr=10.0, status="PROPOSED")
        self.assertEqual(ticket["shares"], 0)
        results = dl.risk_check(self.desk, ticket, TODAY)
        self.assertFalse(dl.check_passed(results))
        self.assertTrue(any("SIZE ZERO" in detail for _, _, detail in results))

    def test_cash_account_caps_by_buying_power(self):
        self.snapshot(100_000, 2_000)
        ticket = self.ticket(status="PROPOSED")
        self.assertEqual(ticket["shares"], 19)  # 2000 / 100.50
        self.assertEqual(ticket["sized_by"], "buying_power")

    def test_exit_due_is_five_trading_days(self):
        self.assertEqual(self.ticket(status="PROPOSED")["exit_due"], "2026-11-23")


class RiskCheckTests(DeskCase):
    def failed(self, ticket):
        return [rule for ok, rule, _ in dl.risk_check(self.desk, ticket, TODAY) if not ok]

    def test_clean_ticket_passes(self):
        self.assertEqual(self.failed(self.ticket(status="CHECKED")), [])

    def test_oversized_risk_fails(self):
        ticket = self.ticket(status="CHECKED")
        ticket["shares"] = 400  # 400 x 4.00 = 1600 > 1000
        self.assertIn("risk per trade", self.failed(ticket))

    def test_position_cap_fails(self):
        ticket = self.ticket(status="CHECKED")
        ticket["shares"] = 150  # 15,075 > 10,000
        self.assertIn("position size", self.failed(ticket))

    def test_buying_power_fails(self):
        ticket = self.ticket(status="CHECKED")
        self.snapshot(100_000, 5_000)
        self.assertIn("buying power (cash only)", self.failed(ticket))

    def test_max_open_positions_fails(self):
        self.snapshot(100_000, 100_000, positions=[
            {"symbol": s, "quantity": 1} for s in ("A", "B", "C", "D", "E")])
        self.assertIn("max open positions", self.failed(self.ticket(status="CHECKED")))

    def test_pending_tickets_count_as_open(self):
        self.limits(max_open_positions=1)
        self.ticket("MSFT", status="APPROVED")
        self.assertIn("max open positions", self.failed(self.ticket("AAPL", status="CHECKED")))

    def test_no_adding_to_a_position(self):
        self.snapshot(100_000, 100_000, positions=[{"symbol": "AAPL", "quantity": 5}])
        self.assertIn("no adding to a held position", self.failed(self.ticket(status="CHECKED")))

    def test_daily_loss_limit_fails(self):
        ticket = self.ticket(status="CHECKED")
        self.snapshot(96_000, 96_000, prior=100_000)  # -4% vs 3% limit
        self.assertIn("daily loss limit", self.failed(ticket))

    def test_stale_snapshot_fails(self):
        ticket = self.ticket(status="CHECKED")
        dl.snapshot_path(self.desk, TODAY).unlink()
        self.assertIn("fresh account snapshot", self.failed(ticket))

    def test_kill_and_pause_fail(self):
        ticket = self.ticket(status="CHECKED")
        for flag in ("KILL", "PAUSE"):
            (self.desk / flag).write_text("x")
            self.assertIn("no KILL/PAUSE", self.failed(ticket))
            (self.desk / flag).unlink()

    def test_paper_only_blocks_a_live_broker(self):
        ticket = self.ticket(status="CHECKED")
        dl.write_json(self.desk / "broker.json", {"mode": "cloud", "environment": "prod"})
        self.assertIn("paper only", self.failed(ticket))
        self.limits(paper_only=False)
        self.assertEqual(self.failed(ticket), [])

    def test_bad_limits_file_fails_closed(self):
        ticket = self.ticket(status="CHECKED")
        raw = dl.read_json(self.desk / "limits.json")
        raw["risk_per_trade_pct"] = 50  # an absurd limit must not be obeyed
        dl.write_json(self.desk / "limits.json", raw)
        self.assertIn("limits.json valid", self.failed(ticket))


class GateTests(DeskCase):
    def test_read_tools_are_not_touched(self):
        self.assertIsNone(self.gate("get_account_balance", {})["decision"])
        self.assertIsNone(self.gate("cancel_order", {"client_order_id": "x"})["decision"])
        self.assertIsNone(self.gate("preview_stock_order", {"symbol": "AAPL", "side": "BUY"})["decision"])

    def test_buy_without_ticket_is_denied(self):
        result = self.gate("place_stock_order", {"symbol": "AAPL", "side": "BUY", "order_type": "LIMIT",
                                                 "quantity": 1, "limit_price": 100})
        self.assertEqual(result["decision"], "deny")

    def test_buy_with_only_checked_ticket_is_denied(self):
        ticket = self.ticket(status="CHECKED")
        self.assertEqual(self.gate("place_stock_order", self.buy(ticket))["decision"], "deny")

    def test_approved_buy_asks_the_human(self):
        ticket = self.ticket()
        result = self.gate("place_stock_order", self.buy(ticket))
        self.assertEqual(result["decision"], "ask")
        self.assertIn("ticket #1", result["reason"])

    def test_cloud_instruction_tool_is_gated_the_same(self):
        ticket = self.ticket()
        self.assertEqual(self.gate("place_stock_instruction", self.buy(ticket))["decision"], "ask")
        self.assertEqual(self.gate("place_stock_instruction", self.buy(ticket, quantity=500))["decision"], "deny")

    def test_bigger_or_pricier_or_market_orders_are_denied(self):
        ticket = self.ticket()
        for change in ({"quantity": ticket["shares"] + 1}, {"limit_price": ticket["limit_price"] + 0.01},
                       {"order_type": "MARKET", "limit_price": None}, {"time_in_force": "GTC"},
                       {"side": "SHORT"}, {"quantity": None}, {"total_cash_amount": 5000},
                       {"symbol": "TSLA"}):
            with self.subTest(change=change):
                self.assertEqual(self.gate("place_stock_order", self.buy(ticket, **change))["decision"], "deny")

    def test_smaller_and_cheaper_is_fine(self):
        ticket = self.ticket()
        order = self.buy(ticket, quantity=ticket["shares"] - 1, limit_price=ticket["limit_price"] - 1)
        self.assertEqual(self.gate("place_stock_order", order)["decision"], "ask")

    def test_kill_blocks_buys(self):
        ticket = self.ticket()
        (self.desk / "KILL").write_text("x")
        result = self.gate("place_stock_order", self.buy(ticket))
        self.assertEqual(result["decision"], "deny")
        self.assertIn("KILL", result["reason"])

    def test_pause_blocks_buys(self):
        ticket = self.ticket()
        (self.desk / "PAUSE").write_text("x")
        self.assertEqual(self.gate("place_stock_order", self.buy(ticket))["decision"], "deny")

    def test_ticket_that_no_longer_passes_is_denied(self):
        ticket = self.ticket()
        self.snapshot(100_000, 500)  # buying power collapsed after approval
        result = self.gate("place_stock_order", self.buy(ticket))
        self.assertEqual(result["decision"], "deny")
        self.assertIn("buying power", result["reason"])

    def test_yesterdays_ticket_is_denied(self):
        ticket = self.ticket()
        tomorrow = date(2026, 11, 17)
        dl.save_snapshot(self.desk, {"equity_usd": 100_000, "buying_power_usd": 100_000}, tomorrow)
        self.assertEqual(dl.gate(SERVER + "place_stock_order", self.buy(ticket), self.desk, tomorrow)["decision"], "deny")

    def test_sent_ticket_cannot_be_sent_twice(self):
        ticket = self.ticket()
        order = self.buy(ticket)
        self.assertEqual(self.gate("place_stock_order", order)["decision"], "ask")
        self.assertEqual(dl.mark_sent(self.desk, SERVER + "place_stock_order", order, TODAY), [1])
        self.assertEqual(self.gate("place_stock_order", order)["decision"], "deny")

    def test_combo_entry_with_stop(self):
        ticket = self.ticket()
        entry = self.buy(ticket, combo_type="MASTER")
        stop = {"symbol": "AAPL", "side": "SELL", "order_type": "STOP_LOSS", "quantity": ticket["shares"],
                "stop_price": ticket["stop_price"], "time_in_force": "GTC", "combo_type": "STOP_LOSS"}
        ok = self.gate("place_stock_combo_order", {"account_id": "x", "orders": [entry, stop]})
        self.assertEqual(ok["decision"], "ask")
        loose = dict(stop, stop_price=ticket["stop_price"] - 5)
        self.assertEqual(self.gate("place_stock_combo_order", {"orders": [entry, loose]})["decision"], "deny")
        not_a_stop = dict(stop, order_type="LIMIT", limit_price=200, stop_price=None)
        self.assertEqual(self.gate("place_stock_combo_order", {"orders": [entry, not_a_stop]})["decision"], "deny")

    def test_combo_passed_as_json_string(self):
        ticket = self.ticket()
        payload = {"orders": json.dumps([self.buy(ticket, quantity=9999)])}
        self.assertEqual(self.gate("place_stock_combo_order", payload)["decision"], "deny")

    def test_sell_needs_a_position(self):
        sell = {"symbol": "AAPL", "side": "SELL", "order_type": "MARKET", "quantity": 5}
        self.assertEqual(self.gate("place_stock_order", sell)["decision"], "deny")  # would be a short
        self.snapshot(100_000, 100_000, positions=[{"symbol": "AAPL", "quantity": 5}])
        self.assertEqual(self.gate("place_stock_order", sell)["decision"], "ask")
        self.assertEqual(self.gate("place_stock_order", dict(sell, quantity=6))["decision"], "deny")

    def test_sell_is_allowed_under_kill(self):
        self.snapshot(100_000, 100_000, positions=[{"symbol": "AAPL", "quantity": 5}])
        (self.desk / "KILL").write_text("x")
        sell = {"symbol": "AAPL", "side": "SELL", "order_type": "MARKET", "quantity": 5}
        self.assertEqual(self.gate("place_stock_order", sell)["decision"], "ask")

    def test_stop_after_fill_for_a_live_ticket(self):
        ticket = self.ticket(status="SENT")
        stop = {"symbol": "AAPL", "side": "SELL", "order_type": "STOP_LOSS",
                "quantity": ticket["shares"], "stop_price": ticket["stop_price"], "time_in_force": "GTC"}
        self.assertEqual(self.gate("place_stock_order", stop)["decision"], "ask")
        # a plain sell before the position shows in the snapshot could open a short
        market = {"symbol": "AAPL", "side": "SELL", "order_type": "MARKET", "quantity": ticket["shares"]}
        self.assertEqual(self.gate("place_stock_order", market)["decision"], "deny")

    def test_auto_allow_exits_is_opt_in(self):
        self.snapshot(100_000, 100_000, positions=[{"symbol": "AAPL", "quantity": 5}])
        sell = {"symbol": "AAPL", "side": "SELL", "order_type": "MARKET", "quantity": 5}
        self.limits(auto_allow_exits=True)
        self.assertEqual(self.gate("place_stock_order", sell)["decision"], "allow")
        ticket = self.ticket("MSFT")
        self.assertEqual(self.gate("place_stock_order", self.buy(ticket))["decision"], "ask")  # buys always ask

    def test_other_asset_classes_and_edits_are_denied(self):
        for tool in ("place_option_single_order", "place_option_strategy_instruction", "place_futures_order",
                     "place_crypto_order", "place_event_order", "place_algo_order", "replace_stock_order",
                     "place_stock_bracket_order", "create_community_post", "create_community_comment"):
            with self.subTest(tool=tool):
                self.assertEqual(self.gate(tool, {"symbol": "AAPL", "side": "BUY", "quantity": 1})["decision"], "deny")

    def test_unrelated_tools_from_other_servers_are_not_touched(self):
        for tool in ("place_map_object", "place_marker", "replace_text", "create_post"):
            with self.subTest(tool=tool):
                self.assertFalse(dl.is_order_tool("mcp__other__" + tool))
                self.assertIsNone(dl.gate("mcp__other__" + tool, {"symbol": "X", "side": "BUY"}, self.desk, TODAY)["decision"])

    def test_hook_matcher_agrees_with_the_gate(self):
        import re
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        matcher = re.compile(hooks["hooks"]["PreToolUse"][0]["matcher"])
        for tool in ("place_stock_order", "place_stock_combo_order", "place_stock_instruction",
                     "place_option_single_order", "place_options_order", "place_futures_order",
                     "place_crypto_instruction", "place_event_order", "place_algo_order",
                     "replace_stock_order", "replace_option_order", "create_community_post"):
            with self.subTest(tool=tool):
                self.assertTrue(matcher.search("mcp__webull__" + tool))
                self.assertTrue(dl.is_order_tool("mcp__webull__" + tool))
        for tool in ("place_map_object", "get_stock_bars", "cancel_order", "preview_stock_order"):
            with self.subTest(tool=tool):
                self.assertFalse(matcher.search("mcp__x__" + tool))

    def test_unreadable_order_fails_closed(self):
        self.assertEqual(self.gate("place_stock_order", {"payload": "???"})["decision"], "deny")

    def test_no_desk_asks_with_a_warning(self):
        for tool in ("place_stock_order", "place_option_single_order"):
            result = dl.gate(SERVER + tool, {"symbol": "AAPL", "side": "BUY"}, None, TODAY)
            self.assertEqual(result["decision"], "ask")
            self.assertIn("NOT checked", result["reason"])
        self.assertIsNone(dl.gate(SERVER + "create_community_post", {}, None, TODAY)["decision"])

    def test_broken_limits_file_denies(self):
        ticket = self.ticket()
        (self.desk / "limits.json").write_text("{not json")
        self.assertEqual(self.gate("place_stock_order", self.buy(ticket))["decision"], "deny")


class HookScriptTests(DeskCase):
    """Run the real hook the way Claude Code does: JSON on stdin, decision out."""

    def run_hook(self, script, event):
        env = dict(os.environ, TRADING_DESK_DIR=str(self.desk), CLAUDE_PLUGIN_ROOT=str(ROOT))
        env.pop("CLAUDE_PROJECT_DIR", None)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script)], input=json.dumps(event),
                              capture_output=True, text=True, env=env, cwd=str(self.desk.parent))

    def order_event(self, tool_input, mode="default", event="PreToolUse"):
        return {"hook_event_name": event, "tool_name": SERVER + "place_stock_order",
                "tool_input": tool_input, "cwd": str(self.desk.parent), "permission_mode": mode}

    def test_deny_exits_2(self):
        # the hook uses the real date, so any BUY here has no ticket for "today"
        out = self.run_hook("order_gate.py", self.order_event(
            {"symbol": "AAPL", "side": "BUY", "order_type": "LIMIT", "quantity": 1, "limit_price": 1}))
        self.assertEqual(out.returncode, 2)
        self.assertIn("Desk gate", out.stderr)
        log = (self.desk / "journal" / "gate.log").read_text()
        self.assertIn('"decision": "deny"', log)

    def test_read_tool_is_silent(self):
        event = self.order_event({})
        event["tool_name"] = SERVER + "get_stock_bars"
        out = self.run_hook("order_gate.py", event)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, "", ""))

    def test_garbage_stdin_does_not_crash(self):
        env = dict(os.environ, TRADING_DESK_DIR=str(self.desk))
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "order_gate.py")], input="not json",
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0)

    def test_protect_files_asks_for_limits_edit(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "permission_mode": "default",
                 "tool_input": {"file_path": str(self.desk / "limits.json")}, "cwd": str(self.desk.parent)}
        out = self.run_hook("protect_files.py", event)
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        event["permission_mode"] = "bypassPermissions"
        self.assertEqual(self.run_hook("protect_files.py", event).returncode, 2)

    def test_protect_files_ignores_ordinary_work(self):
        for tool, tool_input in (
            ("Write", {"file_path": str(self.desk / "inbox" / "2026-11-16-scout.md")}),
            ("Edit", {"file_path": str(self.desk / "lessons.md")}),
            ("Bash", {"command": "python3 desk/tools/desk.py status > /tmp/out.txt"}),
            ("Bash", {"command": "cat desk/limits.json && python3 desk/tools/desk.py tickets"}),
            ("Bash", {"command": "cat > snap.json <<'EOF'\n{}\nEOF\npython3 desk/tools/desk.py snapshot snap.json"}),
            ("Bash", {"command": "python3 desk/tools/desk.py kill"}),
            ("Bash", {"command": "rm desk/PAUSE"}),
        ):
            with self.subTest(tool_input=tool_input):
                event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                         "permission_mode": "default", "cwd": str(self.desk.parent)}
                out = self.run_hook("protect_files.py", event)
                self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_protect_files_catches_shell_tampering(self):
        for command in (
            "rm desk/KILL",
            "echo '{}' > desk/limits.json",
            "sed -i '' 's/true/false/' desk/limits.json",
            "python3 -c \"import json; json.dump({}, open('desk/broker.json','w'))\"",
            "cp /tmp/x.py desk/tools/desklib.py",
            "python3 desk/tools/desk.py broker --environment uat",
            "ls && mv desk/KILL /tmp/",
        ):
            with self.subTest(command=command):
                event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": command}, "permission_mode": "default",
                         "cwd": str(self.desk.parent)}
                out = self.run_hook("protect_files.py", event)
                self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")


class CliTests(DeskCase):
    def cli(self, *args, stdin=None):
        env = dict(os.environ, DESK_TODAY=TODAY.isoformat())
        env.pop("TRADING_DESK_DIR", None)
        return subprocess.run([sys.executable, str(self.desk / "tools" / "desk.py"), *args],
                              input=stdin, capture_output=True, text=True, env=env)

    def test_full_ticket_life(self):
        out = self.cli("snapshot", "-", stdin=json.dumps({"equity_usd": 50_000, "buying_power_usd": 50_000}))
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.cli("ticket", "nvda", "--last-close", "100", "--atr", "2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("RESULT: PASS", out.stdout)
        self.assertEqual(self.cli("fill", "1", "--price", "100").returncode, 2)  # not approved yet
        self.assertEqual(self.cli("approve", "1").returncode, 0)
        self.assertEqual(dl.get_ticket(self.desk, TODAY, 1)["status"], "APPROVED")
        self.assertEqual(self.cli("sent", "1").returncode, 0)
        self.assertEqual(self.cli("fill", "1", "--price", "100.40").returncode, 0)
        self.assertIn("No exits due", self.cli("exits-due").stdout)
        env_day = dict(os.environ, DESK_TODAY="2026-11-23")
        due = subprocess.run([sys.executable, str(self.desk / "tools" / "desk.py"), "exits-due"],
                             capture_output=True, text=True, env=env_day)
        self.assertIn("EXIT DUE: NVDA", due.stdout)
        self.assertEqual(self.cli("close", "1", "--price", "104.40").returncode, 0)
        ticket = dl.get_ticket(self.desk, TODAY, 1)
        self.assertEqual(ticket["status"], "CLOSED")
        self.assertAlmostEqual(ticket["pnl_r"], 1.0)  # +4.00 on 4.00 of risk
        rows = dl.read_journal(self.desk)
        self.assertEqual((rows[0]["ticker"], rows[0]["outcome"]), ("NVDA", "FILLED"))

    def test_kill_blocks_new_tickets_and_resume_does_not_clear_it(self):
        self.assertEqual(self.cli("kill").returncode, 0)
        self.assertEqual(self.cli("resume").returncode, 0)
        self.assertTrue((self.desk / "KILL").exists())
        self.assertEqual(self.cli("ticket", "AAPL", "--last-close", "100", "--atr", "2").returncode, 2)

    def test_snapshot_flags_missing_stop_and_daily_loss(self):
        dl.save_snapshot(self.desk, {"equity_usd": 100_000, "buying_power_usd": 1}, date(2026, 11, 13))
        dl.snapshot_path(self.desk, TODAY).unlink()
        out = self.cli("snapshot", "-", stdin=json.dumps({
            "equity_usd": 96_000, "buying_power_usd": 10_000,
            "positions": [{"symbol": "AAPL", "quantity": 10}, {"symbol": "MSFT", "quantity": 5}],
            "open_orders": [{"symbol": "MSFT", "side": "SELL", "order_type": "STOP_LOSS", "quantity": 5}],
        }))
        self.assertIn("MISSING STOP: AAPL", out.stdout)
        self.assertNotIn("MISSING STOP: MSFT", out.stdout)
        self.assertIn("DAILY LOSS LIMIT HIT", out.stdout)
        self.assertTrue((self.desk / "PAUSE").exists())

    def test_init_never_overwrites_limits(self):
        self.limits(max_open_positions=2)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "desk.py"), "init", str(self.desk)],
                       check=True, capture_output=True)
        self.assertEqual(dl.load_limits(self.desk)["max_open_positions"], 2)

    def test_review_flags_decay(self):
        for _ in range(20):
            dl.append_journal(self.desk, {"date": "2026-11-16", "ticker": "X", "outcome": "FILLED", "pnl_r": 0.05})
        self.assertEqual(dl.review(self.desk, 0.30)["verdict"], "DECAYING")
        self.assertEqual(dl.review(self.desk, 0.08)["verdict"], "ON TRACK")
        self.assertEqual(dl.review(self.desk, None)["verdict"], "NO BACKTEST TO COMPARE")

    def test_health_reports_broken_chain(self):
        out = self.cli("health")
        self.assertEqual(out.returncode, 1)
        self.assertIn("CHAIN BROKEN", out.stdout)


if __name__ == "__main__":
    unittest.main()
