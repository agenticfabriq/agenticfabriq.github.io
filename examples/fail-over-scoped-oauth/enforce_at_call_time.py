"""Call-time enforcement through Agentic Fabriq, and what it does not fix.

The over-granted scope still exists on the provider. What changes is where the
credential lives: the agent holds a token for the control layer rather than the
provider's refresh token, calls a named connection, and the layer evaluates a
policy per request. A call the policy declines does not reach the provider even
though the OAuth grant would have permitted it.

That is mitigation, not narrowing. Anything that reaches the provider without
passing this layer -- a leaked refresh token, a script written against the raw
API, a second integration sharing the client id, a bypass added during an
outage -- recovers the full scope. Enforcement narrows what happens; only
revocation narrows what is possible. Use this to buy the time to run
narrow_google_scope.py, not instead of running it.

Requires: pip install agentic-fabriq-sdk
Set AF_TOKEN to a bearer token issued for the Fabriq gateway.
Set MEETING_DOC_ID to the file the user actually selected.
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"

# Connection names, method names, parameter names and response shapes are all
# per-deployment. Check yours with `afctl tools list` rather than copying
# "drive_meeting_docs" or "get_file" from this file on faith.
CONNECTION = "drive_meeting_docs"


async def main() -> None:
    token = os.environ["AF_TOKEN"]
    async with FabriqClient(base_url=BASE_URL, auth_token=token) as af:
        # The inventory step 2 of the audit needs, without an admin credential:
        # what this agent's token can reach at all.
        tools = await af.list_tools()
        print(f"reachable connections/tools: {len(tools)}")
        for tool in tools:
            print(f"  {tool}")

        # The agent asks for one file by id -- the one the user selected. The
        # policy layer decides whether this agent, acting for this user, may
        # read it. The Drive grant is not consulted by the agent at all,
        # because the agent does not hold it.
        result = await af.invoke_connection(
            CONNECTION,
            method="get_file",
            parameters={"file_id": os.environ["MEETING_DOC_ID"]},
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
