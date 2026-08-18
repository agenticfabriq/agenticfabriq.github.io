"""post_message as a CrewAI tool, with the credential resolved per invocation.

Written against crewai 1.15.16.

Requires: pip install crewai httpx
Set ACTING_USER_ID to the user the agent is acting for.

CrewAI has no per-invocation context object: `_run` receives the validated
arguments and nothing else. So the identity seam is the tool *instance*. Build
the tool, the agent, and the crew per request, and hand the tool a resolver
rather than a token, so nothing long-lived ever holds a user's credential.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Type

from crewai import Agent, Crew, Task
from crewai.tools import BaseTool, ToolFailure, ToolFailurePolicy
from pydantic import BaseModel, Field, PrivateAttr

from channel_api import (
    MODEL_SAFE_CODES,
    ChannelError,
    model_safe_text,
    post_message,
    resolve_token,
)


class PostMessageInput(BaseModel):
    """Arguments the model is allowed to choose."""

    channel: str = Field(description="Channel id, for example C123ABC456.")
    text: str = Field(description="The message body to post.")


class PostMessageTool(BaseTool):
    name: str = "post_message"
    description: str = "Post a message to a chat channel on behalf of the current user."
    args_schema: Type[BaseModel] = PostMessageInput

    # A private attribute, so the resolver is never part of the tool's public
    # pydantic schema and cannot be dumped into a log by model_dump().
    _resolve_token: Callable[[], str] = PrivateAttr()

    def __init__(self, resolve_token: Callable[[], str], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._resolve_token = resolve_token

    def _run(self, channel: str, text: str) -> str | ToolFailure:
        try:
            token = self._resolve_token()
            ts = post_message(token, channel, text)
        except ChannelError as exc:
            # Returning a ToolFailure instead of raising is what stops the six
            # executions described in the guide: CrewAI only retries a call
            # that raised. A returned failure is recorded once and handed to
            # the model as text.
            return ToolFailure(
                message=model_safe_text(exc),
                code=exc.code if exc.code in MODEL_SAFE_CODES else None,
                retryable=exc.retryable,
            )
        except Exception as exc:  # noqa: BLE001 - nothing below re-raises
            return ToolFailure(message=model_safe_text(exc), retryable=False)
        return f"posted to {channel} at {ts}"


def build_crew(user_id: str) -> Crew:
    """One crew per request. Nothing here is safe to cache across users."""
    tool = PostMessageTool(resolve_token=lambda: resolve_token(user_id))
    agent = Agent(
        role="Release announcer",
        goal="Tell the right channel what shipped.",
        backstory="You post short, factual deploy notes.",
        tools=[tool],
        # Bound the ReAct loop. max_iter is per task, not per tool.
        max_iter=8,
        max_execution_time=120,
        # 'raise' turns a reported tool failure into ToolExecutionFailedError
        # instead of letting the agent narrate its way past it. The default is
        # 'warn', which records the failure and keeps going.
        tool_failure_policy=ToolFailurePolicy.RAISE,
    )
    task = Task(
        description="Tell #ops that deploy 41 is live.",
        expected_output="A one-line confirmation containing the message timestamp.",
        agent=agent,
        # Task-level review of the final answer. CrewAI has no per-tool-call
        # approval hook, so this fires after the post has already happened.
        human_input=False,
    )
    return Crew(agents=[agent], tasks=[task])


def main() -> None:
    crew = build_crew(os.environ["ACTING_USER_ID"])
    print(crew.kickoff())


if __name__ == "__main__":
    main()
