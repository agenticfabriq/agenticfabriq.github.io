#!/usr/bin/env python3
"""An agent that holds no resource credential at all.

Companion to the guide "Credential Vaulting and Rotation for Agents".

The agent authenticates to the control layer with one token and calls a named
connection. The credential for the underlying system — the database password,
the OAuth refresh token, the API key — is held by the control layer and never
enters this process, so rotating it changes nothing here and a compromise of
this process leaks a revocable gateway token instead of a standing grant.

Connection names, method names, and response shapes are per-deployment. Run
`afctl tools list` against your own gateway rather than trusting the literals
below.

    pip install agentic-fabriq-sdk
    afctl auth login
    python3 fabriq_no_credential.py
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # What this agent is allowed to reach, resolved server side per request
        # rather than baked into an environment variable at deploy time.
        for tool in await af.list_tools():
            print(tool)

        result = await af.invoke_connection(
            "orders_db",
            method="query",
            parameters={
                "sql": "SELECT id, status FROM orders WHERE customer_id = :customer LIMIT 20",
                "params": {"customer": "amara@example.com"},
            },
        )
        for row in result.get("rows", []):
            print(row)


if __name__ == "__main__":
    asyncio.run(main())
