---
title: "Agent Delegation vs Impersonation: On-Behalf-Of Flows"
description: "Acting for a user is not acting as one. How to run an on-behalf-of token exchange, intersect agent and user permissions, and audit both principals."
pillar: govern
updated: "2026-08-18"
keywords:
  - "agent delegation vs impersonation"
  - "on-behalf-of flow for ai agents"
  - "agent permissions intersection not union"
  - "ai agent acting on behalf of user"
relatedGuides:
  - govern/oauth-flows
  - govern/least-privilege
  - fail/token-leakage
howTo:
  - name: Give the agent an identity of its own
    text: Register the agent as its own principal with its own delegated permissions, so what the agent may do is written down separately from what any user may do.
  - name: Validate the inbound token before you trust the identity in it
    text: Verify the signature against the tenant's JWKS and check that the audience is your own client id, then read oid, tid, and scp from the verified claims.
  - name: Exchange the user's token for a downstream token
    text: Post the inbound token as the assertion with grant_type urn:ietf:params:oauth:grant-type:jwt-bearer and requested_token_use=on_behalf_of to get a token that still names the user.
  - name: Intersect the agent's permissions with the user's
    text: Compute the effective permission set as the intersection of the two grants, never the union, and refuse the call in your own process when a required permission is missing.
  - name: Read the granted scope instead of assuming it
    text: Compare the scope field of the exchange response against what you requested, and treat a wider result as a hard failure rather than a log line.
  - name: Route token-endpoint failures on the numeric error code
    text: Branch on the error_codes array rather than the error string, so a revoked grant, a stale assertion, a missing consent, and a claims challenge each get their own handler.
  - name: Write an audit record naming both principals
    text: Log the agent and the human in the same record, keyed on the immutable oid, so an incident review can answer who the agent was acting for.
  - name: Run the identical prompt as two users
    text: Send the same request under two different identities and confirm the results differ according to each user's own entitlements, not the agent's.
faq:
  - question: What is the difference between an agent acting for a user and acting as a user?
    answer: Acting for a user is delegation, where the agent keeps its own identity and the request carries both principals, so a resource can apply the agent's limits and the user's limits together. Acting as a user is impersonation, where the request is indistinguishable from one the user made, so no system downstream can tell an agent was involved.
  - question: Should an agent's permissions be the union or the intersection of the agent's and the user's?
    answer: The intersection. The union is a privilege escalation path in both directions. It lets any user reach permissions granted to the agent, and it lets anything that can steer the agent reach permissions granted to the human. AWS and Microsoft both define their delegated permission models as intersections.
  - question: What is the OAuth on-behalf-of flow?
    answer: It is a token exchange in which a service trades an access token it received from a user for a new access token to call a downstream API, still carrying the user's identity. Microsoft Entra ID implements it with grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer and requested_token_use=on_behalf_of; RFC 8693 OAuth 2.0 Token Exchange defines the vendor-neutral version.
  - question: How do I stop an agent from acting for an employee who has left?
    answer: Revocation has to reach the agent, not just the directory. Delete the stored delegation when the identity provider deprovisions the user, treat a revoked-grant error such as AADSTS50173 as terminal rather than retryable, and reconcile stored delegations against the directory on a schedule so an orphan cannot survive a missed webhook.
  - question: Why does my agent see the same results for every user?
    answer: Almost always because the downstream credential is resolved once at process start rather than per request. A module-level client, a cached token, or a search index built with an administrative credential all produce a single effective identity no matter who asked.
  - question: Does the user need to consent again when an agent gets a new tool?
    answer: Only when the new tool needs a permission, a resource, or a data destination the user did not already agree to. A new tool that reads what the user already consented to reading is not a new grant; a new tool that writes, or that sends data somewhere new, is, and reusing the old consent for it is scope creep.
---

## TL;DR

- Acting *for* a user is delegation: the agent keeps its own identity and the request carries two principals. Acting *as* a user is impersonation: the request is indistinguishable from one the user made. They produce different answers to the same access question, and they leave different audit records.
- The agent's effective permissions are the **intersection** of what the agent may do and what the user may do. The union is a privilege escalation path in both directions. Either side alone is a different bug.
- The mechanism is a token exchange, not an architecture diagram. RFC 8693 defines the vendor-neutral form; Microsoft Entra ID ships it as the on-behalf-of flow; AWS STS ships the intersection as session policies.
- An audit record with one principal cannot answer the only question asked after an incident. Log the agent and the human together, keyed on an immutable subject id.
- One process serving many users needs request-scoped identity resolution. Any credential the process can reach without a request in hand will eventually answer for the wrong person.

## Who this is for

You are running an agent that touches systems where two different employees are entitled to two different answers, and you have to be able to explain, afterwards, whose authority each call ran under. This guide covers delegation mechanics end to end: token exchange, permission intersection, revocation that arrives mid-run, and the audit record. Skip it if your agent operates on data that has no per-user access model — a public corpus, a shared queue, an internal metrics store everyone can read — because delegation adds machinery that buys you nothing there, and an agent identity with its own scoped permissions is the right answer instead.

## The problem

An agent gets built for one team with one credential. It works. Then a chat box goes in front of it, and the credential does not change.

Here is what that costs, concretely. The agent has a Microsoft Entra app registration holding the *application* permission `Files.Read.All` — the app-only kind, admin-consented, no user in the picture. Microsoft's own description of that permission is that the app "is able to read any file in the tenant using Microsoft Graph". Priya, an intern, asks it: *what was our Q3 margin?* The agent searches, finds the finance quarterly, summarises it, and answers. Nobody wrote a bug. The retrieval worked, the summary was accurate, the model behaved. Priya has just read a document she has never had permission to open, and the only thing standing between her and every other document in the tenant is whether she thinks to ask.

