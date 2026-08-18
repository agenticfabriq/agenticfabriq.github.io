"""Calling a provider through a control layer, so the quota lives at the route.

    pip install agentic-fabriq-sdk
    afctl auth login
    afctl agents list
    export AF_TOKEN="..."
    python3 fabriq_quotas.py

The local controls in guardrails.py bound one process. The moment the same agent
runs in forty workers, forty private budgets bound nothing -- the provider still
sees forty times the traffic. Moving the call through a layer moves the counter
to the one place every call passes through.

What this file does NOT do is tell you the response shape. Connection names,
method names, quota error payloads, and result keys are per-deployment. Run
`afctl tools list` against your own gateway and branch on what it prints, rather
than on the strings below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"
AGENT_ID = "invoice-settler"
USER_ID = "user_42"


def operation_key(*, agent_id: str, user_id: str, operation: str, params: dict[str, Any]) -> str:
    """The same stable key as duplicate_writes.py, and for the same reason.

    Routing a write through a layer does not make it idempotent. The key still
    has to identify the operation rather than the attempt, and it still has to
    reach the downstream provider.
    """
    fingerprint = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]
    return f"{agent_id}:{user_id}:{operation}:{fingerprint}"


async def main() -> None:
    invoice = {"invoice": "inv_2026_0042", "amount_cents": 12_900}
    key = operation_key(
        agent_id=AGENT_ID, user_id=USER_ID, operation="settle", params=invoice
    )

    async with FabriqClient(
        base_url=BASE_URL,
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # Which agents exist and what each is allowed to reach is the layer's
        # view, not the process's. It is also where a stopped agent shows as
        # stopped, which is why the check is worth making at the start of a run.
        agents = await af.list_agents()
        print(f"agents registered with the layer: {agents}")

        # Discover the connection surface instead of assuming it. If the method
        # your code calls is not in here, fail now rather than in a retry loop.
        tools = await af.list_tools()
        print(f"tools this token can reach: {tools}")

        result = await af.invoke_connection(
            "billing_prod",
            method="create_charge",
            parameters={**invoice, "idempotency_key": key},
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
