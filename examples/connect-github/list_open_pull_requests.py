"""Reach GitHub through a brokered connection instead of holding the app key.

The agent process holds a gateway token, not the GitHub App private key. The
control layer mints and rotates the installation token, evaluates policy per
request, and records which agent acted for which human.

    pip install agentic-fabriq-sdk

Connection names and method names are per-deployment. Run `afctl tools list`
against your own gateway rather than copying the names below.
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
        result = await af.invoke_connection(
            "github_platform",
            method="list_pull_requests",
            parameters={"repository": "acme/billing-service", "state": "open"},
        )
        for pull_request in result.get("pull_requests", []):
            print(pull_request)


asyncio.run(main())
