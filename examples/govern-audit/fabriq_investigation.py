"""Run the same investigation against a control layer instead of your own store.

    pip install agentic-fabriq-sdk
    export AF_TOKEN=...
    python3 fabriq_investigation.py

The point of this file is not that it is shorter than emit_record.py plus
schema.sql. It is that nothing in it emits a record. The records already exist,
because the calls routed through the layer that wrote them, so an agent that
forgot to log is not a category of bug that can occur here.

Connection names, method names, and response shapes are per deployment. Run
`afctl tools list` against your own gateway rather than trusting the literals
below -- `audit_store`, `query_actions`, and the `records` response key are
placeholders for whatever your deployment actually exposes.
"""

from __future__ import annotations

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"

AGENT = "agent:billing-triage@v7"
PRINCIPAL = "user:ada@example.com"
WINDOW_START = "2026-08-18T00:00:00Z"
WINDOW_END = "2026-08-19T00:00:00Z"


async def main() -> None:
    async with FabriqClient(
        base_url=BASE_URL,
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # Start from the roster, not from a name you remembered. The agent
        # inventory is itself audit evidence: an agent nobody can name is an
        # agent nobody is reviewing.
        agents = await af.list_agents()
        print(f"{len(agents)} registered agents")

        # Same two principals and the same window as investigate.sql.
        result = await af.invoke_connection(
            "audit_store",
            method="query_actions",
            parameters={
                "agent_id": AGENT,
                "principal_id": PRINCIPAL,
                "from": WINDOW_START,
                "to": WINDOW_END,
            },
        )

        # NOT result.get("records", []). The docstring above says `records` is a
        # placeholder for whatever your deployment returns, and defaulting to an
        # empty list turns getting that literal wrong into the sentence "0
        # actions, 0 denied" -- which is indistinguishable from an agent that did
        # nothing, and is the one wrong answer an investigation must never give.
        # A lookup that finds nothing because it looked in the wrong place has
        # to say so.
        if "records" not in result:
            raise SystemExit(
                f"response carries no 'records' key (keys: {sorted(result)}). "
                "Run `afctl tools list` and use the key your deployment actually "
                "returns -- do not read this as an empty result."
            )

        records = result["records"]
        for record in records:
            print(
                record.get("occurred_at"),
                record.get("resource"),
                record.get("action"),
                record.get("decision"),
                record.get("outcome"),
                sep="\t",
            )

        # Denials are the half of the trail that a log written after a
        # successful call never contains. Count them explicitly, because a
        # window with zero denials means either a well-behaved agent or a
        # policy that is not evaluating anything, and you want to know which.
        denied = [r for r in records if r.get("decision") == "deny"]
        print(f"{len(records)} actions, {len(denied)} denied")


if __name__ == "__main__":
    asyncio.run(main())
