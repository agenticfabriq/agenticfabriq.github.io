#!/usr/bin/env python3
"""A minimal MCP server over stdio, written against protocol revision 2026-07-28.

No dependencies: the wire format is newline-delimited JSON-RPC 2.0 on stdin and
stdout, so the standard library is enough. Writing it by hand keeps every field
visible, which is the point of the example -- the authorization decisions below
are ordinary code, not framework magic.

Run it directly and type a request, or drive it with stdio_client_demo.py.

Protocol facts this file depends on, all from the 2026-07-28 revision:
  * The server reads JSON-RPC messages from stdin and writes them to stdout, one
    per line, with no embedded newlines. Nothing that is not a valid MCP message
    may go to stdout; stderr is free for logging.
  * There is no `initialize` handshake. Every request carries
    `params._meta["io.modelcontextprotocol/protocolVersion"]` and
    `params._meta["io.modelcontextprotocol/clientCapabilities"]`; both are
    REQUIRED, and a request missing either is rejected with -32602.
  * Servers MUST implement `server/discover`.
  * Every result MUST carry a `resultType` field.
  * `tools/list` results MUST carry `ttlMs` and `cacheScope`.

See https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = [PROTOCOL_VERSION]
SERVER_INFO = {"name": "billing-mcp", "version": "0.1.0"}

# JSON-RPC 2.0 standard codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Codes defined by MCP in the -32020..-32099 range reserved for the spec.
UNSUPPORTED_PROTOCOL_VERSION = -32022

PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"


# --------------------------------------------------------------------------
# Entitlements. This is the whole security model: what each caller may do is
# server-side data, keyed by an identity the server established for itself.
# Nothing here is reachable from a tool argument.
# --------------------------------------------------------------------------

ENTITLEMENTS: dict[str, dict[str, Any]] = {
    "alice@example.com": {
        "tools": {"lookup_invoice", "refund_invoice"},
        "accounts": {"acct_1001"},
        "max_refund_cents": 5000,
    },
    "triage-bot": {
        "tools": {"lookup_invoice"},
        "accounts": {"acct_1001", "acct_1002"},
        "max_refund_cents": 0,
    },
}

# Stand-in for the system of record.
INVOICES: dict[str, dict[str, Any]] = {
    "inv_000042": {"account_id": "acct_1001", "total_cents": 4200, "status": "paid"},
    "inv_000043": {"account_id": "acct_1002", "total_cents": 19900, "status": "paid"},
    "inv_000044": {"account_id": "acct_9999", "total_cents": 900, "status": "paid"},
}

INVOICE_ID = re.compile(r"^inv_[0-9]{6}$")
REFUND_REASONS = ("duplicate_charge", "customer_request", "billing_error")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_invoice",
        "title": "Look up an invoice",
        "description": (
            "Return the status and total of one invoice belonging to the calling "
            "user's own accounts. Takes an invoice id of the form inv_000000."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "pattern": "^inv_[0-9]{6}$",
                    "description": "Invoice identifier, e.g. inv_000042.",
                }
            },
            "required": ["invoice_id"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string"},
                "status": {"type": "string"},
                "total_cents": {"type": "integer"},
            },
            "required": ["invoice_id", "status", "total_cents"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "refund_invoice",
        "title": "Refund an invoice",
        "description": (
            "Refund part or all of one invoice belonging to the calling user's own "
            "accounts. The refund is capped by the caller's entitlement and cannot "
            "exceed the invoice total."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "pattern": "^inv_[0-9]{6}$",
                    "description": "Invoice identifier, e.g. inv_000042.",
                },
                "amount_cents": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Amount to refund, in cents.",
                },
                "reason": {
                    "type": "string",
                    "enum": list(REFUND_REASONS),
                    "description": "Why the refund is being issued.",
                },
            },
            "required": ["invoice_id", "amount_cents", "reason"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


class Denied(Exception):
    """An authorization failure, reported to the model as a tool execution error."""


# --------------------------------------------------------------------------
# Identity and audit
# --------------------------------------------------------------------------


def calling_identity() -> str:
    """The identity this process is acting for.

    On stdio there is no protocol-level identity: the host launched this process
    and controls its environment, so MCP_CALLER_ID is the host's assertion, not
    the model's and not the protocol's. That is exactly why it is read here and
    never from a tool argument. Over Streamable HTTP the equivalent line derives
    the identity from a validated access token instead -- same rule, different
    source.
    """
    caller = os.environ.get("MCP_CALLER_ID", "")
    if caller not in ENTITLEMENTS:
        # Fail closed: an unrecognised caller gets no tools at all rather than
        # a default set.
        return ""
    return caller


def audit(caller: str, tool: str, arguments: dict[str, Any], decision: str, detail: str) -> None:
    """One structured line per invocation, on stderr, before anything executes."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "caller": caller or "<unknown>",
        "tool": tool,
        "arguments": arguments,
        "decision": decision,
        "detail": detail,
    }
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Argument validation. Every value the model supplies is checked against a
# closed rule before it reaches anything that can act.
# --------------------------------------------------------------------------