The mechanism is not exotic. The agent held authority; Priya held a question; nothing in the path consulted what Priya was entitled to, because there was no place in the request where that could have been expressed. This has a name in the OAuth security literature: the agent is a confused deputy, an intermediary with more authority than its caller, doing what its caller asked. The MCP authorization specification names the same problem for MCP servers acting as intermediaries to third-party APIs. And Microsoft's on-behalf-of documentation gives the reason its delegation flow refuses to carry application roles through an exchange, in one sentence: "*Roles* remain attached to the principal (the user) and never to the application operating on the user's behalf. This occurs to prevent the user gaining permission to resources they shouldn't have access to."

There is a second, sharper version of the same confusion, and it is the one the rest of this guide turns on. Acting *for* a user and acting *as* a user are not synonyms. Put one access decision through both and they answer differently.

Ravi can send mail as himself; that is an ordinary thing for an employee to be able to do. The agent's app registration has no mail permission at all — it was registered to read files. Ravi asks the agent to send a summary to a customer.

- **For Ravi — delegation.** Two principals are bound into the request. The question the system answers is "may this agent, acting for this user, send mail", and the agent half is empty. Denied. No phrasing of the prompt changes that, because the agent has no mail permission to reach for.
- **As Ravi — impersonation.** One principal is in the request. The question the system answers is "may Ravi send mail", and he may. Allowed. Sent.

Same agent, same user, same action, opposite answers — and the divergence is not caused by a policy anyone wrote. It is caused by how many principals the request carried. The audit records diverge with it: the delegated call is attributable to an agent and a human, and the impersonated one is attributable to Ravi, full stop. Six months later the incident review reads that log and concludes Ravi sent it.

The inverse arrangement fails differently and more quietly. Give the agent the user's own token and nothing else, and the agent inherits everything that human can do. Dana in finance can send mail, delete a site, and approve an invoice. Now a document Dana asks the agent to summarise contains an instruction, the agent follows it, and the blast radius of that prompt is the blast radius of Dana. The [failure pillar](/fail/) covers how instructions arrive inside content the agent was asked to read; the point here is that the identity model decided how much that mattered before the injection ever landed.

And then the part nobody notices until an auditor does. Both of the above produce logs. Neither produces an *answer*. When the question is "who read the finance quarterly on the fourteenth", a log line saying `revenue-assistant` is useless, and a log line saying `priya@contoso.com` is a lie by omission. You need both names in the same record, and if you did not design for that on day one you will be reconstructing it from timestamps.

## Step by step

One working path: a request arrives carrying a user's identity, the agent exchanges that identity for a downstream token scoped to the intersection, calls Microsoft Graph under it, and writes an audit record naming both principals. Then the same prompt runs as two different people. Runnable sources: `examples/govern-delegated-identity/intersection.py` (no credentials needed), `examples/govern-delegated-identity/obo_exchange.py`, and `examples/govern-delegated-identity/delegated_agent.py`.

Entra ID is the concrete provider here because its documentation states the rules explicitly and its literals are stable. Every step below names the standards-level equivalent, so the shape transfers.

### 1. Decide what "the agent may do" means, on its own

Register the agent as its own principal with its own permissions. Not a copy of a user, not a shared service account, not the same app registration your web product uses. This is the half of the intersection that belongs to the software, and it has to exist somewhere a reviewer can read it.

Entra makes the distinction sharp, and the vocabulary is worth learning even if you use a different provider, because the same split exists everywhere under different names:

| | Delegated permissions | Application permissions |
| --- | --- | --- |
| Access context | On behalf of a signed-in user | No user present |
| Token claim carrying them | `scp` | `roles` |
| Who can consent | Users for their own data; admins for all users | Admin only |
| Reach | Bounded by the user | Bounded only by the permission |

Microsoft states the consequence plainly: an app granted the *delegated* permission `Files.Read.All` "is only able to read files that the user can personally access", while an app granted the *application* permission of the same name "is able to read any file in the tenant". Same string, two orders of magnitude of reach, and the difference is which claim it lands in.

That asymmetry is the reason application permissions are the wrong default for an agent with users in front of it. They are the right answer for genuinely unattended work — a nightly reconciliation, an archival job, a system with no human on whose authority it could possibly be acting — and reaching for them because per-user consent is inconvenient is the decision this guide exists to argue you out of.

### 2. Validate the inbound token before you believe anything in it

The request arrives with `Authorization: Bearer <token>`. Before that token becomes an identity, three things must be true: the signature verifies against the issuing tenant's keys, the issuer is who you expect, and the audience is **you**.

The audience check is the one people skip. A token minted for some other API is not a token for your agent, and forwarding it downstream unchanged is the confused-deputy bug the MCP specification forbids in as many words. Its security best practices document sets the acceptance rule — "MCP servers **MUST NOT** accept any tokens that were not explicitly issued for the MCP server" — and its authorization security considerations set the forwarding rule: an MCP server calling an upstream API "**MUST NOT** pass through the token it received from the MCP client". Entra enforces the same thing from the other side — an on-behalf-of assertion must carry an `aud` claim matching the client id making the exchange, and an app cannot redeem a token issued for a different app.

