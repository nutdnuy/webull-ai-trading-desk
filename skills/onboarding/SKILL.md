---
name: onboarding
description: First-time setup of the AI trading desk for someone who just installed this skill pack. Creates the desk folder, connects and verifies the Webull MCP server (paper sandbox or Cloud), walks through the risk limits, proves the order gate works, and ends with a dry run. Use when the user runs /onboarding, says they just installed the trading desk, asks how to start or set up, or when another desk skill finds no desk folder.
---

# Onboarding: set up the trading desk

You are setting up a desk of six AI seats (Scout, Analyst, Skeptic, Risk,
Quant, Journal) plus one human, the PM. The user is the PM. Reply in the
user's language. Keep each message short: say what this step does, do it,
show the result, move on.

**Nothing trades during onboarding.** Do not call any tool that places,
changes or cancels an order, and do not post anything. Every Webull call in
this skill is read-only.

Onboarding can be run again at any time. Each step starts by checking whether
it is already done, so a second run picks up where the first stopped (for
example after the user restarts the session to load the Webull server).

Plugin files live at `${CLAUDE_PLUGIN_ROOT}`. If that text was not replaced by
a real path, the plugin root is two folders above this file's folder.

## Step 0. Say what is about to happen

Tell the user, in five lines or fewer:

- you will create a `desk/` folder in this project;
- you will connect Webull, read-only first;
- orders, later, need a ticket that passed the risk check, the PM's approval
  by number, and the PM's tap on the permission prompt;
- the default is paper trading, and it stays that way until the PM changes it
  by hand;
- this is a tool for learning to run a disciplined process, not financial
  advice, and it can lose money once it trades a live account.

## Step 1. Preflight

Run `python3 --version` (3.9 or newer is required) and `git --version`.
Check the working directory is a project folder the user chose, not their
home folder. If it is the home folder or an unrelated repo, ask where the desk
should live before creating anything.

## Step 2. Create the desk

