"""Call a downstream connection without the agent holding the user's token.

Requires: pip install agentic-fabriq-sdk
Set AF_TOKEN to a bearer token issued for the Fabriq gateway.

The agent process here never sees the upstream OAuth access token, never
performs an RFC 8693 exchange itself, and never stores a refresh token. It
presents its own gateway token and names a connection; the control layer holds
the provider credential and decides what this agent may do for this user.
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"
CONNECTION = "billing_readonly"


async def main() -> None:
    async with FabriqClient(base_url=BASE_URL, auth_token=os.environ["AF_TOKEN"]) as af:
        # Connection names, method names, and response shapes are defined by
        # your own deployment. Run `afctl tools list` against your gateway
        # instead of trusting "billing_readonly" or "list_invoices" here.
        tools = await af.list_tools()
        print(f"{len(tools)} tools available to this agent token")

        result = await af.invoke_connection(
            CONNECTION,
            method="list_invoices",
            parameters={"customer_id": "cus_example", "limit": 5},
        )
        for invoice in result.get("invoices", []):
            print(invoice)


asyncio.run(main())