```python
metadata = _discover(tenant)  # /v2.0/.well-known/openid-configuration
signing_key = PyJWKClient(metadata["jwks_uri"]).get_signing_key_from_jwt(token)
claims = jwt.decode(
    token,
    signing_key.key,
    algorithms=["RS256"],
    audience=os.environ["AF_CLIENT_ID"],   # this agent, not "any of our APIs"
    issuer=metadata["issuer"],
    options={"require": ["exp", "aud", "iss"]},
)
```

Read the endpoints out of the discovery document rather than hardcoding them. Then take exactly three things from the verified claims:

- `oid` — the immutable object id of the user. This is what you key audit records and stored delegations on.
- `tid` — the tenant.
- `scp` — the space-separated delegated scopes this user consented to for this agent.

Do not authorize on `preferred_username` or `name`. Microsoft's own claims reference says both are mutable and marks them display-only; `sub` is immutable but pairwise per application, so it will not correlate across your own services the way `oid` does.

One hour-saving detail before you debug this at 2am: token version changes both halves of the code above. The issuer in the v2.0 discovery document ends in `/v2.0`, while an app registration emitting v1 tokens issues `https://sts.windows.net/{tid}/`, so the issuer check fails with nothing in the error pointing at why. Set `accessTokenAcceptedVersion` to 2 in the app manifest, or discover against the v1 endpoint. The claim names move with it: `preferred_username` is v2-only and v1 carries `upn` instead, which is why `resolve_identity()` reads both.

### 3. Exchange the user's token for a downstream token

The agent now needs to call Graph. It does not forward the token it was given, and it does not reach for a credential of its own. It exchanges.

```python
body = _post_form(
    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": client_id,
        "client_secret": client_secret,
        "assertion": user_assertion,          # the token the caller sent you
        "scope": "https://graph.microsoft.com/Files.Read",
        "requested_token_use": "on_behalf_of",
    },
)
```

That is Entra's on-behalf-of flow against the protocol. Five details are worth holding onto:

- `requested_token_use=on_behalf_of` is what distinguishes this from an ordinary JWT bearer grant. Without it you are not delegating.
- The flow works only for user principals. A service principal that obtained an app-only token cannot exchange it, because there is no user for the result to be on behalf of.
- Certificate credentials replace `client_secret` with `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer` plus a signed `client_assertion`. Same flow, better key handling.
- An app configured with a custom signing key cannot be a middle tier here — Entra rejects the arrangement because the downstream API would be validating a signature the client controls.
- Do not send the resulting token back to the client. Microsoft's warning is unusually direct: tokens issued to the middle tier are for the middle tier's use with its intended audience, and relaying them to a client makes it impossible to satisfy token binding and the Conditional Access scenarios that require a claims step-up.

**The vendor-neutral version.** RFC 8693 OAuth 2.0 Token Exchange defines `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, with `subject_token` carrying "a security token that represents the identity of the party on behalf of whom the request is being made" and the optional `actor_token` carrying "the identity of the acting party". `audience` or `resource` names the target, `scope` requests the permissions, and `issued_token_type` in the response tells you what came back. Keycloak implements this as standard token exchange with `subject_token`, `subject_token_type`, `audience`, and `scope`. Auth0, Okta, and Ping have their own surfaces; check yours before assuming the parameter names carry over.

**Crossing a trust boundary.** If the downstream resource lives in a different domain with a different authorization server, the IETF draft *OAuth Identity and Authorization Chaining Across Domains* chains RFC 8693 with the RFC 7523 JWT bearer grant: exchange at home for a JWT authorization grant, present that grant to the other domain. It is an active Internet-Draft in the OAuth working group, not an RFC, so treat it as a direction rather than a dependency.

### 4. Compute the intersection, and refuse in your own process

Two grants, one answer:

```python
def intersect(agent: Principal, user: Principal) -> frozenset[str]:
    return agent.effective_grant & user.effective_grant
