#!/usr/bin/env python3
"""A stand-in MCP server for testing the order gate. It is not a broker.

It offers tools named like Webull's (`place_stock_order`, `cancel_order`,
`get_account_balance`). A "placed" order is only appended to the file named
by $FAKE_BROKER_LOG, so a test can prove whether a call got past the gate.

    claude mcp add fake-broker --env FAKE_BROKER_LOG=/tmp/fake-orders.log -- python3 tests/fake_broker_mcp.py
"""
from __future__ import annotations

import json
import os
import sys

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "side": {"type": "string"},
        "order_type": {"type": "string"},
        "time_in_force": {"type": "string"},
        "quantity": {"type": "number"},
        "limit_price": {"type": "number"},
        "stop_price": {"type": "number"},
    },
    "required": ["symbol", "side", "order_type", "quantity"],
}
TOOLS = [
    {"name": "place_stock_order", "description": "FAKE: record a stock order in a local log file.",
     "inputSchema": ORDER_SCHEMA},
    {"name": "cancel_order", "description": "FAKE: record a cancel in a local log file.",
     "inputSchema": {"type": "object", "properties": {"client_order_id": {"type": "string"}}}},
    {"name": "get_account_balance", "description": "FAKE: return a made-up balance.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def reply(message_id, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, message_id = message.get("method"), message.get("id")
        if message_id is None:
            continue  # a notification
        if method == "initialize":
            reply(message_id, {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-broker", "version": "0.1.0"},
            })
        elif method == "tools/list":
            reply(message_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params", {})
            name, arguments = params.get("name"), params.get("arguments", {})
            if name == "get_account_balance":
                text = "FAKE balance: equity 100000.00 USD, buying power 100000.00 USD"
            else:
                log = os.environ.get("FAKE_BROKER_LOG")
                if log:
                    with open(log, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"tool": name, "arguments": arguments}) + "\n")
                text = f"FAKE {name} recorded: {json.dumps(arguments)}"
            reply(message_id, {"content": [{"type": "text", "text": text}]})
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id,
                                         "error": {"code": -32601, "message": "method not found"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
