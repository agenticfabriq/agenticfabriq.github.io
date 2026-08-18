"""The same delegated call routed through a control layer instead of a token store.

    pip install agentic-fabriq-sdk
    afctl auth login
    afctl agents list

The shape to notice: the agent process holds one token for the gateway, and the
per-user delegation lives in the layer. Connection names, method names, and
response keys are per-deployment, so run `afctl tools list` against your own
gateway rather than trusting the literals below.
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # Which agent identities exist, separately from any user.
        for agent in await af.list_agents():
            print(agent)

        # The delegation is a parameter of the call, not a property of the
        # process: the subject travels with the request, so two callers reach
        # the same connection and get different data back.
        for subject in (os.environ["USER_A_OID"], os.environ["USER_B_OID"]):
            result = await af.invoke_connection(
                "sharepoint_docs",
                method="search_files",
                parameters={"query": "quarterly margin", "on_behalf_of": subject},
            )
            print(subject, result)


asyncio.run(main())