def require_invoice_id(arguments: dict[str, Any]) -> str:
    invoice_id = arguments.get("invoice_id")
    if not isinstance(invoice_id, str) or not INVOICE_ID.match(invoice_id):
        raise Denied("invoice_id must match ^inv_[0-9]{6}$")
    return invoice_id


def owned_invoice(caller: str, invoice_id: str) -> dict[str, Any]:
    """Resolve an invoice, but only within the caller's own accounts.

    The account is never an argument. The model names an invoice; the server
    decides whether that invoice is inside the caller's boundary. An invoice
    outside it is reported as not found, so the tool result cannot be used to
    probe for the existence of other tenants' records.
    """
    entitlement = ENTITLEMENTS[caller]
    invoice = INVOICES.get(invoice_id)
    if invoice is None or invoice["account_id"] not in entitlement["accounts"]:
        raise Denied(f"no invoice {invoice_id} in your accounts")
    return invoice


def check_tool_allowed(caller: str, tool_name: str) -> None:
    if tool_name not in TOOLS_BY_NAME:
        raise Denied(f"unknown tool {tool_name}")
    if not caller or tool_name not in ENTITLEMENTS[caller]["tools"]:
        raise Denied(f"{caller or 'unknown caller'} may not call {tool_name}")


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def tool_lookup_invoice(caller: str, arguments: dict[str, Any]) -> dict[str, Any]:
    invoice_id = require_invoice_id(arguments)
    invoice = owned_invoice(caller, invoice_id)
    return {
        "invoice_id": invoice_id,
        "status": invoice["status"],
        "total_cents": invoice["total_cents"],
    }


def tool_refund_invoice(caller: str, arguments: dict[str, Any]) -> dict[str, Any]:
    invoice_id = require_invoice_id(arguments)

    amount = arguments.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        raise Denied("amount_cents must be a positive integer")

    reason = arguments.get("reason")
    if reason not in REFUND_REASONS:
        raise Denied(f"reason must be one of {', '.join(REFUND_REASONS)}")

    invoice = owned_invoice(caller, invoice_id)
    ceiling = ENTITLEMENTS[caller]["max_refund_cents"]
    if amount > ceiling:
        raise Denied(f"refund of {amount} exceeds your ceiling of {ceiling}")
    if amount > invoice["total_cents"]:
        raise Denied(f"refund of {amount} exceeds the invoice total")

    invoice["status"] = "refunded"
    return {"invoice_id": invoice_id, "refunded_cents": amount, "reason": reason}


HANDLERS = {
    "lookup_invoice": tool_lookup_invoice,
    "refund_invoice": tool_refund_invoice,
}


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------


