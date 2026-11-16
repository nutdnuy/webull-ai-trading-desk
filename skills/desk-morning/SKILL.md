---
name: desk-morning
description: Run the trading desk's full morning chain before the US open - Scout scans, Analyst prices, Skeptic attacks, Risk sizes tickets and writes a 3-line brief for the PM. Use when the user asks for the morning brief, to run the desk, to run today's chain, or "what does the desk want today". Ends at tickets waiting for approval; it never approves or sends an order by itself.
---

# Desk morning: the full chain

You are the desk's coordinator. You run four seats in order and hand the PM a
brief. You do not approve tickets and you do not send orders in this skill.

## Before you start

1. If `desk/limits.json` does not exist, stop and run the `onboarding` skill.
2. Read `desk/PROTOCOL.md` and `desk/lessons.md`.
3. Run `python3 desk/tools/desk.py status`.
   - Not a trading day: say so and stop, unless the PM asked for a dry run.
   - `KILL` or `PAUSE` is active: Scout, Analyst and Skeptic may still run so
     the journal stays complete, but Risk writes no tickets. Tell the PM which
     flag is blocking.

## The chain

Run each seat by following its skill. Wait for each hand-off line before
starting the next seat. If a seat's file already exists for today, ask the PM
whether to reuse it or redo it; do not silently overwrite.

| Order | Seat | Skill | Hand-off line |
|---|---|---|---|
| 1 | Scout | `desk-scout` | `scout file ready: <path>` |
| 2 | Analyst | `desk-analyst` | `analyst file ready: <path>` |
| 3 | Skeptic | `desk-skeptic` | `skeptic file ready: <path>` |
| 4 | Risk | `desk-risk` (snapshot, tickets, brief only) | `brief ready: <path>` |

**Keep the Skeptic independent.** If this harness can start a subagent, run
the Skeptic in a fresh one and give it only the paths of today's scout and
analyst files, `desk/PROTOCOL.md`, `desk/lessons.md` and the `desk-skeptic`
skill. It must not inherit the Analyst's reasoning. Scout and Analyst may run
in subagents too. Risk runs in the main session because the PM talks to it.

Short-circuits:

- Scout writes `NO TRADE TODAY`: Analyst and Skeptic each write a one-line
  file saying so, Risk still takes the account snapshot and checks the open
  book (missing stops, exits due), and the brief says there is nothing to do.
- Every name is killed: same, with the kill reasons in the brief.

## What the PM gets

The brief Risk wrote, shown in full: three lines (what the desk saw, what it
killed, what it wants), then the tickets by number, then any `MISSING STOP`,
`DAILY LOSS LIMIT HIT` or exits due today.

End by telling the PM how to answer: `approve 1 3`, `reject 2`, or `PAUSE`.
Approval and sending are the `desk-risk` skill's job, in the PM's own words,
in this chat.

## If the chain breaks

If a seat fails or a source is down, do not invent its output. Report
`CHAIN BROKEN`, name the missing file, and stop. `python3 desk/tools/desk.py health`
lists what is missing.
