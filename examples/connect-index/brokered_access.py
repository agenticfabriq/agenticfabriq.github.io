#!/usr/bin/env python3
"""Brokered access: the agent holds a gateway token, not a provider credential.

    pip install agentic-fabriq-sdk
    afctl auth login
    AF_TOKEN=... python3 examples/connect-index/brokered_access.py

This is the fourth access pattern from the Connect cornerstone. The agent
process holds no Gmail refresh token, no Slack bot token, and no database
password. It holds one token for the control layer, names a connection, and
the layer resolves the credential, evaluates policy for this specific request,
and records the call against the agent and the user it acted for.

Caveats, both of which the guide states as well:

* `af_sdk.fabriq_client` comes from the SDK's published documentation, and the
  published package has lagged its own README. Check the module exists in the
  version you installed before writing code against it:

      python -c "import af_sdk.fabriq_client"

* Connection names, method names, and response shapes are per-deployment. Run
  `afctl tools list` against your own gateway rather than trusting the
  "support_mail", "get_emails", or "emails" literals below.
"""

from __future__ import annotations

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"
CONNECTION = "support_mail"


async def main() -> None:
    async with FabriqClient(
        base_url=BASE_URL,
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # What this agent is permitted to reach, decided centrally rather than
        # by whatever credentials happen to be in its environment.
        for tool in await af.list_tools():
            print(tool)

        # Agents are first-class objects here, which is what makes "which agent
        # acted for which user" answerable from one place.
        for agent in await af.list_agents():
            print(agent)

        result = await af.invoke_connection(
            CONNECTION,
            method="get_emails",
            parameters={"max_results": 10, "q": "is:unread"},
        )
        for message in result.get("emails", []):
            print(message)


if __name__ == "__main__":
    asyncio.run(main())
