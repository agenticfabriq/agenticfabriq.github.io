"""The lifecycle question, asked of the control layer rather than of a wiki.

    pip install agentic-fabriq-sdk
    export AF_TOKEN=...
    python3 examples/fail-index/fabriq_inventory.py

Two things decay silently in an agent estate: an agent whose owner left, and a
tool that is still callable after the workflow that needed it was retired.
Neither produces an error, so neither is found by anything except a deliberate
diff against a list of what is supposed to exist.

`OWNERS` is that list. Keep it in the repository, review it like a permission
change, and let this script tell you where reality has drifted from it.

Note on the SDK: the published `agentic-fabriq-sdk` package has lagged its own
README on the `af_sdk.dx` and `af_sdk.fabriq_client` module paths, so check the
import against the version you installed before assuming this file is wrong.
Connection, agent, and tool identifiers are per deployment - run
`afctl tools list` and `afctl agents list` against your own gateway rather than
copying anything below.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from af_sdk.fabriq_client import FabriqClient

# The declared estate: every agent that is supposed to be running, and the
# human accountable for it. A row with no current owner is a lifecycle finding.
OWNERS: dict[str, str] = {
    "meeting-brief": "dana@example.com",
    "support-triage": "ravi@example.com",
}

# Humans who have left. In a real deployment this comes from the directory,
# which is the only source that is authoritative about it.
DEPARTED: frozenset[str] = frozenset({"ravi@example.com"})


def _name(item: Any) -> str | None:
    """Response shapes are per deployment: a dict with a name, or a bare string."""
    if isinstance(item, dict):
        value = item.get("name") or item.get("id")
        return value if isinstance(value, str) else None
    return item if isinstance(item, str) else None


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        agents = await af.list_agents()
        tools = await af.list_tools()

        # Drop the entries with no usable name rather than letting None into a
        # sorted() call, which raises comparing None to str.
        live_agents = sorted(n for n in (_name(agent) for agent in agents) if n)
        live_tools = sorted(n for n in (_name(tool) for tool in tools) if n)

        print(f"{len(live_agents)} agents registered, {len(live_tools)} tools reachable")

        for agent in live_agents:
            owner = OWNERS.get(agent)
            if owner is None:
                print(f"UNOWNED   {agent}  -- registered and callable, named by nobody")
            elif owner in DEPARTED:
                print(f"ORPHANED  {agent}  -- owner {owner} has left; who stops it?")

        for agent in sorted(OWNERS.keys() - set(live_agents)):
            print(f"RETIRED   {agent}  -- declared but not registered; delete the row")


asyncio.run(main())
