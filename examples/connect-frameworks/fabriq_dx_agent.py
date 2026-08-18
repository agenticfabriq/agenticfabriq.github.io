"""The same tool, with the credential resolved by a control layer instead.

Requires: pip install agentic-fabriq-sdk
Set AF_TOKEN to a bearer token issued for the Fabriq gateway and AF_TENANT to
your tenant id. Authenticate the CLI first with `afctl auth login`.

Nothing in this file holds a chat credential. The agent holds a token for the
gateway; the gateway holds the per-user grant and decides, per request, whether
this agent may act for this user against this connection.

The `af_sdk.dx` import path and the keyword names below come from the SDK's
published README. Confirm them against your installed version before copying:
`afctl auth status` and `afctl tools list` will tell you what your gateway
actually exposes, and connection names are per-deployment.
"""

from __future__ import annotations

import asyncio
import os

from af_sdk.dx import Agent, AgentFabric, ToolFabric, tool
from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"


@tool
def format_deploy_note(release: str, status: str) -> str:
    """Render the announcement text. Local logic stays local."""
    return f"Deploy {release} is {status}."


async def discover() -> None:
    """List what this token is allowed to reach, before wiring anything to it.

    Run this first. The set it prints is the set of connections your policy
    already permits; anything missing is a policy change, not a code change.
    """
    async with FabriqClient(
        base_url=BASE_URL,
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        for entry in await af.list_tools():
            print("tool:", entry)
        for entry in await af.list_agents():
            print("agent:", entry)


def build_bot() -> Agent:
    """Compose an agent whose chat access is a fabric, not a token in memory."""
    token = os.environ["AF_TOKEN"]
    tenant = os.environ["AF_TENANT"]

    chat = ToolFabric(
        provider="slack",
        base_url=BASE_URL,
        access_token=token,
        tenant_id=tenant,
    )
    agents = AgentFabric(base_url=BASE_URL, access_token=token, tenant_id=tenant)

    return Agent(
        system_prompt="Post short, factual deploy notes to the channel you are asked about.",
        tools=[format_deploy_note],
        agents=agents.get_agents(["summarizer"]),
        base_url=BASE_URL,
        access_token=token,
        tenant_id=tenant,
        provider_fabrics={"slack": chat},
    )


async def post_directly(user_channel: str, text: str) -> dict:
    """The low-level path: name a connection and a method, carry no secret.

    Connection name, method name, and response shape are defined by your
    deployment. Check yours with `afctl tools list` rather than assuming
    "my-slack" or "post_message".
    """
    async with FabriqClient(
        base_url=BASE_URL,
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        return await af.invoke_connection(
            "my-slack",
            method="post_message",
            parameters={"channel": user_channel, "text": text},
        )


def main() -> None:
    asyncio.run(discover())
    print(build_bot().run("Tell the ops channel that deploy 41 is live."))


if __name__ == "__main__":
    main()
