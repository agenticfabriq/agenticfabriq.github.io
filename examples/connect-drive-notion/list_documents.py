"""The brokered version of the same two reads.

The Drive refresh token and the Notion connection secret stay in the control
layer. This process holds one gateway token, which is revocable in one place
and attributable to this agent and the user it acted for.

Connection names, method names, and response shapes are per-deployment. Run
`afctl tools list` against your own gateway instead of trusting the names
below -- "drive_docs", "notion_handbook", "list_files", and "search" are
placeholders for whatever your deployment actually exposes.

Install:
    pip install agentic-fabriq-sdk

Environment:
    AF_TOKEN    gateway token for this agent

Run:
    afctl auth login
    afctl auth status
    python3 list_documents.py
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
        # What this agent is allowed to reach, according to the gateway rather
        # than according to a scope string the agent asked for itself.
        for tool in await af.list_tools():
            print(tool)

        drive_files = await af.invoke_connection(
            "drive_docs",
            method="list_files",
            parameters={"page_size": 25, "q": "trashed = false"},
        )
        for item in drive_files.get("files", []):
            print("drive:", item)

        notion_pages = await af.invoke_connection(
            "notion_handbook",
            method="search",
            parameters={"page_size": 25},
        )
        for item in notion_pages.get("results", []):
            print("notion:", item)


asyncio.run(main())