```

That is the entire rule, and both major clouds already state it for their own mechanisms. AWS: "The permissions for a session are the intersection of the identity-based policies for the IAM entity (user or role) used to create the session and the session policies." Microsoft: a delegated permission means the application "isn't able to access anything the signed in user couldn't access." Google says the same about domain-wide delegation — a service account's access "is constrained by two factors: the permissions of the impersonated user and the OAuth scopes you authorize in the Admin console."

Two implementation details that decide whether the rule holds in practice.

**Expand before you intersect.** Scope strings are hierarchical. `Files.ReadWrite` implies `Files.Read`, so the naive set intersection of `{"Files.ReadWrite"}` and `{"Files.Read"}` is empty when the correct answer is `{"Files.Read"}`. Close both sets under an implication table first — `expand()` in `intersection.py` — and keep that table next to the app registration it mirrors, because a stale entry silently changes every decision downstream of it.

**Fail closed, in your process, before the call.** Entra performs its own intersection inside the exchange, and Graph applies the user's entitlements per object. You still compute the intersection locally, for a reason worth stating: an unrecognised permission string is in neither grant, so it is in neither intersection, so the request never leaves the process. That is what turns a model that hallucinated a tool name into a denial rather than an error from a system you had to reach to get.

**Why not the union.** Work it in both directions, because it is a different bug each way, and neither one announces itself.

*Direction one — the user reaches the agent's authority.* Priya the intern has consented to `Files.Read`. She asks for the finance quarterly and the delegated call returns nothing, because Graph applied her entitlements. Under a union, the agent has somewhere else to go: the application permission from the problem section, or a stored service credential, or an admin-built search index. The pattern in the diff is almost always a fallback — `try` the user's token, catch the empty result or the 403, retry with the agent's own. It reads like resilience. It is a union, and it hands every user of the agent the agent's ceiling, reachable by rephrasing a question. Intersection makes the empty result the answer, which is the correct answer.

*Direction two — the agent reaches the human's authority.* This is Dana's case from the problem section, now as set arithmetic. She has consented to `Mail.ReadWrite`; the agent was never registered for mail at all. Under a union the agent's capability ceiling is the most privileged human who ever talks to it. Under intersection the agent has no mail-write permission, so no phrasing of any prompt produces one.

Both come from the same move — treating two grants as additive — and only the intersection resists both. `python3 examples/govern-delegated-identity/intersection.py` prints exactly these cases with no credentials required. Read what that file models carefully: it is the set arithmetic, not the plumbing. Its broad agent grant stands in for whatever authority the union reaches for — an application permission, a service credential, an admin-built index — because the arithmetic is identical whichever of those it is, and the arithmetic is the part people get wrong.

Two honest caveats on how much the platform does for you. First, a provider that implements delegation properly already refuses part of this: an Entra on-behalf-of exchange for a scope the user never consented to returns `AADSTS65001`, and Graph enforces the user's per-object entitlements regardless of which delegated scope the token carries. The union bug in that environment lives in *your* fallback logic, not in the protocol. Second, do not assume every authorization server is that strict. Keycloak's own documentation says "by default, token exchange can be used to request extra scopes and audiences that are not present in the initial `subject_token`", and points at a `downscope-assertion-grant-enforcer` policy to restrict it to downscoping. Whether yours narrows or widens on exchange is a configuration question with a bad default, and it is worth ten minutes to find out which one you have.

### 5. Read the scope you got, not the scope you asked for

RFC 8693 makes the response `scope` parameter "OPTIONAL if the scope of the issued security token is identical to the scope requested by the client; otherwise, it is REQUIRED". Narrower than requested is normal — the user consented to less than the agent asked for. Wider is a problem you want to hear about loudly:

```python
if _bare(granted) - _bare(requested):
    raise ScopeWidened(f"asked for {sorted(requested)}, received {sorted(granted)}")
```

Compare bare permission names rather than raw strings. Entra's own on-behalf-of example requests `https://graph.microsoft.com/user.read offline_access` and answers with `"scope": "https://graph.microsoft.com/user.read"` — the resource prefix round-trips and `offline_access` does not come back at all, so a naive string comparison fires on a completely correct response. `_bare()` in `obo_exchange.py` strips the prefix, drops the protocol scopes, and case-folds. That last one matters because this check raises rather than logs: the casing of the returned `scope` is not documented, Entra's example happens to be lowercase on both sides, and the request in step 3 asks for `Files.Read`. A comparison that treats `files.read` as a widening halts a request that was answered correctly.

### 6. Branch on the error code, not the error string

A revoked grant, an expired assertion, a missing consent, and a Conditional Access step-up each need a different response from you, and the OAuth `error` string is too coarse to tell them apart. Entra carries the distinction in a numeric `error_codes` array, and that is what to branch on. One caveat worth stating in place: Microsoft's error reference documents each `AADSTS` code and its text, but does not state which OAuth `error` string each code arrives alongside. Treat the string as a hint and the code as the fact — which is what `_raise_for_oauth_error()` in `obo_exchange.py` does, checking codes before strings.

| Code | Meaning | What to do |
| --- | --- | --- |
| `AADSTS50173` | "The provided grant has expired due to it being revoked... The user might have changed or reset their password." | Terminal. Delete the stored delegation, stop the run. |
| `AADSTS700082` | Refresh token expired due to inactivity. | Terminal. Re-consent required. |
| `AADSTS65001` | `DelegationDoesNotExist` — "The user or administrator hasn't consented to use the application with ID X." | Send the user through consent. Not retryable. |
| `AADSTS500133` | "Assertion isn't within its valid time range." | Recoverable, caller-side. The inbound token was stale by the time you exchanged it. Answer 401 asking for a fresh one, and **do not** delete the stored delegation — nothing was revoked. |
| `interaction_required` with a `claims` body | Conditional Access wants MFA, a compliant device, or a CAE re-evaluation. | Relay the opaque `claims` value to the client in a 401. The agent cannot satisfy it. |

Two rows in that table carry more weight than the others, because both are recoverable and both are easy to mistake for a dead grant.

`AADSTS500133` is the cheap one to get wrong. A stale inbound token is the caller's problem and takes one round trip to fix, but a handler that lumps every `invalid_grant` together will classify it as a revocation — and if you also implemented the offboarding fix below, that handler deletes a delegation nobody revoked and puts the user through full re-consent for presenting a token a few seconds late. `obo_exchange.py` gives it its own `AssertionExpired` exception, deliberately not a subclass of `DelegationEnded`, so no `except` clause can quietly absorb it, and `handle()` audits it as `assertion_expired` rather than `revoked`.

The claims challenge is a design constraint rather than an error case. The agent is not the party that can complete an MFA prompt. Microsoft's guidance is that the middle tier replies 401 with a `WWW-Authenticate` header carrying the challenge, the client re-requests a token presenting it, and clients "shouldn't retry to access the middle-tier service using a cached access token". An agent that swallows this and retries turns a solvable step-up into a silent failure.

