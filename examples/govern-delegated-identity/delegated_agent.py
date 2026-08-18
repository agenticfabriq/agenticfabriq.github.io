"""One agent process, many users, one identity per request.

Runs the identical prompt as two different people against Microsoft Graph and
prints two legitimately different answers, plus the audit record for each.

    export AF_TENANT_ID=... AF_CLIENT_ID=... AF_CLIENT_SECRET=...
    export USER_A_TOKEN=... USER_B_TOKEN=...
    python3 delegated_agent.py "quarterly margin"

Each USER_*_TOKEN is an access token issued to *this* agent's client id for one
user. Any client that can sign a user in will produce one; the device code flow
is the least painful for a terminal.

There is no module-level Graph client, no default user, and no credential the
process can reach without a request in hand. That is the whole design.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from intersection import Denied, Principal, authorize
from obo_exchange import (
    ClaimsChallenge,
    ConsentRequired,
    DelegationEnded,
    exchange_on_behalf_of,
    validate_inbound_token,
)

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_RESOURCE = "https://graph.microsoft.com"
TIMEOUT_SECONDS = 30

# There are two intersections in this file, at two layers, and conflating them
# is the usual mistake.
#
# Layer 1, in this process: the agent exposes its own delegated scopes on its own
# app registration, and the inbound token's `scp` claim says which of them this
# user consented to. AGENT_SURFACE is the agent's side; identity.consented is the
# user's side; `authorize()` intersects them and refuses before anything leaves.
#
# Layer 2, inside Entra: the OBO exchange intersects the agent registration's
# Graph permissions with what the user is entitled to, and reports the answer in
# the response `scope`. You do not implement layer 2. You read its result, and
# you make sure it did not come back wider than you asked.
AGENT_SURFACE = Principal("revenue-assistant", frozenset({"files.search", "mail.send"}))

# Prompt -> tool -> (capability the user must have consented to, Graph scope to
# request downstream). The router below picks the tool; in your system a model
# does. The point is what happens after the pick, and it must not depend on the
# model behaving.
TOOL_CAPABILITY = {"search_files": frozenset({"files.search"})}
TOOL_GRAPH_SCOPES = {"search_files": frozenset({"Files.Read"})}


@dataclass(frozen=True)
class RequestIdentity:
    """Everything about *who* this request is, resolved once, at the edge."""

    request_id: str
    subject: str  # `oid`: immutable, per-tenant, the id to key audit records on
    upn: str  # `preferred_username`: mutable, display only, never for authz
    tenant: str  # `tid`
    consented: frozenset[str]  # `scp`: what this user granted this agent

    @property
    def as_principal(self) -> Principal:
        return Principal(self.upn, self.consented)


class ToolRefused(Exception):
    """The agent declined before calling anything. This is a normal outcome."""


def resolve_identity(inbound_token: str) -> RequestIdentity:
    """Turn a validated bearer token into the identity this request runs as.

    Fails closed in three ways worth naming: an unsigned or wrongly-audienced
    token raises out of validate_inbound_token; a token with no `oid` raises
    here; and nothing in this function has a default. There is no path that
    produces an identity the caller did not prove.
    """
    claims = validate_inbound_token(
        inbound_token,
        tenant=os.environ["AF_TENANT_ID"],
        audience=os.environ["AF_CLIENT_ID"],
    )
    subject = claims.get("oid")
    if not subject:
        raise ToolRefused("token carries no oid; refusing to guess the subject")
    return RequestIdentity(
        request_id=str(uuid.uuid4()),
        subject=subject,
        upn=claims.get("preferred_username", "<unknown>"),
        tenant=claims.get("tid", "<unknown>"),
        consented=frozenset(claims.get("scp", "").split()),
    )


def route(prompt: str) -> str:
    """Stand-in for the model's tool choice. Deterministic so the demo is."""
    del prompt
    return "search_files"


def graph_search(access_token: str, query: str) -> list[dict]:
    """GET /me/drive/search(q='...') — items in the caller's drive plus items
    shared with the caller. `/me` only resolves when a user is signed in, which
    is precisely why an app-only token cannot make this call at all.
    """
    encoded = urllib.parse.quote(query.replace("'", "''"), safe="")
    url = f"{GRAPH}/me/drive/search(q='{encoded}')?$top=10&$select=id,name,webUrl"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read()).get("value", [])
    except urllib.error.HTTPError as error:
        if error.code == 401:
            # The resource rejected a token that has not expired. With continuous
            # access evaluation that is a revocation, not a fluke, and the
            # WWW-Authenticate header carries the claims challenge to relay.
            raise DelegationEnded(
                f"graph 401; www-authenticate: {error.headers.get('WWW-Authenticate')}"
            ) from error
        if error.code == 403:
            raise ToolRefused(f"graph 403: {error.read()!r}") from error
        raise


def audit(identity: RequestIdentity, action: str, outcome: str, **extra: object) -> None:
    """Two principals in one record, or the record cannot answer the question.

    After an incident nobody asks "did the agent do this". They ask "who was it
    doing it for". `actor` and `subject` both being present, in the same line,
    is the difference between a five-minute answer and a week of correlation.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": identity.request_id,
        "actor": AGENT_SURFACE.name,  # the agent: which software took the action
        "subject": identity.subject,  # the human: on whose authority
        "subject_upn": identity.upn,
        "tenant": identity.tenant,
        "action": action,
        "outcome": outcome,
        **extra,
    }
    print(json.dumps(record, sort_keys=True), file=sys.stderr)


def handle(prompt: str, inbound_token: str) -> list[dict]:
    """One request, start to finish, under exactly one user's identity."""
    identity = resolve_identity(inbound_token)
    tool = route(prompt)

    try:
        authorize(AGENT_SURFACE, identity.as_principal, TOOL_CAPABILITY[tool])
    except Denied as denied:
        audit(identity, tool, "denied", reason=str(denied))
        raise ToolRefused(str(denied)) from denied

    scopes = frozenset(f"{GRAPH_RESOURCE}/{name}" for name in TOOL_GRAPH_SCOPES[tool])
    try:
        delegated = exchange_on_behalf_of(inbound_token, scopes)
        results = graph_search(delegated.access_token, prompt)
    except DelegationEnded as ended:
        # Terminal. Do not retry, do not fall back to another credential, and do
        # not write partial results as though the task finished.
        audit(identity, tool, "revoked", reason=str(ended))
        raise
    except ConsentRequired as consent:
        audit(identity, tool, "consent_required", reason=str(consent))
        raise
    except ClaimsChallenge as challenge:
        # Relay to the caller as a 401 with the challenge; the client, not the
        # agent, is the party that can satisfy it.
        audit(identity, tool, "claims_challenge", claims=challenge.claims)
        raise

    audit(
        identity,
        tool,
        "ok",
        scopes=sorted(delegated.granted_scopes),
        result_count=len(results),
        result_ids=[item.get("id") for item in results],
    )
    return results


def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "quarterly margin"
    for label in ("USER_A_TOKEN", "USER_B_TOKEN"):
        token = os.environ.get(label)
        if not token:
            print(f"{label} unset; skipping", file=sys.stderr)
            continue
        print(f"\n=== {label}: {prompt!r} ===")
        try:
            for item in handle(prompt, token):
                print(f"  {item.get('name')}  {item.get('webUrl')}")
        except (ToolRefused, DelegationEnded, ConsentRequired, ClaimsChallenge) as stop:
            print(f"  refused: {stop}")


if __name__ == "__main__":
    main()
