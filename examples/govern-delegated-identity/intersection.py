"""Effective permissions for an agent acting for a user.

Run it with no credentials at all:

    python3 intersection.py

The rule this file encodes: the permissions of an agent acting for a user are
the *intersection* of what the agent may do and what the user may do. Never the
union, and never just one side. Two vendors state the same rule for their own
delegation mechanisms:

  * AWS: "The permissions for a session are the intersection of the identity-based
    policies for the IAM entity (user or role) used to create the session and the
    session policies."
  * Microsoft: a client holding a delegated permission "isn't able to access
    anything the signed in user couldn't access."

Two details this file exists to make concrete:

  1. Scope strings are hierarchical. Intersecting the raw strings is wrong.
     `Files.ReadWrite` implies `Files.Read`, so the naive intersection of
     {"Files.ReadWrite"} and {"Files.Read"} is empty when the true answer is
     {"Files.Read"}. Expand first, then intersect.
  2. The intersection is a *ceiling*, not the access decision. Which files the
     user may actually open is decided by the resource, per object, at call
     time. `resource_decision()` at the bottom marks where that boundary sits.
"""

from __future__ import annotations

from dataclasses import dataclass

# Microsoft Graph delegated permission names, from the driveItem search and mail
# permission references. `IMPLIES[p]` is the set of permissions that holding `p`
# also grants. Keep this table next to the app registration it mirrors: a stale
# entry here silently widens or narrows every decision below.
IMPLIES: dict[str, set[str]] = {
    "Files.ReadWrite.All": {"Files.Read.All", "Files.ReadWrite", "Files.Read"},
    "Files.Read.All": {"Files.Read"},
    "Files.ReadWrite": {"Files.Read"},
    "Sites.ReadWrite.All": {"Sites.Read.All"},
    "Mail.ReadWrite": {"Mail.Read"},
}


def expand(permissions: frozenset[str] | set[str]) -> frozenset[str]:
    """Close a permission set under IMPLIES, so subset tests mean what they say."""
    closed = set(permissions)
    frontier = list(permissions)
    while frontier:
        current = frontier.pop()
        for implied in IMPLIES.get(current, ()):
            if implied not in closed:
                closed.add(implied)
                frontier.append(implied)
    return frozenset(closed)


@dataclass(frozen=True)
class Principal:
    """One side of the intersection.

    `name` is what lands in the audit record. `granted` is what this principal
    may do: for the agent, the delegated permissions its app registration holds;
    for the user, the permissions the user actually consented to for this agent,
    read from the `scp` claim of the inbound token rather than from your own
    database, because your database is a cache and the token is the grant.
    """

    name: str
    granted: frozenset[str]

    @property
    def effective_grant(self) -> frozenset[str]:
        return expand(self.granted)


def intersect(agent: Principal, user: Principal) -> frozenset[str]:
    """The correct default. Neither principal can lift the other."""
    return agent.effective_grant & user.effective_grant


def union(agent: Principal, user: Principal) -> frozenset[str]:
    """The escalation path. Present only so the demo below can show it losing.

    Do not ship this. Every permission the agent holds becomes reachable by any
    user who can type a prompt, and every permission the user holds becomes
    reachable by anything that can influence the agent's next tool call.
    """
    return agent.effective_grant | user.effective_grant


class Denied(Exception):
    """Raised instead of calling a resource with permissions you do not have."""


def authorize(
    agent: Principal,
    user: Principal,
    required: frozenset[str] | set[str],
) -> frozenset[str]:
    """Return the permissions to request downstream, or raise Denied.

    Fails closed: an unknown permission string is in neither grant, so it is not
    in the intersection, so the call never leaves the process.
    """
    allowed = intersect(agent, user)
    missing = expand(required) - allowed
    if missing:
        raise Denied(
            f"{agent.name} acting for {user.name} lacks {sorted(missing)}; "
            f"agent has {sorted(agent.effective_grant)}, "
            f"user consented to {sorted(user.effective_grant)}"
        )
    return frozenset(required)


def resource_decision(user_upn: str, item_owner_group: str, memberships: set[str]) -> bool:
    """Stand-in for the decision the *resource* makes, which is not yours to make.

    `authorize()` above answers "may this class of call happen at all". This
    answers "may this particular person see this particular object", and in a
    real system it is Microsoft Graph, SharePoint, or your database answering
    it, per object, at call time. It is written out here only to mark the
    boundary: do not reimplement it in the agent, and do not cache its answer,
    because the entitlement behind it can change between two calls in one run.
    """
    del user_upn  # the resource keys off the authenticated subject in the token
    return item_owner_group in memberships


def _demo() -> None:
    # The agent's app registration holds one broad, admin-consented delegated
    # permission. That is deliberately realistic: it is the shape almost every
    # internal agent ends up with within a quarter of shipping.
    agent = Principal("revenue-assistant", frozenset({"Files.Read.All", "Mail.Read"}))

    # Priya is an intern who consented to the agent reading her own files.
    priya = Principal("priya (intern)", frozenset({"Files.Read"}))
    # Dana is a finance lead who consented to the same agent reading everything
    # she can reach, plus reading and writing her mail.
    dana = Principal("dana (finance lead)", frozenset({"Files.Read.All", "Mail.ReadWrite"}))

    print("=== permission sets ===")
    for principal in (agent, priya, dana):
        print(f"{principal.name:24} {sorted(principal.effective_grant)}")

    print("\n=== the identical request: search every site the caller can reach ===")
    required = {"Files.Read.All"}
    for user in (priya, dana):
        print(f"\n{user.name}")
        print(f"  intersection -> {sorted(intersect(agent, user))}")
        print(f"  union        -> {sorted(union(agent, user))}")
        try:
            print(f"  authorize    -> ALLOW {sorted(authorize(agent, user, required))}")
        except Denied as denied:
            print(f"  authorize    -> DENY  {denied}")
            if expand(required) <= union(agent, user):
                print("  under union  -> ALLOW <-- escalation: the agent's grant answered for the user")

    print("\n=== the reverse direction: a request only the human is entitled to ===")
    # Dana consented to Mail.ReadWrite. The agent was never registered for it.
    # Under a user-only model the agent inherits Dana's full authority, so a
    # prompt injection in a document Dana reads can now send mail as Dana.
    write_mail = {"Mail.ReadWrite"}
    print(f"  user-only    -> {'ALLOW' if write_mail <= dana.effective_grant else 'DENY'}")
    try:
        print(f"  intersection -> ALLOW {sorted(authorize(agent, dana, write_mail))}")
    except Denied as denied:
        print(f"  intersection -> DENY  {denied}")

    print("\n=== the ceiling is not the decision ===")
    print(
        "  dana clears authorize() for Files.Read.All, and the finance "
        "quarterly still stays invisible to anyone outside its group:"
    )
    for who, groups in (("dana", {"finance"}), ("priya", {"interns"})):
        visible = resource_decision(who, "finance", groups)
        print(f"    {who:6} sees Q3-margin.xlsx: {visible}")


if __name__ == "__main__":
    _demo()