def send(message: dict[str, Any]) -> None:
    """One JSON message per line on stdout, and nothing else, ever."""
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result_response(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    payload = {"resultType": "complete", **payload}
    meta = dict(payload.get("_meta") or {})
    meta[SERVER_INFO_KEY] = SERVER_INFO
    payload["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def check_request_meta(params: dict[str, Any]) -> str | None:
    """Return an error message if the required per-request _meta is missing."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return "params._meta is required"
    if not isinstance(meta.get(PROTOCOL_VERSION_KEY), str):
        return f"params._meta['{PROTOCOL_VERSION_KEY}'] is required"
    if not isinstance(meta.get(CLIENT_CAPABILITIES_KEY), dict):
        return f"params._meta['{CLIENT_CAPABILITIES_KEY}'] is required"
    return None


def handle_discover(request_id: Any) -> dict[str, Any]:
    return result_response(
        request_id,
        {
            "supportedVersions": SUPPORTED_VERSIONS,
            "capabilities": {"tools": {}},
            "instructions": (
                "Billing tools for one customer account. Invoice ids look like "
                "inv_000042. The account is chosen by the server from the calling "
                "user's entitlements and cannot be passed in."
            ),
            "ttlMs": 3600000,
            # No user-specific data in this result, so a shared cache is fine.
            "cacheScope": "public",
        },
    )


def handle_tools_list(request_id: Any, caller: str) -> dict[str, Any]:
    allowed = ENTITLEMENTS.get(caller, {}).get("tools", set())
    visible = [tool for tool in TOOLS if tool["name"] in allowed]
    return result_response(
        request_id,
        {
            "tools": visible,
            "ttlMs": 300000,
            # The list varies by caller, so a shared intermediary must not reuse
            # it across authorization contexts.
            "cacheScope": "private",
        },
    )


def handle_tools_call(request_id: Any, caller: str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        return error_response(request_id, INVALID_PARAMS, "params.name must be a string")

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return error_response(request_id, INVALID_PARAMS, "params.arguments must be an object")

    if name not in TOOLS_BY_NAME:
        # Not finding the tool is a protocol error, not a tool execution error.
        audit(caller, name, arguments, "error", "unknown tool")
        return error_response(request_id, INVALID_PARAMS, f"Unknown tool: {name}")

    try:
        check_tool_allowed(caller, name)
        payload = HANDLERS[name](caller, arguments)
    except Denied as denied:
        audit(caller, name, arguments, "denied", str(denied))
        # A denial is something the model can react to, so it is a tool
        # execution error with isError: true, not a JSON-RPC error.
        return result_response(
            request_id,
            {
                "content": [{"type": "text", "text": f"Denied: {denied}"}],
                "isError": True,
            },
        )

    audit(caller, name, arguments, "allowed", "ok")
    return result_response(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
            "structuredContent": payload,
            "isError": False,
        },
    )


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")

    if request_id is None:
        # A notification. This server has nothing to do with any of them.
        return None
    if not isinstance(method, str):
        return error_response(request_id, INVALID_REQUEST, "method must be a string")

    params = message.get("params") or {}
    if not isinstance(params, dict):
        return error_response(request_id, INVALID_PARAMS, "params must be an object")

    problem = check_request_meta(params)
    if problem is not None:
        return error_response(request_id, INVALID_PARAMS, problem)

    requested = params["_meta"][PROTOCOL_VERSION_KEY]
    if requested not in SUPPORTED_VERSIONS:
        return error_response(
            request_id,
            UNSUPPORTED_PROTOCOL_VERSION,
            f"Unsupported protocol version: {requested}",
            {"supported": SUPPORTED_VERSIONS, "requested": requested},
        )

    caller = calling_identity()

    if method == "server/discover":
        return handle_discover(request_id)
    if method == "tools/list":
        return handle_tools_list(request_id, caller)
    if method == "tools/call":
        return handle_tools_call(request_id, caller, params)
    return error_response(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            send(error_response(None, PARSE_ERROR, f"Parse error: {exc}"))
            continue
        if not isinstance(message, dict):
            send(error_response(None, INVALID_REQUEST, "message must be an object"))
            continue
        try:
            response = dispatch(message)
        except Exception as exc:  # noqa: BLE001 - never let one bad call kill the loop
            send(error_response(message.get("id"), INTERNAL_ERROR, f"Internal error: {exc}"))
            continue
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()
