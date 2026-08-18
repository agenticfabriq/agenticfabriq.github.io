"""The scope manifest for a support-triage agent, and the check that gates it.

Every granted scope is one row. A row that cannot name the tool that needs it,
the API call that needs it, and a written reason does not get merged. Run this
in CI so widening a grant is a reviewed diff rather than a console click:

    python3 scope_manifest.py            # print the review table
    python3 scope_manifest.py --check    # exit 1 if any row is unjustified

No third-party dependencies. Scope strings are the providers' own literals; see
the guide's Further reading for the page each one comes from.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Grant:
    """One scope, and the evidence that earned it."""

    provider: str
    scope: str
    tools: tuple[str, ...]
    calls: tuple[str, ...]
    reason: str
    added: str
    # Set when the scope is wider than the action needs and nothing narrower
    # exists. The compensating control is what actually bounds the grant.
    over_grant: str = ""
    resource_limit: str = ""
    reviewers: tuple[str, ...] = field(default_factory=tuple)


MANIFEST: tuple[Grant, ...] = (
    Grant(
        provider="google",
        scope="https://www.googleapis.com/auth/gmail.readonly",
        tools=("list_unread_support_mail", "read_message"),
        calls=("gmail.users.messages.list", "gmail.users.messages.get"),
        reason=(
            "Triage classifies on the message body, so gmail.metadata is not "
            "sufficient; the q= search parameter is also unavailable under "
            "gmail.metadata, and the list call filters with q=."
        ),
        resource_limit="q=label:support is:unread, enforced in list_unread_support_mail",
        added="2026-08-18",
        reviewers=("security-review",),
    ),
    Grant(
        provider="zendesk",
        scope="tickets:write",
        tools=("create_ticket",),
        calls=("POST /api/v2/tickets",),
        reason="Filing the ticket is the agent's output. No narrower Zendesk scope covers create-only.",
        over_grant=(
            "Zendesk write scopes cover POST, PUT and DELETE on the resource, so "
            "tickets:write also permits updating and deleting existing tickets."
        ),
        resource_limit="Gateway policy allows POST /api/v2/tickets only; PUT and DELETE are denied.",
        added="2026-08-18",
        reviewers=("security-review", "support-ops"),
    ),
    Grant(
        provider="zendesk",
        scope="users:read",
        tools=("find_requester",),
        calls=("GET /api/v2/users/search",),
        reason="A ticket needs a requester id; search by email returns it.",
        added="2026-08-18",
        reviewers=("security-review",),
    ),
    Grant(
        provider="slack",
        scope="chat:write",
        tools=("post_triage_summary",),
        calls=("chat.postMessage",),
        reason="Posts one summary per run to the on-call channel.",
        resource_limit=(
            "chat:write.public is deliberately NOT granted, so the bot can only "
            "post to channels it has been added to."
        ),
        added="2026-08-18",
        reviewers=("security-review",),
    ),
)

# Removed grants stay in the file. A deleted line is invisible in a later
# review; a struck-through line tells the next reader the question was asked.
RETIRED: tuple[tuple[str, str, str], ...] = (
    (
        "google",
        "https://www.googleapis.com/auth/gmail.modify",
        "Applying a label needs users.messages.modify, whose narrowest described "
        "scope is gmail.modify -- which also grants compose and send. Dropped the "
        "label_message tool instead. Google's method reference also lists "
        "gmail.modify.restricted; revisit if its grant is ever documented. "
        "Removed 2026-08-18.",
    ),
    (
        "slack",
        "channels:history",
        "Added for a channel-reading feature that was cut before launch. Zero "
        "calls in 30 days. Removed 2026-08-18.",
    ),
)


def unjustified(manifest: tuple[Grant, ...]) -> list[str]:
    """Return one message per row that a reviewer could not approve as written."""
    problems: list[str] = []
    for grant in manifest:
        where = f"{grant.provider}:{grant.scope}"
        if not grant.tools:
            problems.append(f"{where}: no tool needs it")
        if not grant.calls:
            problems.append(f"{where}: no API call named")
        if len(grant.reason.split()) < 6:
            problems.append(f"{where}: reason is not a reason")
        if not grant.reviewers:
            problems.append(f"{where}: nobody signed off")
        if grant.over_grant and not grant.resource_limit:
            problems.append(
                f"{where}: known to be wider than the action needs, with no compensating limit"
            )
    return problems


def render(manifest: tuple[Grant, ...]) -> str:
    lines = ["GRANTED", "-------"]
    for grant in manifest:
        lines.append(f"{grant.provider:8} {grant.scope}")
        lines.append(f"         tools: {', '.join(grant.tools)}")
        lines.append(f"         calls: {', '.join(grant.calls)}")
        lines.append(f"         why:   {grant.reason}")
        if grant.over_grant:
            lines.append(f"         wider: {grant.over_grant}")
        if grant.resource_limit:
            lines.append(f"         bound: {grant.resource_limit}")
        lines.append(f"         added: {grant.added} by {', '.join(grant.reviewers) or 'nobody'}")
        lines.append("")
    lines += ["RETIRED", "-------"]
    for provider, scope, note in RETIRED:
        lines.append(f"{provider:8} {scope}")
        lines.append(f"         {note}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 on an unjustified grant")
    args = parser.parse_args()

    if args.check:
        problems = unjustified(MANIFEST)
        for problem in problems:
            print(f"unjustified grant: {problem}", file=sys.stderr)
        return 1 if problems else 0

    print(render(MANIFEST))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
