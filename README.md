# Webull AI Trading Desk

A set of Claude Code skills that runs a small trading desk: six AI seats that
research, price, attack, size and journal trades, and one human who approves
every order. Market data, account data and orders go through the
[Webull MCP server](https://developer.webull.com/apis/docs/AI-friendly-Resources/mcp/).
Filings and news also come from outside sources such as SEC EDGAR.

The AI proposes. Code decides what may reach the broker. You keep the final tap.

> **Not financial advice.** This is an educational tool for running a
> disciplined process. It starts in paper mode. Once you point it at a live
> account it can lose real money. You are responsible for every order you
> approve. This project is not affiliated with Webull, Anthropic, or anyone
> named below.

[ภาษาไทย: เริ่มใช้งาน](#เริ่มใช้งาน-ภาษาไทย)

## The idea

The design follows the article
[How to Build 24/7 AI Trading System](https://x.com/51bodila/article/2110409939600462238)
by @51bodila, which turns seven rules from public talks by engineers at a
large trading firm into seven seats. This project rebuilds that desk for
Claude Code and Webull, and ships the safety layer as tested code instead of
asking a bot to write it.

| Rule | Seat | What it does here |
|---|---|---|
| Ignore 99% of the data | **Scout** | Scans insider purchases and news; passes 5 names at most, often none |
| Every trade is "what is this worth?" | **Analyst** | Sourced fair value range and the gap to price |
| Most ideas are bad | **Quant** | Backtests in code with a 12-month holdout that runs once |
| Your AI will cheat | **Skeptic** | Attacks every trade and audits the backtest's git history |
| Code guards the money | **Risk** | Tickets sized by a script; an order gate checks every order call |
| Humans run the weird days | **You (PM)** | Approve by ticket number; write PAUSE or KILL at any time |
| Every model decays | **Journal** | Logs trades taken and skipped; flags a decaying strategy |

```
05:30 ET  Scout -> Analyst -> Skeptic -> Risk -> brief + tickets -> you
          you: "approve 1 3"  ->  order gate  ->  your tap  ->  Webull
15:45 ET  Risk closes positions on their 5th trading day
16:30 ET  Journal logs everything, including the trades not taken
Friday    weekly review vs the backtest      Sunday  Skeptic audits Quant
```

## Install

Requires [Claude Code](https://claude.com/claude-code), Python 3.9+ and git.
No Python packages are needed.

```
/plugin marketplace add nutdnuy/webull-ai-trading-desk
/plugin install trading-desk@webull-ai-trading-desk
```

Restart Claude Code, open the project folder where the desk should live, and
run:

```
/trading-desk:onboarding
```

(`/onboarding` also works when no other skill uses that name.)

Onboarding creates a `desk/` folder, runs the safety tests, helps you connect
Webull, walks through the limits with your real account size, proves the
order gate refuses an order, and offers a dry run. Nothing trades during
onboarding.

## Connecting Webull

Onboarding guides you through one of two ways.

| | Local MCP, sandbox | Cloud MCP |
|---|---|---|
| Money | Simulated (paper) | Your live account |
| Sign-in | App Key and Secret from the Webull developer console | OAuth, no keys |
| Orders | `place_stock_order` goes to the sandbox | `place_stock_instruction` waits for you to confirm it in the Webull app |
| Best for | Your first month | Read-only research, then live trading you confirm in the app |

Local sandbox, run in your own terminal (never paste keys into a chat):

```bash
claude mcp add webull --env WEBULL_APP_KEY=your_key --env WEBULL_APP_SECRET=your_secret --env WEBULL_ENVIRONMENT=uat --env WEBULL_REGION_ID=us --env WEBULL_MAX_ORDER_NOTIONAL_USD=10000 -- uvx webull-openapi-mcp serve
```

Cloud:

```bash
claude mcp add --transport http webull https://api.webull.com/mcp
```

then `/mcp` in a session to sign in. Choose Non-Trading while you are learning
the desk. See Webull's own pages for
[Cloud MCP](https://developer.webull.com/apis/docs/AI-friendly-Resources/mcp/)
and
[Local MCP](https://developer.webull.com/apis/docs/AI-friendly-Resources/local-mcp);
tool availability differs by region and account.

If the server has no order tool, the desk still works: Risk prints each
approved ticket as steps for you to type into the Webull app.

## Skills

| Skill | Use it to |
|---|---|
| `onboarding` | Set up or repair the desk |
| `desk-morning` | Run the whole morning chain and get the brief |
| `desk-scout` | Scan, dry-run the scan, or summarise a weird day |
| `desk-analyst` | Price the scouted names |
| `desk-skeptic` | Attack today's names, or audit the Quant repo |
| `desk-risk` | Snapshot, tickets, approvals, sending, stops, exits, PAUSE / RESUME / KILL |
| `desk-quant` | Backtest the starter strategy or a new idea |
| `desk-journal` | Daily log, Friday review, health check, go-live check |
| `desk-routines` | Put the desk on a schedule |

The starter strategy: buy after a CEO, CFO, COO, President or Chair makes an
open-market purchase of $100K or more (SEC Form 4, code P), in a stock that
trades $20M a day or more; enter with a DAY limit 0.5% above the last close;
stop 2 x ATR(14) below entry; exit after 5 trading days; risk 1% of equity
per trade. It is a public, checkable idea to learn the process on. Backtest
it before you trust it. "No edge" is a valid result.

## How the money is guarded

Five layers, from the inside out.

1. **Limits in a file.** `desk/limits.json` holds risk per trade, position
   size, open positions, daily loss, paper-only and cash-only. Defaults:

   ```json
   {"risk_per_trade_pct": 1.0, "max_open_positions": 5, "max_position_pct": 10,
    "max_daily_loss_pct": 3, "paper_only": true, "cash_only": true,
    "allow_fractional": false, "auto_allow_exits": false}
   ```

2. **Sizing and risk check in code.** `desk.py ticket` computes
   `shares = equity x 1% / (entry - stop)`, rounds down, applies every cap and
   writes a ticket. The AI does not do this arithmetic.
3. **The order gate.** A Claude Code `PreToolUse` hook runs before every
   broker order tool call. A BUY passes only if a ticket for that symbol
   exists today, passed the risk check, was approved, still passes against a
   fresh account snapshot, and the order is a DAY LIMIT at or below the
   ticket's price and size. A SELL passes only if it closes or protects a
   position the desk holds. Option, futures, crypto, event, algo, order-edit
   and community-post tools are blocked. Anything the gate cannot read is
   blocked.
4. **Your tap.** An order that passes the gate still asks you to approve the
   call. In a session that asks nobody (bypass mode, unattended runs) the
   order is blocked.
5. **Webull's own controls.** The Cloud server only creates instructions you
   confirm in the Webull app. The Local server defaults to the sandbox and
   has its own order-size cap and symbol whitelist.

`PAUSE` stops new tickets and buys. `KILL` does the same and only you can
remove it. Cancelling orders is never blocked.

### What this does not protect against

Be clear-eyed about the limits of the design:

- The hooks only run when this pack is installed as a Claude Code plugin on
  the machine doing the work. They do **not** run in cloud scheduled tasks or
  in other agent tools. Do not give those a Webull connection that can trade.
- A second hook asks you before the AI edits `limits.json`, `broker.json`,
  the KILL file or the safety scripts. It is a speed bump against mistakes,
  not a sandbox. Read what you approve.
- `paper_only` trusts `desk/broker.json` to say whether the server is a
  sandbox. The script cannot see the server's real environment. Onboarding
  asks you; answer truthfully and keep keys for live and paper apart.
- The market calendar knows scheduled NYSE holidays, not unscheduled closures
  or half days.
- A stop order is not a guaranteed price. Gaps go through stops.
- The backtest uses whatever price files you give it. Free sources drop
  delisted stocks, so results are a best case.

## The desk CLI

Everything the seats compute goes through one script, copied into your desk
at `desk/tools/desk.py`:

```
status        limits, flags and today's tickets
snapshot      validate and save the account snapshot; flags missing stops and daily loss
bars          check a daily bars CSV; print last close, ATR(14), dollar volume
ticket        size a BUY ticket and risk-check it
approve       record your approval of ticket numbers
reject / sent / reopen / not-filled / fill / close     ticket life cycle
exits-due     positions on their exit day and positions without a stop
pause / resume / kill
log / review  journal rows; live results vs the backtest; DECAYING check
health        is today's chain complete?
broker        show or set how the desk reaches Webull
```

No command talks to the broker.

## Scheduling

`/trading-desk:desk-routines` sets up the schedule in the table above with
scheduled tasks on your own computer. Research can run unattended. Orders
cannot: they wait for you.

## Without the plugin

The skills are plain `SKILL.md` files and work in any agent that reads them,
but the order gate is a Claude Code hook. If you copy the skills by hand,
clone this repo and add the hooks to your `~/.claude/settings.json`, with the
real path in place of `/path/to/webull-ai-trading-desk`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "mcp__.*__((place|replace)_(stock|equity|options?|futures|crypto|event|algo)(_.*)?|create_community_(post|comment))",
       "hooks": [{"type": "command", "command": "python3 /path/to/webull-ai-trading-desk/scripts/order_gate.py"}]},
      {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
       "hooks": [{"type": "command", "command": "python3 /path/to/webull-ai-trading-desk/scripts/protect_files.py"}]}
    ],
    "PostToolUse": [
      {"matcher": "mcp__.*__place_stock_.*",
       "hooks": [{"type": "command", "command": "python3 /path/to/webull-ai-trading-desk/scripts/order_gate.py"}]}
    ]
  }
}
```

Create the desk with `python3 /path/to/webull-ai-trading-desk/scripts/desk.py init desk`.
Without the hooks, treat the desk as read-only and enter orders yourself.

## Tests

```bash
python3 -m unittest discover -s tests -v
cd desk-template/quant && python3 -m unittest discover -s tests -v
```

The first suite covers the size formula, every limit, KILL and PAUSE, the
gate's decisions for each order shape, and the two hook scripts run the way
Claude Code runs them. The second covers the backtest: no look-ahead, costs,
gap-through stops, and a holdout that cannot be seen early or run twice.

## Status

Version 0.1.0. What has and has not been verified:

- Verified: both test suites (76 tests); the plugin and marketplace
  manifests with `claude plugin validate`; the two hook scripts run as
  subprocesses with the stdin and exit-code contract from the Claude Code
  hooks documentation.
- Not verified by the author: the hooks firing inside a live Claude Code
  session, and a full trading day against a Webull sandbox or live account.
  `bash tests/e2e_gate.sh` checks the first on your machine against a stand-in
  server that trades nothing; run it once after installing. The order tools'
  argument names were read from Webull's open-source Local MCP server; the
  Cloud server's `place_stock_instruction` arguments were not available to
  check, and onboarding checks them on your machine. Expect rough edges, and
  please open an issue when you find one.

## Credits

- Desk design: [@51bodila's article](https://x.com/51bodila/article/2110409939600462238).
- Broker access: [Webull OpenAPI MCP](https://github.com/webull-inc/webull-openapi-mcp).
- Starter data: SEC EDGAR and the SEC Insider Transactions Data Sets.

MIT licence.

---

## เริ่มใช้งาน (ภาษาไทย)

ชุดสกิลนี้ทำให้ Claude Code ทำงานเป็น "โต๊ะเทรด" ที่มี AI 6 ที่นั่ง และมีคุณเป็น
ผู้จัดการพอร์ต (PM) ที่อนุมัติทุกคำสั่งซื้อขาย ข้อมูลราคา บัญชี และการส่งคำสั่ง
ใช้ Webull MCP ส่วนข่าวและเอกสารยื่น ก.ล.ต. สหรัฐฯ ดึงจากแหล่งภายนอกได้

> ไม่ใช่คำแนะนำการลงทุน เครื่องมือนี้มีไว้ฝึกกระบวนการเทรดอย่างมีวินัย
> ค่าเริ่มต้นคือบัญชีทดลอง (paper) เมื่อเชื่อมบัญชีจริงแล้วขาดทุนได้จริง
> คุณรับผิดชอบทุกคำสั่งที่คุณกดอนุมัติ

**ติดตั้ง** (ต้องมี Claude Code, Python 3.9 ขึ้นไป และ git)

```
/plugin marketplace add nutdnuy/webull-ai-trading-desk
/plugin install trading-desk@webull-ai-trading-desk
```

ปิดแล้วเปิด Claude Code ใหม่ เปิดโฟลเดอร์โปรเจกต์ที่ต้องการ แล้วพิมพ์

```
/trading-desk:onboarding
```

Onboarding จะพาทำทีละขั้น: สร้างโฟลเดอร์ `desk/` รันชุดทดสอบความปลอดภัย
เชื่อม Webull (แนะนำเริ่มที่ sandbox) อธิบายเพดานความเสี่ยงด้วยขนาดบัญชีจริงของคุณ
พิสูจน์ว่าด่านกั้นคำสั่งทำงาน แล้วลองสแกนแบบไม่ส่งคำสั่ง ระหว่าง onboarding
จะไม่มีการซื้อขายใด ๆ

**ใช้งานประจำวัน**

| พิมพ์ | ได้อะไร |
|---|---|
| `/trading-desk:desk-morning` | สแกน ตีมูลค่า จับผิด คำนวณขนาดไม้ แล้วสรุป 3 บรรทัดพร้อมตั๋วคำสั่ง |
| `approve 1 3` | อนุมัติตั๋วหมายเลข 1 และ 3 จากนั้นกดอนุญาตอีกครั้งตอนส่งคำสั่ง |
| `PAUSE` / `RESUME` / `KILL` | หยุดเปิดสถานะใหม่ / กลับมาทำงาน / หยุดทุกอย่าง |
| `/trading-desk:desk-quant` | ทดสอบกลยุทธ์ย้อนหลังก่อนใช้เงิน ควรทำในสัปดาห์แรก |
| `/trading-desk:desk-journal` | บันทึกผล ทบทวนรายสัปดาห์ และตรวจความพร้อมก่อนใช้เงินจริง |
| `/trading-desk:desk-routines` | ตั้งเวลาให้โต๊ะทำงานเอง (05:30 เวลานิวยอร์ก ตรงกับช่วงเย็นของไทย) |

**เงินถูกคุมอย่างไร** เพดานอยู่ในไฟล์ `desk/limits.json` สคริปต์เป็นคนคำนวณขนาดไม้
(เสี่ยง 1% ของพอร์ตต่อไม้) และมี hook ตรวจทุกคำสั่งก่อนถึง Webull
คำสั่งซื้อจะผ่านได้ก็ต่อเมื่อมีตั๋วของวันนี้ที่ผ่านการตรวจความเสี่ยงและคุณอนุมัติแล้ว
และถึงผ่านด่านก็ยังต้องให้คุณกดอนุญาตอีกครั้ง AI แก้เพดานเองไม่ได้
ข้อจำกัดของระบบอ่านได้ในหัวข้อ "What this does not protect against" ด้านบน

**เดือนแรก** สัปดาห์ที่ 1 ติดตั้งและ backtest ถ้าไม่มี edge ให้ทดสอบไอเดียถัดไป
จากนั้นเทรดบัญชีทดลองจนครบ 25 ถึง 50 ไม้ก่อนเปลี่ยนอะไร แล้วค่อยพิจารณาบัญชีจริงขนาดเล็ก
เมื่อผลจริงตรงกับ backtest การสลับไปบัญชีจริงคุณต้องทำเองด้วยมือเสมอ
