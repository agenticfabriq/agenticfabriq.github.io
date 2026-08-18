#!/usr/bin/env python3
"""Drive mcp_stdio_server.py over stdio and show an allowed call and a denied one.

    python3 examples/connect-mcp/stdio_client_demo.py

This is a deliberately small MCP client: it launches the server as a subprocess,
writes newline-delimited JSON-RPC requests to its stdin, and reads one JSON
response per line from its stdout. It is not a general client -- it does not
handle SSE, pagination, or the InputRequiredResult retry pattern -- but it is
enough to watch the authorization boundary do its job.

Written against protocol revision 2026-07-28: every request carries its protocol
version and client capabilities in `params._meta`, and there is no `initialize`
handshake to perform first.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
SERVER = pathlib.Path(__file__).with_name("mcp_stdio_server.py")

CLIENT_META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "demo-client", "version": "0.1.0"},
    # An empty object is a real answer: this client supports no optional
    # capabilities. The field is required on every request regardless.
    "io.modelcontextprotocol/clientCapabilities": {},
}


class Server:
    """A server subprocess plus a request id counter."""

    def __init__(self, caller: str) -> None:
        environment = dict(os.environ)
        # The host asserts who the agent is acting for. The model never sees or
        # sets this.
        environment["MCP_CALLER_ID"] = caller
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # let the server's audit log land on our stderr
            env=environment,
            text=True,
            bufsize=1,
        )
        self.next_id = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": method,
            "params": {**(params or {}), "_meta": CLIENT_META},
        }
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout without responding")
        return json.loads(line)

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=10)


def show(label: str, response: dict[str, Any]) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(response, indent=2, sort_keys=True))


def run_as(caller: str) -> None:
    print(f"\n================ acting for {caller} ================")
    server = Server(caller)
    try:
        show("server/discover", server.call("server/discover"))
        show("tools/list", server.call("tools/list"))
        show(
            "tools/call lookup_invoice (own account)",
            server.call("tools/call", {"name": "lookup_invoice",
                                       "arguments": {"invoice_id": "inv_000042"}}),
        )
        show(
            "tools/call lookup_invoice (another tenant's invoice)",
            server.call("tools/call", {"name": "lookup_invoice",
                                       "arguments": {"invoice_id": "inv_000044"}}),
        )
        show(
            "tools/call refund_invoice",
            server.call("tools/call", {"name": "refund_invoice",
                                       "arguments": {"invoice_id": "inv_000042",
                                                     "amount_cents": 4200,
                                                     "reason": "duplicate_charge"}}),
        )
    finally:
        server.close()


def main() -> None:
    # alice may refund up to 5000 cents on acct_1001 and nothing else.
    run_as("alice@example.com")
    # triage-bot can read two accounts and refund nothing. Its tools/list is
    # shorter, and the refund is denied even though it asked for it by name.
    run_as("triage-bot")


if __name__ == "__main__":
    main()
