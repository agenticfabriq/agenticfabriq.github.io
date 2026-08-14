"""List unread Gmail messages through an Agentic Fabriq connection.

Requires: pip install agentic-fabriq-sdk
Set AF_TOKEN to a bearer token issued for the Fabriq gateway.
"""

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"
CONNECTION = "gmail_work"


async def main() -> None:
    token = os.environ["AF_TOKEN"]
    async with FabriqClient(base_url=BASE_URL, auth_token=token) as af:
        result = await af.invoke_connection(
            CONNECTION,
            method="get_emails",
            parameters={"max_results": 10, "q": "is:unread"},
        )
        for email in result.get("emails", []):
            print(email)


if __name__ == "__main__":
    asyncio.run(main())
