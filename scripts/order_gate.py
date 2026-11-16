#!/usr/bin/env python3
"""Claude Code hook: the trading desk's order gate.

PreToolUse  -> decide whether a broker order tool call may proceed.
PostToolUse -> mark the matching ticket SENT.

A BUY reaches the broker only if it matches a ticket that passed risk-check,
that the PM approved, and that still passes right now. Even then the human is
asked. Anything unclear is blocked (exit code 2).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

NO_HUMAN_MODES = ("bypassPermissions", "dontAsk")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_name = str(event.get("tool_name", ""))
    try:
        import desklib as dl
    except Exception as exc:  # the safety code itself is broken: block orders
        if tool_name.rsplit("__", 1)[-1].startswith(("place_stock", "replace_stock")):
            print(f"Desk gate cannot load its safety code ({exc}); order blocked.", file=sys.stderr)
            return 2
        return 0

    if not dl.is_order_tool(tool_name):
        return 0
    tool_input = event.get("tool_input")
    desk = dl.find_desk(event.get("cwd"))

    if event.get("hook_event_name") == "PostToolUse":
        if desk is not None:
            try:
                dl.mark_sent(desk, tool_name, tool_input)
            except Exception:
                pass
        return 0

    try:
        result = dl.gate(tool_name, tool_input, desk)
    except Exception as exc:
        result = {"decision": "deny", "reason": f"Desk gate error ({exc}); failing closed."}

    decision, reason = result.get("decision"), result.get("reason", "")
    if decision == "ask" and event.get("permission_mode") in NO_HUMAN_MODES:
        decision = "deny"
        reason = (
            "Desk gate: this order passed the checks but needs the PM's tap, and this session "
            f"runs in {event.get('permission_mode')} mode where nobody is asked. Switch to a "
            "normal permission mode to send orders."
        )
        result = {"decision": decision, "reason": reason}
    dl.log_gate(desk, tool_name, tool_input, result)

    if decision is None:
        return 0
    if decision == "deny":
        print(reason, file=sys.stderr)
        return 2
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
