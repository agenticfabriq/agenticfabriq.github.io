"""Ask the control layer the four questions the matrix is made of.

    pip install agentic-fabriq-sdk
    export AF_TOKEN=...
    python3 fabriq_control_plane.py

The interesting property is not that this file is shorter than
permission_matrix.py. It is which axis each call answers. `list_agents()` is
the agent axis -- the roster of principals, which is the inventory an access
review starts from. `list_tools()` is the action axis, and diffing it against
your own manifest is how you find a tool that became callable without a scope
diff. `invoke_connection()` carries the subject, so the user axis travels with
the request rather than being decided once when a credential was configured.

Two caveats before you copy anything here:

* The published package has at times lagged its own README on the module
  layout, so confirm `from af_sdk.fabriq_client import FabriqClient` against
  the version you actually install rather than against this file.
* Connection names, method names, parameter names, and response keys are all
  per-deployment. Run `afctl tools list` against your own gateway; `runbooks`,
  `read_file`, and the `content` key below are placeholders.
"""

from __future__ import annotations

import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

BASE_URL = "https://dashboard.agenticfabriq.com"

RUNBOOK = "/runbooks/payments.docx"


async def main() -> None:
    async with FabriqClient(
        base_url=BASE_URL,
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # The agent axis: every non-human principal that can act at all.
        agents = await af.list_agents()
        print(f"{len(agents)} registered agents")

        # The action axis: everything callable, which is the permission
        # surface whether or not anyone reviewed it as one.
        tools = await af.list_tools()
        for tool in tools:
            print(tool)

        # The user axis: the same connection, the same resource, two subjects.
        # Whether each one comes back with content is the resource's decision
        # about that human, not this process's decision about the agent.
        for subject in (os.environ["USER_A_OID"], os.environ["USER_B_OID"]):
            result = await af.invoke_connection(
                "runbooks",
                method="read_file",
                parameters={"path": RUNBOOK, "on_behalf_of": subject},
            )
            print(subject, bool(result.get("content")))


if __name__ == "__main__":
    asyncio.run(main())
