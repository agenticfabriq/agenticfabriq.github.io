"""post_message as a LangChain tool, with the credential resolved per invocation.

Written against langchain 1.3.15, langchain-core 1.5.6, langgraph 1.2.11.

Requires: pip install langchain httpx
Set AGENT_MODEL to a model identifier your provider accepts, and
ACTING_USER_ID to the user the agent is acting for.

The identity seam is `ToolRuntime[UserContext]`. That parameter is stripped
from the schema the model sees, so `user_id` is not something the model can
set, guess, or be argued into changing.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
    wrap_tool_call,
)
from langchain.messages import ToolMessage
from langchain.tools import ToolRuntime, tool
from langchain.tools.tool_node import ToolCallRequest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from channel_api import ChannelError, model_safe_text, post_message, resolve_token


@dataclass
class UserContext:
    """Passed to .invoke(context=...). Never serialised into the prompt."""

    user_id: str


@tool(parse_docstring=True)
def post_message_tool(channel: str, text: str, runtime: ToolRuntime[UserContext]) -> str:
    """Post a message to a chat channel on behalf of the current user.

    Args:
        channel: Channel id, for example C123ABC456.
        text: The message body to post.
    """
    token = resolve_token(runtime.context.user_id)
    ts = post_message(token, channel, text)
    return f"posted to {channel} at {ts}"


@wrap_tool_call
def sanitise_tool_errors(
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage],
) -> ToolMessage:
    """Turn an exception into a string this repository wrote.

    Without it, langchain 1.3's default handler re-raises anything that is not
    a ToolInvocationError, so the exception escapes agent.invoke() and takes
    the run with it. Handing the error handler a blanket `True` instead would
    put str(exception) in front of the model, which is the other failure.
    """
    try:
        return handler(request)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; nothing re-raises
        return ToolMessage(
            content=model_safe_text(exc),
            tool_call_id=request.tool_call["id"],
            status="error",
        )


def build_agent():
    """Assemble the agent. The tool object is process-wide; the identity is not."""
    return create_agent(
        model=os.environ["AGENT_MODEL"],
        tools=[post_message_tool],
        context_schema=UserContext,
        # HumanInTheLoopMiddleware suspends the graph with interrupt(), which
        # needs somewhere to keep the suspended state. Swap InMemorySaver for a
        # durable checkpointer once approvals outlive the process.
        checkpointer=InMemorySaver(),
        middleware=[
            # Earlier entries wrap later ones, so retry sits closest to the
            # tool and sanitise only sees what survived every attempt.
            sanitise_tool_errors,
            # The default retry_on is (Exception,), which would happily repeat
            # a post that already landed. Name the retryable failures.
            ToolRetryMiddleware(
                max_retries=2,
                tools=["post_message_tool"],
                retry_on=lambda exc: isinstance(exc, ChannelError) and exc.retryable,
            ),
            ToolCallLimitMiddleware(tool_name="post_message_tool", run_limit=5),
            HumanInTheLoopMiddleware(
                interrupt_on={"post_message_tool": True},
                description_prefix="Posting to a channel requires approval",
            ),
        ],
    )


def main() -> None:
    agent = build_agent()
    context = UserContext(user_id=os.environ["ACTING_USER_ID"])
    config = {"configurable": {"thread_id": "frameworks-demo"}}

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "Tell #ops that deploy 41 is live."}]},
        config=config,
        context=context,
    )

    # The first invoke returns before the tool runs, carrying the pending
    # approval. Show it to a human, then resume with their decision. The other
    # decision types are "edit", "reject", and "respond".
    for interrupt in result.get("__interrupt__", []):
        for request in interrupt.value["action_requests"]:
            print("approval needed:", request["name"], request["args"])

    result = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
        context=context,
    )
    for message in result["messages"]:
        print(type(message).__name__, message.content)


if __name__ == "__main__":
    main()