The same discipline applies one layer out, at the resource. A 401 from Graph on a token that has not expired is *also* two different things, and the `WWW-Authenticate` header is what separates them: a `claims` parameter means a challenge the client can satisfy, and no `claims` parameter means a revocation it cannot. `graph_search()` parses the header for exactly that and raises `ClaimsChallenge` or `DelegationEnded` accordingly. Treating both as terminal throws away the recoverable half and records a live grant as revoked.

### 7. Write the audit record with both principals

```python
record = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "request_id": identity.request_id,
    "actor": AGENT_SURFACE.name,   # which software took the action
    "subject": identity.subject,   # oid: on whose authority
    "subject_upn": identity.upn,
    "action": tool,
    "outcome": outcome,
    "scopes": sorted(delegated.granted_scopes),
}
```

Two principals, one line, keyed on the immutable `oid`. Two production mechanisms do this at the platform level and are worth copying rather than inventing around.

RFC 8693 defines the `act` claim, "a means within a JWT to express that delegation has occurred and identify the acting party to whom authority has been delegated". The token itself carries both names:

```json
{
  "sub": "user@example.com",
  "act": { "sub": "https://agent.example.com" }
}
```

`act` nests, so a chain through two services records both hops in order. The companion `may_act` claim "makes a statement that one party is authorized to become the actor and act on behalf of another party" — a policy hook for constraining *who may act for whom*, which impersonation gives you no place to express.

AWS solves the same problem outside the token. `sts:SetSourceIdentity` lets a role session carry a string naming the human behind it; the value "is present in requests for any AWS action taken during the role session", "persists when a role is used to assume another role", and "cannot be changed during the role session". It surfaces in CloudTrail under `requestParameters.sourceIdentity` on the assume call and inside the `sessionContext` of `userIdentity` on everything after. Two constraints to know before you rely on it: trust policies for every role connected to an identity provider need the `sts:SetSourceIdentity` permission or `AssumeRole*` fails outright, and AWS does not control the value, so its trustworthiness is entirely a property of how your IdP populates it.

The [governance pillar](/govern/) covers what else belongs in that record and how long to keep it.

### 8. Run the identical prompt as two users

This is the demonstration. Same process, same prompt, same code path, two identities:

```python
def main() -> None:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "quarterly margin"
    for label in ("USER_A_TOKEN", "USER_B_TOKEN"):
        token = os.environ.get(label)
        ...
        for item in handle(prompt, token):
            print(f"  {item.get('name')}  {item.get('webUrl')}")
```

`handle()` calls `GET /me/drive/search(q='...')`, which returns items in the caller's drive plus items shared with the caller. Run it with two users' tokens and the output differs — not because the agent decided it should, and not because anything in your code branched on who was asking, but because `/me` resolved to two different people and Graph applied each one's entitlements.

Two things about that endpoint make the point better than any diagram. First, `/me` needs a signed-in user, so an app-only token cannot use it. The fix that presents itself is to rewrite the call as `/users/{user-id}/drive/root/search(q='...')` with an application permission, and it works — which is why it keeps getting merged. It is also no longer delegation. Nothing in that request represents the user as a principal; the user id is a path parameter, so Graph can only apply the app's own grant, and for `Files.Read.All` that grant is the whole tenant. In the diff it reads as a routing change.

Second, Microsoft lists `Files.Read` as the least privileged delegated permission for this search; every wider option in the same table — `Files.Read.All`, `Sites.Read.All` — buys reach the user may not have. The [connect pillar](/connect/) walks the same read-only-first ordering for other providers, and [connecting an agent to Gmail](/connect/gmail/) shows the per-user consent side in full.

If both users get the same results, you have a bug, and the failure modes below name the usual causes.

## Decision table

| Option | When it wins | Attribution | Revocation | Escalation risk | Cost |
| --- | --- | --- | --- | --- | --- |
| Impersonation — a service account that acts as the user | No interactive user exists at all: shared mailboxes, archival, compliance export, offboarded accounts | One principal in the log. The agent is invisible downstream. | One switch for everyone. Users cannot revoke their own. | High. One credential reaches every user in scope. | Lowest to build, highest to explain |
| Delegation with intersection | Any agent with named humans in front of it. The default. | Both principals, if you write both. `act` and `sourceIdentity` exist for this. | Per user, by the user, and it lands mid-run. | Bounded by the smaller of two grants. | A token exchange per request and error branching that is not optional |
| Agent-only identity | Genuinely unattended work over data with no per-user access model | The agent, correctly, because there is no human to name. | One switch, which is honest here. | Bounded by the agent's own grant, which stays small if you keep it small. | Lowest, and only correct when the premise holds |

Impersonation deserves the sharpest warning because it is the tempting one. It is tempting for real reasons: no consent screen, one credential instead of one per user, it works for batch and offline runs, and every tutorial reaches for it because it makes the first call succeed. Google's domain-wide delegation is the canonical implementation — the JWT's `sub` field is "the email address of the user for which the application is requesting delegated access", and "your application must specify which user to impersonate for each API request".

What you give up is not the permission model. Google's constraint holds: the service account "cannot access data that the impersonated user themselves cannot access". What you give up is everything else. RFC 8693's definition is the precise statement of the loss: under impersonation, principal A "is given all the rights that B has within some defined rights context and is indistinguishable from B in that context." Indistinguishable is the operative word. The resource cannot apply a different policy to an agent than to a person, because it cannot tell. The token has no `act` claim to constrain, so `may_act` has nothing to say. The user has no grant of their own to revoke, so offboarding one person does not narrow the credential. And the credential itself is a master key over its whole scope, which is a very different object from a per-user refresh token, both in what it is worth to an attacker and in what its loss obliges you to disclose.

