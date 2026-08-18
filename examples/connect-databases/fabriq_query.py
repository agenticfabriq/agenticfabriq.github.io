"""The same two tools, routed through a control layer instead of a DSN.

    pip install agentic-fabriq-sdk
    afctl auth login
    afctl tools list          # connection and method names are per deployment
    export AF_TOKEN=...
    python3 fabriq_query.py 1

The difference from query_tools.py is not the shorter code. It is that the
agent process never holds the database password, and the acting user's id is a
parameter of the call rather than something the agent is trusted to set on the
session itself.
"""

from __future__ import annotations

import asyncio
import os
import sys

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # Do not copy the connection or method names below. List what your own
        # gateway actually exposes and use those.
        for tool in await af.list_tools():
            print(tool)

        summary = await af.invoke_connection(
            "orders_db",
            method="order_summary",
            parameters={"acting_user_id": user_id},
        )
        print(summary)

        shipped = await af.invoke_connection(
            "orders_db",
            method="orders_by_status",
            parameters={"acting_user_id": user_id, "status": "shipped", "limit": 200},
        )
        print(shipped)


if __name__ == "__main__":
    asyncio.run(main())
