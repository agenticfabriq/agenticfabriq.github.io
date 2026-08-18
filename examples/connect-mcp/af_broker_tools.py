#!/usr/bin/env python3
"""Call a brokered tool through Agentic Fabriq instead of holding the credential.

    pip install agentic-fabriq-sdk
    afctl auth login
    AF_TOKEN=... python3 examples/connect-mcp/af_broker_tools.py

The agent holds a token for the control layer, not for the billing system. The
policy decision -- may this agent, acting for this user, run this method with
these parameters -- happens in the layer, and the call is attributed to both.

Connection names, method names, and response shapes are per-deployment. Run
`afctl tools list` against your own gateway rather than copying the literals
below.
"""

from __future__ import annotations

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # What this agent is actually permitted to reach, decided centrally.
        for tool in await af.list_tools():
            print(tool)

        result = await af.invoke_connection(
            "billing",
            method="lookup_invoice",
            parameters={"invoice_id": "inv_000042"},
        )
        print(result)


asyncio.run(main())
