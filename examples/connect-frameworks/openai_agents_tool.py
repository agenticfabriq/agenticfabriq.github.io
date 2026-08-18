"""post_message as an OpenAI Agents SDK tool, credential resolved per invocation.

Written against openai-agents 0.21.1.

Requires: pip install openai-agents httpx
Set AGENT_MODEL to a model identifier your provider accepts, and
ACTING_USER_ID to the user the agent is acting for.

The identity seam is `RunContextWrapper[UserContext]` as the first parameter.
The SDK drops it from the JSON schema, and the context object is never sent to
the model, so it cannot be read out of the transcript or overwritten by it.

Import the decorator as `function_tool`. `from agents import tool` resolves to
the `agents.tool` module, not the decorator; `from agents.decorators import
tool` is the same object under a shorter name.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from agents import (
    Agent,
    RunConfig,
    RunContextWrapper,
    Runner,
    ToolExecutionConfig,
    function_tool,
)

from channel_api import model_safe_text, post_message_async, resolve_token


@dataclass
class UserContext:
    """Passed to Runner.run(context=...). Local only; never reaches the model."""

    user_id: str


def safe_tool_error(ctx: RunContextWrapper[UserContext], error: Exception) -> str:
    """Replace the SDK default, which interpolates str(error) into the prompt."""
    return model_safe_text(error)


@function_tool(
    failure_error_function=safe_tool_error,
    needs_approval=True,
    # timeout is only honoured for async handlers; a sync one raises at
    # decoration time rather than silently running unbounded.
    timeout=15.0,
    timeout_behavior="error_as_result",
)
async def post_message_tool(
    ctx: RunContextWrapper[UserContext], channel: str, text: str
) -> str:
    """Post a message to a chat channel on behalf of the current user.

    Args:
        channel: Channel id, for example C123ABC456.
        text: The message body to post.
    """
    token = resolve_token(ctx.context.user_id)
    ts = await post_message_async(token, channel, text)
    return f"posted to {channel} at {ts}"


def build_agent() -> Agent[UserContext]:
    """The agent is stateless with respect to identity, so one is enough."""
    return Agent[UserContext](
        name="Release announcer",
        instructions="Post short, factual deploy notes to the channel you are asked about.",
        model=os.environ["AGENT_MODEL"],
        tools=[post_message_tool],
    )


# Without this the SDK starts every function tool call in a turn at once. One
# in flight at a time is the right default for a tool that writes.
RUN_CONFIG = RunConfig(
    tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
)


async def main() -> None:
    agent = build_agent()
    context = UserContext(user_id=os.environ["ACTING_USER_ID"])
    result = await Runner.run(
        agent,
        "Tell C123ABC456 that deploy 41 is live.",
        context=context,
        max_turns=8,
        run_config=RUN_CONFIG,
    )

    # needs_approval stops the run before the tool executes and hands back the
    # pending calls. Show them to a human, decide, then resume from the state.
    while result.interruptions:
        state = result.to_state()
        for item in result.interruptions:
            print("approval needed:", item)
            state.approve(item)  # state.reject(item) is the other branch
        result = await Runner.run(
            agent, state, context=context, max_turns=8, run_config=RUN_CONFIG
        )

    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