The narrow case where impersonation is right is worth stating so the warning stays honest: unattended work against accounts with no interactive owner, where there is no human to consent and no delegation to record. A shared `support@` alias, a compliance archive, a departed employee's mailbox under legal hold. If a named human is present and could have consented, you are choosing impersonation for your convenience and paying for it with theirs.

## Checklist

- [ ] The agent has its own registered identity with its own permissions, separate from any user and from any other application you run.
- [ ] Every inbound token is signature-verified, issuer-checked, and audience-checked against this agent's own client id before any claim in it is used.
- [ ] No inbound token is ever forwarded to a downstream API unchanged.
- [ ] The effective permission set is computed as an intersection, and the code path that would compute a union does not exist.
- [ ] Permission sets are expanded under their implication hierarchy before any subset or intersection test.
- [ ] The exchange response's `scope` is compared against what was requested, and a wider result fails the call.
- [ ] Token-endpoint failures are routed on the numeric `error_codes`, never on the `error` string, into at least revoked, expired-assertion, needs-consent, and claims-challenge, with a different handler for each — and an expired assertion never deletes a delegation.
- [ ] A Conditional Access claims challenge is relayed to the caller rather than retried.
- [ ] Every audit record names the agent and the human, keyed on an immutable subject id, and includes the granted scopes.
- [ ] Nothing in the process holds a downstream credential that is reachable without a request in hand: no module-level client, no global token cache keyed on nothing.
- [ ] Offboarding deletes stored delegations, and a scheduled job reconciles stored delegations against the directory so an orphan cannot survive a missed webhook.
- [ ] Any retrieval index the agent queries either filters by the caller's entitlements at query time or is partitioned per principal.
- [ ] You can run the same prompt as two users and show two different, correct results.

## Failure modes

### The agent answers a question the asker was never allowed to ask

**Symptom:** no error anywhere. A user gets a correct, well-sourced answer drawn from a document they cannot open in the browser. It usually surfaces when they mention it to someone.

**Cause:** the agent's own grant answered on the user's behalf. Either the effective permission set was a union, or the downstream call used a credential belonging to the agent rather than to the caller. The tell is in your own records, not in the token: log the scopes you requested and the scopes the exchange returned, per request, and look for the calls that carried neither. Read those from your audit line rather than by decoding the Graph token — Microsoft asks you not to validate or read tokens for APIs you do not own, and the `scp` claim you would be looking for is documented as "only included for user tokens" precisely because an app-only call never had a user to describe.

**Fix:** intersect, and fail closed locally before the call. Then check the layer below the API: if the agent queries a vector index or a cache built with an administrative credential, the intersection is broken at the index and no amount of correctness at the API boundary repairs it. Filter retrieval by the caller's entitlements at query time or partition the index per principal. This one is a pattern, not a standard — there is no specification for ACL-aware retrieval, and the honest position is that you have to build it and test it with two users.

### The cached token outlives the revoked permission

**Symptom:** an admin disables an account or revokes sessions, and the agent keeps working for that person for minutes or hours.

**Cause:** access tokens are bearer tokens with a lifetime, and revoking a grant does not reach into memory. Entra's default access token lifetime is one hour; in a continuous-access-evaluation session it rises to as much as 28 hours, on the explicit trade that revocation is event-driven rather than expiry-driven.

**Detection, in increasing order of effort.** Handle the 401 you get on a token that has not expired: with CAE, a resource provider can reject a live token, and the response carries a claims challenge rather than a plain expiry. Microsoft puts the target for critical event evaluation at near real time, with "latency of up to 15 minutes... because of event propagation time". For anything not in that ecosystem, RFC 7662 token introspection gives you a direct answer — `active` is true when the token "has been issued by this authorization server, has not been revoked by the resource owner, and is within its given time window of validity" — at the cost of a round trip per check, and the spec's own caching guidance is that a response carrying `exp` "MUST NOT be cached beyond the time indicated therein". The push version is the OpenID Shared Signals family: subscribe to CAEP events, of which `https://schemas.openid.net/secevent/caep/event-type/session-revoked` and `https://schemas.openid.net/secevent/caep/event-type/token-claims-change` are the two that matter here.

**Fix:** shorten the window you actually control. Exchange per request rather than caching a downstream token across a long run, treat a 401 on an unexpired token as a revocation rather than a retry, and stop the run instead of finishing with the results you already have.

### The offboarded employee whose agent is still running

**Symptom:** a scheduled or long-running agent task keeps producing output for a person who left the company weeks ago. Usually discovered by a report with a departed employee's name on it.

**Cause:** deprovisioning reached the directory and not the agent. The identity provider disabled the account; your service still holds a stored delegation for it, and if that delegation includes a refresh token it will keep minting access tokens until something breaks.

**Detection:** two independent mechanisms, because each one alone fails silently. Subscribe to deprovisioning from the identity provider — SCIM (RFC 7644) `DELETE /Users/{id}` for a removal, or a PATCH clearing the `active` attribute that RFC 7643 defines on the core user schema — and treat either as a delete of every delegation you hold for that subject. Then reconcile on a schedule: walk your stored delegations, resolve each subject against the directory, and alert on any that no longer resolve or resolve to an inactive user. The webhook is the fast path; the reconciliation is the one that catches the webhook you missed.

**Fix:** delete the delegation, do not merely mark it. And make revoked-grant errors terminal at the code level — `AADSTS50173` and `AADSTS700082` in Entra — so a missed deprovisioning event turns into a stopped agent rather than a retry loop. A retry loop against a revoked grant is the signature of an integration that does not model revocation at all.