Skip if `desk/limits.json` exists; then only refresh the tools with the same
command (it never overwrites `limits.json`, `lessons.md` or anything in
`quant/`).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/desk.py" init desk
git -C desk/quant rev-parse --git-dir 2>/dev/null || git init desk/quant
python3 -m unittest discover -s "${CLAUDE_PLUGIN_ROOT}/tests"
( cd desk/quant && python3 -m unittest discover -s tests )
```

Both test runs must end in `OK`. If either fails, stop and show the failure.
Do not continue with a desk whose safety tests fail.

Then read `desk/PROTOCOL.md`. It is the rulebook every seat follows.

## Step 3. Connect Webull

First look at the tools this session already has. If you can see Webull tools
such as `get_account_list` and `get_stock_snapshot`, the server is connected:
go to Step 4.

If not, explain the two ways and ask the user to pick one.

**A. Paper sandbox with the Local MCP (recommended to start).** Simulated
money, real order flow. Needs `uv` and a sandbox API key.

1. In the Webull developer portal for their region, open the Developer
   Console, switch to the sandbox (paper trade) environment, apply for OpenAPI
   for paper trading, and generate an App Key and App Secret. Sandbox approval
   usually takes minutes. A live API application takes one to two business
   days and needs an open brokerage account.
2. The user runs this **in their own terminal**, filling in the two values.
   The key and secret must never be typed into this chat or written to a
   project file:

   ```bash
   claude mcp add webull --env WEBULL_APP_KEY=your_key --env WEBULL_APP_SECRET=your_secret --env WEBULL_ENVIRONMENT=uat --env WEBULL_REGION_ID=us --env WEBULL_MAX_ORDER_NOTIONAL_USD=10000 -- uvx webull-openapi-mcp serve
   ```

   `WEBULL_ENVIRONMENT=uat` is the sandbox. `WEBULL_REGION_ID` is their
   region (`us`, `hk`, `jp`, `sg`, `th`, `my`, `uk`, ...). To make the server
   read-only, add `--env WEBULL_TOOLSETS=account,instrument,market-data`.
3. If the account has 2FA, they run `uvx webull-openapi-mcp auth` once and
   approve it in the Webull app.

**B. Webull Cloud MCP.** Hosted by Webull, signs in with OAuth, no keys. It
reaches the **live** account; there is no sandbox. The user picks which
accounts to share and, per account, Non-Trading (read-only) or Trading. With
Trading, the desk can only create a trade *instruction*; it becomes an order
when the user confirms it in the Webull app.

```bash
claude mcp add --transport http webull https://api.webull.com/mcp
```

Then `/mcp` in the session, choose `webull`, and sign in. In the Claude
desktop or web app, add it as a custom connector with the same URL. Suggest
Non-Trading for the first weeks.

After either path the user restarts the session and runs onboarding again.
If the user gives you a key by mistake, do not repeat it, do not store it, and
tell them to rotate it.

## Step 4. Verify the connection (read-only)

Call, in order, and report each result in one line:

1. `get_account_list`: which accounts are visible. Never write an account
   number or ID to a file.
2. `get_account_balance` and `get_account_positions` for the account the PM
   names (ask if there is more than one).
3. `get_stock_snapshot` for `SPY`: is the price live, delayed or missing?
4. `get_stock_bars` for `SPY`, daily, 30 bars: do bars come back?

Then work out the mode from the tools you can see:

| You can see | Mode | Orders go |
|---|---|---|
| `place_stock_order` | `local` | straight to Webull (sandbox if `uat`) |
| `place_stock_instruction` | `cloud` | to the Webull app for the PM to confirm |
| neither | read-only | nowhere; the PM types each ticket into the app |

Ask the PM whether a local server runs `uat` (sandbox) or `prod`; you cannot
see its environment from here, and sandbox data can look real. Then record it
(the hook will ask the PM to approve this command; that is expected):

```bash
python3 desk/tools/desk.py broker --mode local --environment uat --order-tool place_stock_order --stop-method oto
```

Use `--mode cloud` for Cloud (the environment is then always `prod`),
`--stop-method oto` only if `place_stock_combo_order` exists, otherwise
`after_fill`. For read-only setups record the mode and environment and leave
out `--order-tool`.

If an order tool exists, read its input schema and check it has fields the
gate can read: a symbol, a side, a quantity, an order type and a limit price.
If the names differ from `symbol`, `side`, `quantity`, `order_type`,
`limit_price`, tell the PM: the gate fails closed on orders it cannot read, so
the desk would run in read-only mode until that is fixed.

## Step 5. Walk through the limits

Run `python3 desk/tools/desk.py status` and explain `desk/limits.json` in
plain words, using the PM's real equity from Step 4 for the examples:

- `risk_per_trade_pct` 1.0: a stopped-out trade loses about 1% of equity.
- `max_position_pct` 10: no position costs more than 10% of equity.
- `max_open_positions` 5, `max_daily_loss_pct` 3 (a worse day creates PAUSE).
- `paper_only` true: every ticket fails unless the broker is a sandbox.
- `cash_only` true: a ticket can never cost more than the buying power.
- `auto_allow_exits` false: even closing orders ask the PM.
- trade plan: entry 0.5% above the last close, stop 2 x ATR(14) below entry,
  exit after 5 trading days.

Ask whether they want different numbers. If so, the PM edits `limits.json`
themselves, or approves your edit when the hook asks. Never loosen a limit on
your own suggestion. If equity is small enough that most stocks size to zero
shares, say so now; that is the limit working, not a bug.

Leave `paper_only` true. Going live is a later, deliberate decision that the
PM makes by hand after 25 to 50 paper trades match the backtest.

## Step 6. SEC contact line

Scout reads filings from SEC EDGAR, which requires every client to identify
itself with a contact line (a name and an email address). Ask the PM for the
line they want to use and save exactly that to `desk/.sec-user-agent` (it is
git-ignored). Never make one up from details you happen to know. If they
skip this, Scout uses other sources and marks EDGAR as unavailable.

## Step 7. Prove the gate works

Show the PM that an order without a ticket is refused, without calling Webull:

```bash
echo '{"hook_event_name":"PreToolUse","tool_name":"mcp__webull__place_stock_order","tool_input":{"symbol":"SPY","side":"BUY","order_type":"LIMIT","quantity":1,"limit_price":1},"permission_mode":"default"}' | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/order_gate.py"; echo "exit code: $?"
```

Expected: a `Desk gate:` reason and `exit code: 2`. Explain that Claude Code
runs this same script before every real order call, and that it only does so
when this pack is installed as a plugin. For a full check inside a real
session, the PM can run `bash "${CLAUDE_PLUGIN_ROOT}/tests/e2e_gate.sh"` in
their own terminal; it uses a stand-in server and trades nothing. If the skills were copied by hand,
the hooks are not active: point the PM to the README section "Without the
plugin" and treat the desk as read-only until they are.

## Step 8. Optional: fewer prompts for routine work

Offer to add allow rules to the project's `.claude/settings.json` for the
read-only desk commands only. Show the lines and add them only if the PM
agrees:

```json
{"permissions": {"allow": [
  "Bash(python3 desk/tools/desk.py status*)",
  "Bash(python3 desk/tools/desk.py snapshot*)",
  "Bash(python3 desk/tools/desk.py bars*)",
  "Bash(python3 desk/tools/desk.py ticket*)",
  "Bash(python3 desk/tools/desk.py risk-check*)",
  "Bash(python3 desk/tools/desk.py exits-due*)",
  "Bash(python3 desk/tools/desk.py health*)",
  "Bash(python3 desk/tools/desk.py review*)"
]}}
```

Never add an allow rule for an order tool, for `desk.py approve`, or for
`desk.py broker`.

## Step 9. Dry run

Offer a dry run of the morning scan for the last trading day using the
`desk-scout` skill: every name passed, every name rejected and why, every
batch flagged. No tickets are approved and no order is sent.

## Step 10. Finish

Run `python3 desk/tools/desk.py health` and give the PM a short checklist:
desk folder, safety tests, Webull connection and mode, paper or live, limits,
SEC line, gate proof, dry run. Mark each done or not done, and say plainly
what is still missing.

Then tell them what to type next:

- `/trading-desk:desk-morning` runs the morning chain and ends with a brief
  and tickets.
- `/trading-desk:desk-quant` backtests the starter strategy before any paper
  share is bought. Do this in week one.
- `/trading-desk:desk-routines` sets the daily schedule.
- Writing `PAUSE`, `RESUME` or `KILL` in a chat with Risk stops or restarts
  the desk.
