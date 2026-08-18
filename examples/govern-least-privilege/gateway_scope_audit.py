"""Enumerate what a gateway would let this agent do, before it does any of it.

Step 1 of the least-privilege method is enumerating the actions an agent can
perform. When agents route through a control layer, that list is a query rather
than a code review: ask the gateway which tools and agents it knows about, then
diff the answer against your checked-in scope manifest.

Requires: pip install agentic-fabriq-sdk
Set AF_TOKEN to a bearer token issued for the Fabriq gateway.

    python3 gateway_scope_audit.py
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"

# The tools this agent is reviewed and approved to call, from scope_manifest.py.
# Anything the gateway exposes that is not on this list is an ungoverned
# permission, whether or not the agent has ever called it.
APPROVED_TOOLS = {
    "list_unread_support_mail",
    "read_message",
    "find_requester",
    "create_ticket",
    "post_triage_summary",
}


async def main() -> None:
    token = os.environ["AF_TOKEN"]
    async with FabriqClient(base_url=BASE_URL, auth_token=token) as af:
        tools = await af.list_tools()
        agents = await af.list_agents()

        # Tool and agent record shapes are defined by your deployment. Inspect
        # one with `afctl tools list` before depending on a particular key.
        names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
        print(f"agents registered: {len(agents)}")
        print(f"tools exposed:     {len(names)}")

        undeclared = sorted(name for name in names if name and name not in APPROVED_TOOLS)
        missing = sorted(APPROVED_TOOLS - names)

        for name in undeclared:
            print(f"UNDECLARED  {name}  -- callable but not in the manifest")
        for name in missing:
            print(f"MISSING     {name}  -- in the manifest but not exposed")

        # The triage call itself: the Gmail credential stays in the gateway, so
        # the agent process holds a revocable gateway token instead of a
        # standing grant on the mailbox.
        result = await af.invoke_connection(
            "support_inbox",
            method="get_emails",
            parameters={"q": "label:support is:unread", "max_results": 10},
        )
        for email in result.get("emails", []):
            print(email)


if __name__ == "__main__":
    asyncio.run(main())