Terminal means *those codes*, not every `invalid_grant`. This is the pairing that does real damage: a handler broad enough to catch everything, plus a fix aggressive enough to delete on catch, turns a caller's stale token into a destroyed delegation and a full re-consent. Delete only on the codes that mean revoked, which is why step 6 routes on the numeric code and gives an expired assertion its own exception.

### The role change that widens, and nobody alerts on it

**Symptom:** an employee moves teams, and for the rest of that day the agent keeps returning their old team's documents — or starts returning their new team's before the move was meant to take effect.

**Cause:** entitlement changes propagate, and propagation takes time. Microsoft documents this precisely for its own stack: changes to Conditional Access policies and group membership "could take up to one day to be effective", from replication between Entra and resource providers, with optimisation reducing it to two hours for some policy updates. Narrowing changes get noticed because someone loses access and complains. Widening changes get noticed by nobody, which is why they are the dangerous half.

**Fix:** do not cache the user's entitlements inside the agent. This is the practical reason the intersection has to be computed from a live grant and enforced at the resource rather than from a copy of the directory: your copy is stale in whichever direction is least convenient. Where you must act immediately, use the escape hatch the provider gives you — Microsoft's documented answer for applying a policy or group-membership change to a specific user right now is `Revoke-MgUserSignInSession`, which revokes all of that user's refresh tokens. Otherwise, keep the agent's memory of who someone is as short as one request.

### The audit log names one principal

**Symptom:** an incident review asks which user's authority a given call ran under, and the answer takes three days and a join across four systems, or never arrives.

**Cause:** the record has one name in it. Either the agent's, because the call went out under a service credential, or the user's, because impersonation erased the agent. Both are common; the second is worse because it looks complete.

**Fix:** put both in the same record at the point of the call, not by correlation afterwards. If your token format supports it, carry the pair in the token — RFC 8693's `act` claim exists exactly for this and nests through multiple hops. If your platform has a first-class field, use it: AWS `sourceIdentity` persists across role chaining and cannot be changed for the life of the session, which is a stronger guarantee than anything you will implement in application code. If neither applies, log it yourself, keyed on the immutable subject id rather than an email address, because email addresses change and your two-year-old logs will not.

### Two users, one process, one client

**Symptom:** every user gets identical results, or worse, intermittently gets someone else's. Under low load it looks fine; it appears when concurrency rises.

**Cause:** identity resolved once instead of per request. The usual forms are a module-level client constructed at import, a token cache keyed on the tool name rather than the subject, a connection pool that carries an `Authorization` header, or a framework-level "current user" stored somewhere shared between coroutines.

**Fix:** request-scoped identity resolution, enforced structurally rather than by discipline. Resolve the identity once at the edge, thread it explicitly through every call, and make the downstream client take a token as an argument so there is no constructor that can succeed without one. In `delegated_agent.py` there is no module-level Graph client and no default user; `handle()` takes the inbound token and everything else derives from it. If you need ambient context, use `contextvars` rather than a module global — it is the ambient mechanism that survives `asyncio` correctly — and still assert the subject matches at the point of the call. The test that catches this is the one from step 8: run the same prompt as two users, concurrently, and assert the results differ.

### The consent that quietly grew

**Symptom:** a user consented months ago to an agent that read their calendar. It now files expenses. Nobody lied; each change was small.

