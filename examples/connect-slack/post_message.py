"""Post to Slack through an Agentic Fabriq connection instead of holding xoxb.

Requires: pip install agentic-fabriq-sdk
Set AF_TOKEN to a bearer token issued for the Fabriq gateway.

Connection names and method names are per-deployment. Run `afctl tools list`
against your own gateway to see what yours are actually called rather than
trusting the literals below.
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"
CONNECTION = "slack"


async def main() -> None:
    token = os.environ["AF_TOKEN"]
    async with FabriqClient(base_url=BASE_URL, auth_token=token) as af:
        channels = await af.invoke_connection(CONNECTION, method="get_channels", parameters={})
        for channel in channels.get("channels", []):
            print(channel)

        await af.invoke_connection(
            CONNECTION,
            method="post_message",
            parameters={
                "channel": os.environ["SLACK_CHANNEL_ID"],
                "text": "Acknowledged. A human is picking this up.",
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
