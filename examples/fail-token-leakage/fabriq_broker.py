"""The broker from brokered_agent.py, as a service instead of an object.

    pip install agentic-fabriq-sdk
    afctl auth login
    afctl tools list          # connection and method names are per deployment
    export AF_TOKEN=...
    python3 fabriq_broker.py

Unlike the rest of this directory, this file opens a real connection to a real
gateway. It is a reference sample, not part of the leak demonstration.

The property that matters is the same one brokered_agent.py asserts: the
billing credential is not in this process, so it cannot be in the context
window, in a captured span, in a stack trace, or in the environment a
subprocess inherits. What the agent holds is AF_TOKEN, which is a credential
too — one you can revoke in one place, scoped to the gateway rather than to the
provider, and attributable to this agent in the gateway's audit log.

Connection names, method names, and response shapes are per deployment. Run
`afctl tools list` against your own gateway rather than copying "billing" or
"get_invoice" from here.
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
        # Names, which are all the model is ever shown.
        for tool in await af.list_tools():
            print(tool)

        invoice = await af.invoke_connection(
            "billing",
            method="get_invoice",
            parameters={"invoice_id": "INV-4417"},
        )
        print(invoice)


asyncio.run(main())
