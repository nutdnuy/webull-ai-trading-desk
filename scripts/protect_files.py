#!/usr/bin/env python3
"""Claude Code hook: ask the human before the AI changes the desk's safety files.

Covers limits.json, broker.json, the KILL switch, desk/tools and the plugin's
own scripts. It is a speed bump, not a vault: the real backstops are the
order gate, the broker-side limits and the human's tap.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    try:
        event = json.load(sys.stdin)
        import desklib as dl

        desk = dl.find_desk(event.get("cwd"))
        if desk is None:
            return 0
        reason = dl.protected_reason(
            str(event.get("tool_name", "")),
            event.get("tool_input"),
            desk,
            os.environ.get("CLAUDE_PLUGIN_ROOT"),
        )
    except Exception:
        return 0
    if not reason:
        return 0
    if event.get("permission_mode") in ("bypassPermissions", "dontAsk"):
        print(reason + " Blocked because this session asks nobody.", file=sys.stderr)
        return 2
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