**Cause:** OAuth scopes are a consent unit, not an intent unit. A user who agreed to `Files.Read` agreed to a permission, not to a purpose, so every new capability that fits inside the old permission arrives without a prompt. [Incremental authorization](https://developers.google.com/identity/protocols/oauth2/web-server#incrementalAuth) makes this explicit — set Google's `include_granted_scopes` to `true` and, if the request is granted, "the new access token will also cover any scopes to which the user previously granted the application access". The mechanism is fine; the drift is what you do with it.

**Fix, and the honest position.** Re-consent when the *capability class* changes, not when the tool count does. A new tool that reads what the user already agreed to let the agent read is not a new grant. A new tool that writes, that sends data to a destination the user did not agree to, or that reaches a resource outside the original consent, is a new grant, and reusing the old consent for it is the drift. Keep a per-agent record of what was consented to and when, and surface it to the user somewhere they can revoke it.

The uncomfortable part: OAuth has no standard way to ask "may this agent do this specific thing, once, on your behalf". Scopes are coarse and durable by design. RFC 9396 Rich Authorization Requests is the closest standard — `authorization_details` carries structured, per-action authorization data instead of a flat scope string — and support is thin enough that you should check your provider before designing around it. Everything else in this area, including per-action confirmation prompts in the agent's own UI, is a pattern you build and enforce yourself, with no protocol underneath it. Say so in your design doc rather than implying the identity provider is enforcing something it is not.

## Doing this at scale

Everything above is one agent against one provider. The cost does not scale linearly, and it is worth being specific about where it goes.

Each new downstream system brings its own delegation dialect. Entra wants `requested_token_use=on_behalf_of`; a Keycloak realm wants RFC 8693 parameters and a policy to stop it widening scopes on exchange; AWS wants an `AssumeRole` with a session policy and `sts:SetSourceIdentity` wired into every role trust policy connected to your IdP; Google wants either per-user OAuth or a domain-wide delegation you would rather not have. Each has its own revoked-grant error and its own propagation delay. Multiply that by the number of tools, then note that every one of them has to make the same intersection decision the same way, or the weakest one defines your security posture. The failure is not that any single integration is hard. It is that identity propagation is a cross-cutting concern being implemented once per tool, by whoever wired that tool up, on the day they wired it.

The structural answer is to make identity propagation a property of the call path rather than of each tool. One place resolves the caller, one place holds the per-user delegations, one place evaluates policy per request, and one place emits an audit record with both principals — and the tools receive an already-resolved identity instead of each learning a different token exchange.

That is the problem [Agentic Fabriq](https://agenticfabriq.com) is built around. Agents route through the control layer rather than holding credentials themselves; the per-user delegation lives in the layer; policy is evaluated per request; and each action is attributed to an agent and the user it acted for, which is the record step 7 argued for, produced by the path rather than by your logging discipline.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        for agent in await af.list_agents():
            print(agent)

        for subject in (os.environ["USER_A_OID"], os.environ["USER_B_OID"]):
            result = await af.invoke_connection(
                "sharepoint_docs",
                method="search_files",
                parameters={"query": "quarterly margin", "on_behalf_of": subject},
            )
            print(subject, result)


asyncio.run(main())
```

The runnable version is `examples/govern-delegated-identity/fabriq_delegated_call.py`. The property that matters is the shape, not the line count: the subject travels with the request, so two callers reach the same connection and get different data back, and the agent process holds one gateway token instead of one standing grant per user per system. Connection names, method names, parameter names, and response shapes are all per-deployment — run `afctl tools list` against your own gateway rather than copying `sharepoint_docs` or `search_files` on faith.

What a layer like [Agentic Fabriq](https://agenticfabriq.com) buys is the uniformity, not the exchange. Everything in this guide stays correct if you would rather own that yourself; the thing worth deciding deliberately is whether the intersection rule, the revocation handling, and the two-principal audit record are implemented once or once per integration.

## Further reading

The [governance pillar](/govern/) covers the surrounding decisions — how the agent's own permissions get chosen and reviewed, and what the audit trail has to retain. The [connect pillar](/connect/) works the per-provider side, where the scope taxonomy differs but the read-only-first ordering does not, and [connecting an agent to Gmail](/connect/gmail/) is the fullest worked example of per-user consent and its lifecycle. The [failure pillar](/fail/) collects what happens when identity is right and something else is not.

Primary sources for everything asserted above:

- [RFC 8693 OAuth 2.0 Token Exchange](https://www.rfc-editor.org/rfc/rfc8693.html) — the exchange grant type, `subject_token` and `actor_token`, the impersonation-versus-delegation definitions in section 1.1, and the `act`, `may_act`, `scope`, and `client_id` claims.
- [Microsoft identity platform and OAuth 2.0 On-Behalf-Of flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow) — every OBO parameter, the client limitations, the middle-tier token warning, and the claims-challenge error response.
- [Overview of permissions and consent in the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/permissions-consent-overview) — delegated versus application permissions and the `Files.Read.All` comparison.
- [Access token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — `scp`, `roles`, `oid`, `sub`, `tid`, `azp`, and which claims are safe for authorization decisions.
- [Microsoft Entra authentication and authorization error codes](https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes) — `AADSTS50173`, `AADSTS65001`, `AADSTS500133`, and `AADSTS700082`.
- [Continuous access evaluation in Microsoft Entra](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation) — critical events, the 28-hour CAE token lifetime, the up-to-15-minute propagation target, and the up-to-one-day group and policy replication delay.
- [Policies and permissions in AWS IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html) — session policies and the intersection rule.
- [Monitor and control actions taken with assumed roles](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_temp_control-access_monitor.html) — `sts:SetSourceIdentity`, `aws:SourceIdentity`, persistence across role chaining, and where it lands in CloudTrail.
- [Using OAuth 2.0 for server to server applications](https://developers.google.com/identity/protocols/oauth2/service-account) — domain-wide delegation, the `sub` field, and the two constraints on a service account's reach.
- [Search for files — Microsoft Graph](https://learn.microsoft.com/en-us/graph/api/driveitem-search) — the `/me/drive/search(q='...')` form and its delegated versus application permissions.
- [MCP security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices) — the token passthrough anti-pattern and the rule that a server must not accept any token not explicitly issued for it.
- [MCP authorization security considerations](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations) — token audience binding and validation, and the prohibition on passing a client's token through to an upstream API.
- [Using OAuth 2.0 for web server applications — incremental authorization](https://developers.google.com/identity/protocols/oauth2/web-server#incrementalAuth) — `include_granted_scopes` and what the resulting access token covers.
- [RFC 7662 OAuth 2.0 Token Introspection](https://www.rfc-editor.org/rfc/rfc7662.html) — the `active` response member and the caching constraint.
- [OpenID Continuous Access Evaluation Profile 1.0](https://openid.net/specs/openid-caep-1_0-final.html) — the `session-revoked` and `token-claims-change` event type URIs.
- [Keycloak token exchange](https://www.keycloak.org/securing-apps/token-exchange) — standard token exchange parameters and the default that permits requesting extra scopes.
- [OAuth Identity and Authorization Chaining Across Domains](https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-chaining/) — the cross-domain draft, still an Internet-Draft.
- [RFC 9396 OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/rfc/rfc9396.html) — `authorization_details` as the fine-grained alternative to scope strings.
- [RFC 7644 SCIM Protocol](https://www.rfc-editor.org/rfc/rfc7644.html) and [RFC 7643 SCIM Core Schema](https://www.rfc-editor.org/rfc/rfc7643.html) — user deprovisioning and the `active` attribute.
