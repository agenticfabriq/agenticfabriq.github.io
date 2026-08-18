---
title: Agent Identity and Permissions
description: An agent is neither a person nor a service, and identity systems were built for those two. How to give one a provable identity, decide per call, and revoke it.
pillar: govern
updated: "2026-08-18"
cornerstone: true
keywords:
  - ai agent identity
  - agent permissions
  - agent access control
  - agent authorization
  - delegated agent authority
  - agent revocation
relatedGuides:
  - govern/oauth-flows
  - govern/least-privilege
  - fail/token-leakage
howTo:
  - name: Name the principal before you grant it anything
    text: Register the agent as its own versioned identity with an owner and a one-sentence job description, separate from any user and from every other agent.
  - name: Draw the permission matrix
    text: Enumerate the cells the agent needs as agent by user by resource by action, and treat every cell you did not write down as denied.
  - name: Give the agent an identity it can prove
    text: Replace the shared API key with a credential issued to exactly one agent, audience-restricted so a second service cannot accept it.
  - name: Move the decision from grant time to call time
    text: Evaluate the four axes at the moment of the call rather than relying on the scope somebody chose when the credential was created.
  - name: Borrow the user's authority for the cells that hold user data
    text: Exchange the caller's token for a downstream token that still names them, and compute the effective permission as the intersection of the agent's grant and theirs.
  - name: Test revocation in three parts
    text: Stop one agent, for one user, against one resource, and confirm the next call is refused while every other cell keeps working.
  - name: Write one record per attempt naming both principals
    text: Record the agent, the human, the grant, the decision, the reason that names which axis decided, and the outcome as a separate field.
  - name: Plan the retirement before you ship
    text: Enumerate every grant the agent holds, revoke each at the provider, delete derived artifacts built under its authority, and retire the identity last.
faq:
  - question: Is an AI agent a user or a service account?
    answer: Neither, and that mismatch causes most agent identity problems. An agent acts on its own initiative like a service but acts on a named person's authority like a user, so a single request needs both principals in it. The workable model gives the agent its own registered identity and has it borrow a user's authority per request, rather than forcing it into either existing box.
  - question: What is the agent permission matrix?
    answer: It is the model that says every authorization question about an agent is a cell addressed by four axes - which agent, on whose behalf, against which resource, performing which action. Grants, policies, revocations, and audit records all address cells, and a system that cannot name all four axes cannot express a decision about one of them.
  - question: Why is a shared API key not an agent identity?
    answer: Because a bearer credential proves possession, not identity. RFC 6750 defines a bearer token as one where any party in possession can use it exactly as any other holder can, so a key shared by an agent, a cron job, and two engineers names the account rather than the caller. Attribution collapses to that account and revoking it stops every holder at once.
  - question: Should agent permissions be decided at grant time or at call time?
    answer: Both, in different roles. The grant is the ceiling and should be as narrow as the provider allows, because anything that bypasses your policy layer recovers the full grant. The per-call decision is where the resource, the acting user, and the task are all known, and it is the only place a per-resource revocation can take effect immediately.
  - question: How do you revoke an AI agent's access?
    answer: Run the three-part test - can you stop one agent, for one user, against one resource, right now. Standard OAuth revocation ends a whole grant, which is agent-and-user granularity with no resource axis and a propagation delay, so per-resource immediate revocation exists only if a decision point is consulted on every call.
  - question: What does complete attribution of an agent action look like?
    answer: One record per attempted action naming the agent, the human whose authority was used, the grant that permitted it, the resource, the action, the decision and the rule that produced it, and the outcome as a separate field. A record naming only the agent cannot narrow an incident to one user, and one naming only the human asserts that a person did something an agent did.
---

## TL;DR

- An agent is a third kind of principal. An OAuth grant produces a token representing either a human who consented ([RFC 6749 section 4.1](https://www.rfc-editor.org/rfc/rfc6749#section-4.1)) or software acting as itself ([section 4.4](https://www.rfc-editor.org/rfc/rfc6749#section-4.4)), and an agent routinely needs both inside one task. Every problem below is a consequence of forcing it into one box.
- Organize the whole subject as a matrix: **agent × user × resource × action**. Grants, policies, revocations, and audit records all address cells. A system that cannot name all four axes cannot express a decision about one of them.
- A shared API key is not an identity. [RFC 6750 section 1.2](https://www.rfc-editor.org/rfc/rfc6750#section-1.2) defines a bearer token as one where any holder can use it exactly as any other holder can, which means it names an account rather than a caller.
- Decide at call time, where the resource, the acting user, and the task are known. The grant is still the ceiling, and a per-call policy does not excuse a broad one — anything that bypasses the decision point recovers the whole grant.
- Two tests decide whether the model is real. Revocation: can you stop one agent, for one user, against one resource, right now. Attribution: does one record name both principals, the rule that decided, and the attempts that were refused.

## Who this is for

You are putting an agent in front of more than one person, against systems where two employees are entitled to two different answers, and someone has asked what it can do and how you would stop it. This page is the map for the whole governance pillar: the model, the vocabulary, and the design decisions each of the five guides below goes deep on. Skip it if your agent is a single-tenant batch job against data every operator may already read, because then there is no user axis and most of what follows collapses to ordinary service credential hygiene.

## The problem

Every identity system you will integrate with was built for a human or for a service, and it assumes the difference matters.

A human account has a password, a second factor, a manager, a joiner-mover-leaver process, and a consent screen that asks permission in the first person. A service account has none of those. It has a secret, an owner who is a team rather than a person, no consent because there is nobody to ask, and it exists to do one uniform thing forever. The two boxes are not arbitrary; they encode a real distinction about whether an action has a person behind it.

An agent has properties of both and fits neither. It runs on its own initiative in a loop, like a service. It acts because a named person asked it to, and often on that person's data, like a user. It is deployed, versioned, and rolled back like software, but it is consented to like an application. It reads content it did not author — a document, a support email, a web page — and then makes choices that determine which API calls happen, which is a behaviour neither box was designed to hold.

Put it in the wrong box and you lose a property you needed, deterministically.

**In the human box**, the agent gets a person's authority: a service account that logs in as the user, a copied `Authorization` header, a shared login. Now every action is attributable to that human and to nobody else, so the audit trail says a person did something a program did. Revocation is the human's — you cannot stop the agent without stopping them. And the agent's reach is the human's reach, so an instruction that arrives inside a document the agent was asked to summarise now runs with a finance manager's authority. The [failure pillar](/fail/) covers how those instructions arrive; the identity model decided how much they cost before the injection ever landed.

**In the service box**, the agent gets one credential of its own that must satisfy every request it will ever serve. That credential's authority is therefore the union of what every user of the agent may reach — which is the arithmetic worked in full in [credential vaulting and rotation for agents](/govern/credentials/), where the same environment variable answers for four hundred people. Attribution collapses the other way: every call is "the service did it", and per-user accountability has to be reconstructed from timestamps. And the intern who asks a well-phrased question reaches the agent's ceiling rather than her own.

### Agent as principal, agent as delegate

There are two honest modes, and the mistake is picking one for the whole agent.

**Agent as principal** means the agent acts as itself. It authenticates with its own credential, it is authorized by its own grant, the audit record names the software because there is no human to name, and revocation is one switch that stops it entirely. This is correct — not a compromise — for work with no person behind it: a nightly reconciliation, the agent reading its own configuration, posting to a channel it owns.

**Agent as delegate** means the agent acts for a named person while remaining itself. It still authenticates as itself and additionally presents evidence of that person's authority. [RFC 8693 section 1.1](https://www.rfc-editor.org/rfc/rfc8693#section-1.1) draws the line exactly: under delegation "principal A still has its own identity separate from B, and it is explicitly understood that while B may have delegated some of its rights to A, any actions taken are being taken by A representing B."

Here is what each mode does to the four properties that matter.

| | Agent as principal | Agent as delegate |
| --- | --- | --- |
| **Authentication** | One credential, proving one thing: this is the agent. | Two facts in one request: this is the agent, and this token represents that person. Carried as `subject_token` plus an optional `actor_token` in an [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693#section-2.1) exchange, or as Microsoft Entra's on-behalf-of flow. |
| **Authorization** | Bounded by the agent's own grant. Uniform across every caller. | The intersection of the agent's grant and the user's. Missing from either side denies, and the union is a privilege escalation path in both directions. |
| **Attribution** | Names the agent, correctly, because no human authorized this. | Names both. [RFC 8693 section 4.1](https://www.rfc-editor.org/rfc/rfc8693#section-4.1) defines the `act` claim for exactly this, and it nests so a chain of actors survives. |
| **Revocation** | One switch. Honest, and all-or-nothing. | Per user, per agent. One person ends their own delegation and nobody else is affected. |

The third possibility — the agent acting *as* the user, indistinguishable from them — is impersonation rather than delegation, and [delegated user identity for agents](/govern/delegated-identity/) is the guide that works it end to end: the token exchange, the intersection rule, the error codes that mean a grant is gone, and the narrow set of cases where impersonation is genuinely the right answer.

Most real agents are both modes at once, per call rather than per agent. The on-call assistant used throughout this page reads an incident channel as itself and reads the incident runbook as the engineer who asked — inside one task, seconds apart. A design that picks one mode for the whole agent gets one of those two calls wrong, and which one it gets wrong is determined by which call the author wrote first.

### The failure this all exists to prevent

An incident is running. Someone notices the assistant has been summarising a channel it should not have been in, and asks the question that decides whether your identity model is real: *can you turn it off for the payments team, right now, without turning it off for everyone else?*

The usual answers are all bad. You can delete the Slack app, which stops every team. You can revoke a user's refresh tokens, which stops that person's access to everything, not just to this agent. You can push a config change and wait for a deploy. You can rotate the credential, which — as [credential vaulting and rotation](/govern/credentials/) shows in detail — leaves the leaked copy working for the whole overlap window anyway.

None of those is the requested operation, because the requested operation names three things at once: one agent, one set of users, one resource. If your permissions live in a token, you have at best two of those axes and no way to combine them. That is the gap the rest of this page closes.

## Step by step

One agent, designed from nothing: `agent:oncall-assistant@v3`. An engineer asks it in Slack to summarise a running incident. It reads the incident channel, reads the incident runbook out of the asker's own document store, opens a tracking issue in one repository, and posts the summary back to the channel.

The decision layer is runnable with no credentials and no network: `examples/govern-index/permission_matrix.py`. Every output block below is real output from `python3 permission_matrix.py`.

### 1. Name the principal before you grant it anything

Give the agent an identity of its own before you decide what it may do, because everything downstream addresses that name.

Three properties, all of which get used later:

- **Its own registration.** Not a copy of a user, not the team's existing app registration, not the same OAuth client your product uses. Two agents sharing one registration cannot be revoked independently and cannot be told apart in a log, which is the shared-key problem wearing a nicer hat.
- **A version in the id.** `agent:oncall-assistant@v3`, the shape [audit trails and compliance for agent actions](/govern/audit/) uses for its `agent_id` column. An agent whose tool set changed is a different principal in every way that matters, and records written before the change have to stay interpretable after it.
- **An owner and a one-sentence job description.** *Summarise a running incident for the engineer who asked, and file a tracking issue.* Every grant in the next step is a claim that it implements that sentence, and a reviewer's job is to check the claim.

### 2. Draw the permission matrix

Every authorization question about an agent is a cell addressed by four axes:

- **agent** — which software is acting. Not "the platform", not "the integration": the versioned principal from step 1.
- **user** — on whose authority. A person, or explicitly nobody.
- **resource** — which thing. A channel, a document, a repository, a row.
- **action** — which verb. Read, create, delete, and the difference between them.

For the on-call assistant, the intended cells are these, and the last column is the guide that goes deep on that row:

| Resource | Action | Whose authority | How the agent proves it | Guide |
| --- | --- | --- | --- | --- |
| `slack:channel/C-INC-402` | read history | The agent's own | A Slack app identity holding [`channels:history`](https://docs.slack.dev/reference/scopes), which Slack scopes to "public channels that your Slack app has been added to" — so channel membership is half the grant | [least privilege](/govern/least-privilege/) |
| `graph:drive/runbook-payments.docx` | read file | The engineer's | A token exchange producing a downstream token that still names them, delegated rather than application permissions | [delegated identity](/govern/delegated-identity/) |
| `github:issues/acme-payments-api` | create issue | The agent's own | A GitHub App installation token, minted per run with `repositories` and `permissions` cut down at mint time | [credentials](/govern/credentials/) |
| `slack:channel/C-INC-402` | post message | The agent's own | `chat:write`, with `chat:write.public` deliberately withheld so it reaches only channels it was invited to | [least privilege](/govern/least-privilege/) |

Four rows. Everything else is denied, including things nobody has thought of, because the matrix is a list of what is permitted rather than a list of what is forbidden.

Two properties of this table are worth stating before anyone extends it.

**It is sparse on purpose, and it is not the same size as the system.** One agent, forty engineers, three resources and four verbs is 480 addressable cells. You do not write 480 rules. You write rules over axes — this action is user-scoped, this resource class is agent-owned — and the matrix is what those rules resolve to. What the matrix buys is that every rule you write has a place to be written, which is the thing a scope string cannot give you.

**Each column is somebody's specialist subject**, and the five guides in this pillar divide along exactly these lines:

- [OAuth flows for AI agents](/govern/oauth-flows/) is how a cell gets filled in the first place: the three grants that survive current security guidance, and the RFC 8693 token exchange that turns one broad user grant into a narrow downstream one instead of forwarding the original.
- [Delegated user identity for agents](/govern/delegated-identity/) owns the user axis: what it means to act *for* someone rather than *as* them, why the effective permission is an intersection rather than a union, and how a revoked grant arrives mid-run.
- [Designing least-privilege scopes for agents](/govern/least-privilege/) owns the resource and action axes: a repeatable method for deciding which cells exist at all, starting from the tool definitions rather than from the scope list, and a 30-day report on which granted scopes were never exercised.
- [Credential vaulting and rotation for agents](/govern/credentials/) owns the material behind the agent axis: why an environment variable cannot express per-user access, how to issue a credential per request, and the rotation ordering that does not take production down.
- [Audit trails and compliance for agent actions](/govern/audit/) owns the record of which cell was consulted and what it answered, field by field, including the denials that are the only evidence your controls did anything.

### 3. Give the agent an identity it can prove

An identity is three things: a name that is stable and unique, a way to prove you hold it, and a registration with an owner and a lifecycle. A shared API key has none of them.

[RFC 6750 section 1.2](https://www.rfc-editor.org/rfc/rfc6750#section-1.2) defines a bearer token as "a security token with the property that any party in possession of the token (a 'bearer') can use the token in any way that any other party in possession of it can." Read that as a statement about identity rather than about transport security. A key held by the agent, a cron job, and two engineers with production access proves that *somebody* who has the key is calling. It names the account. The caller is not in the request at all.

Three consequences follow, and each maps onto an axis:

- **The agent axis disappears.** Every holder is one principal, so no policy can distinguish them, and no log can either.
- **The revocation granularity collapses to the key.** Rotating it stops the agent, the cron job, and both engineers simultaneously — which is why nobody rotates it during an incident, which is the whole problem.
- **Its authority becomes a union.** A credential that must satisfy every caller has to be able to do everything any caller needs.

The nuance that keeps this honest: being a bearer token is not the flaw. A bearer token issued to exactly one agent, short-lived, and audience-restricted is a perfectly good agent credential, because the identity comes from *who it was issued to* rather than from who holds it — and the receiving side is what enforces that. [RFC 9068 section 4](https://www.rfc-editor.org/rfc/rfc9068#section-4) puts the rule on the resource server: a JWT access token "MUST be rejected if `aud` does not contain a resource indicator of the current resource server as a valid audience." Sharing and unbounded lifetime are the flaws, not the bearer property.

Where the agent's own name comes from, in practice:

- **A provider-native app identity** — a Slack app, a GitHub App installation. Already per-application, already revocable on its own, and the natural fit for the agent-as-principal rows of the matrix.
- **Its own OAuth client**, using the `client_credentials` grant for work with no user in it. [RFC 6749 section 4.4.3](https://www.rfc-editor.org/rfc/rfc6749#section-4.4.3) even says a refresh token "SHOULD NOT be included", because the client can simply authenticate again.
- **A workload identity it cannot choose** — a Kubernetes projected service account token, an instance metadata credential, a CI OIDC token. This is the strongest form, because the agent does not hold a secret that can be copied out of it, and [credential vaulting and rotation](/govern/credentials/) covers the trust chain behind each one and the specific ways each is misconfigured.

Whichever you use, the test is the same: at the resource, can you answer "which agent is this?" without reference to any human, and would the answer change if a different agent were calling?

### 4. Move the decision from grant time to call time

A grant-time decision is made once, when a credential is created or a consent screen is accepted, and then applies uniformly to every call that credential ever makes. A call-time decision is made with the resource, the action, the acting user, and the task all in hand.

Almost everything OAuth gives you is grant time. That is not a criticism of OAuth — a scope is a durable, coarse consent unit and it is good at being one — but it means the decision was made by whoever was integrating that afternoon, and it cannot see any of the facts that make one call fine and the next one an incident. `channels:history` cannot distinguish the incident channel from a different channel the app was also invited to. `Files.Read` cannot distinguish the runbook from the compensation spreadsheet. The scope was chosen before either object existed.

Four things exist only at call time, and each is a real control:

- **Which object.** The resource axis is an instance at call time and only a class at grant time.
- **Which human.** Delegated authority is per request, so the effective permission is computable only when you know who asked.
- **What the input was.** A step that just consumed untrusted content is a different risk from one that did not, which is why audit records carry an `untrusted_input` flag at all.
- **Whether anything has been revoked since.** This is the big one, and step 6 is about it.

The mechanisms are real and shipping, not hypothetical. [AWS session policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html) compute an intersection per session, and AWS states the rule directly: "The permissions for a session are the intersection of the identity-based policies for the IAM entity (user or role) used to create the session and the session policies." IAM `Condition` blocks are evaluated per request. [RFC 7662](https://www.rfc-editor.org/rfc/rfc7662.html) token introspection answers `active` per call, at the cost of a round trip. Microsoft's [continuous access evaluation](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation) lets a resource reject a token that has not expired. And [RFC 9396](https://www.rfc-editor.org/rfc/rfc9396.html) specifies `authorization_details` as structured, per-action authorization data instead of a flat scope string — check your provider before designing around it, because support is thin.

Here is the decision from the example, which is the matrix compiled into four ordered checks:

```python
def decide(call: Call) -> Decision:
    cell = (_resource_class(call.resource), call.action)

    # 1. The agent axis. An action outside the agent's own registered grant is
    #    refused no matter which human asked, which is the half of the
    #    intersection that no prompt can talk its way past.
    if cell not in AGENT_GRANT:
        return Decision(False, f"agent_grant: {AGENT} holds no {cell[1]} on {cell[0]}")

    # 2. The user axis. Data that belongs to a person needs a person in the
    #    request; running it as the agent is the confused-deputy shape.
    if cell[0] in USER_SCOPED:
        if call.subject is None:
            return Decision(False, f"subject_required: {cell[0]} is user-scoped and no subject was supplied")
        ...
```

Seven requests, decided per call:

```text
== one agent, seven cells, decided per call ==
  allow success  oid:ravi   slack:channel/C-INC-402                read_history  agent_grant covers slack:channel:read_history, no user data reached
  allow success  oid:ravi   graph:drive/runbook-payments.docx      read_file     intersection of agent_grant and grant:01JQ8Z9RMK covers graph:drive:read_file
  allow error    oid:priya  graph:drive/runbook-payments.docx      read_file     intersection of agent_grant and grant:01JQ8ZB4TT covers graph:drive:read_file
  allow success  oid:ravi   github:issues/acme-payments-api        create        agent_grant covers github:issues:create, no user data reached
  allow success  -          slack:channel/C-INC-402                post_message  agent_grant covers slack:channel:post_message, no user data reached
  deny  blocked  oid:ravi   graph:drive/runbook-payments.docx      delete_file   agent_grant: agent:oncall-assistant@v3 holds no delete_file on graph:drive
  deny  blocked  -          graph:drive/runbook-payments.docx      read_file     subject_required: graph:drive is user-scoped and no subject was supplied
```

Read the third and last rows, because they carry the argument.

The third row is Priya asking for a runbook she has no access to. The local decision *allows* it — she has delegated `read_file` to this agent, and the agent holds that action — and then the outcome is `error`, because the resource applied her own entitlements and refused (`Resource.fetch` in the example stands in for Graph doing that). That is the correct division of labour: whether Priya may open this particular file is a fact the resource holds and the agent does not. A local policy that tried to know it would be holding a copy of the directory that is stale in whichever direction is least convenient.

The last row is the same read with no subject supplied. It is denied before it leaves the process, because a user-scoped resource reached with no user in the request is the confused-deputy shape with nothing to attribute it to.

Now the same seven requests, decided by a shared API key:

```text
== the same 7 requests, decided by a shared API key ==
  allow: the caller possesses the key  (x7)
```

That is not a simplification for effect. It is the whole information content of a shared credential: somebody who has it is calling. Both denials above are unreachable, because neither the agent axis nor the user axis exists to check.

Three honest limits on call-time policy, because it is routinely oversold:

1. **A decision made inside the agent's own process is a boundary against a mistaken agent, not a compromised one.** An injection that steers the agent walks straight through a check that lives in the same process as the prompt. If the constraint must hold under attack, it lives in a gateway, a proxy, or the provider's policy engine — the point [least privilege](/govern/least-privilege/) makes about in-process allowlists.
2. **Call-time policy does not replace narrowing the grant.** The grant remains the ceiling, so anything that bypasses the decision point — a leaked refresh token, a direct API call, a misconfiguration — recovers the full scope. Enforcement buys time to narrow; it is not the narrowing, and [over-scoped OAuth](/fail/over-scoped-oauth/) is what the un-narrowed version costs.
3. **A cached decision is a grant-time decision wearing call-time clothes.** If your policy layer resolves entitlements once at startup, your revocation latency is the cache TTL regardless of how per-call the code looks.

### 5. Borrow the user's authority for the cells that hold user data

Classify every resource once: does its content belong to a person, or to the agent's own workspace? In the example that is a single set, `USER_SCOPED`, and getting a resource wrong in the permissive direction is precisely the bug that lets an intern read the finance quarterly.

For the user-scoped cells, the agent needs the caller's authority, and there is one correct way to get it: exchange, not forwarding. The agent presents the token it received and receives a different token, narrower, still naming the user, scoped to one downstream service. [OAuth flows for AI agents](/govern/oauth-flows/) has the wire-level version — every RFC 8693 parameter, what `audience` and `resource` each mean, and why you must verify the result is actually narrower, since nothing in the specification says an exchange cannot widen. [Delegated user identity](/govern/delegated-identity/) has the semantics: the intersection rule, the claims to authorize on, and the error codes that distinguish a revoked grant from a stale assertion. The [connect pillar](/connect/) runs the same reasoning against specific providers, where the scope taxonomy changes and the ordering does not.

What belongs here rather than there is the artifact. OAuth defines no consent receipt — there is no standard endpoint that answers "what did this user agree to, and when" — so the user axis of your matrix is a record you keep yourself: the agent, the subject, the scopes granted, the timestamp, and an id you can reference from an audit record. In the example that is the `DELEGATIONS` table, and each row carries a `grant_id` that lands in every record the delegation authorizes.

That record is not bookkeeping. It is the only way to enumerate whose authority the agent currently holds, and enumeration is what steps 6 and 8 need. An agent that cannot list its delegations cannot be revoked selectively and cannot be retired cleanly.

### 6. Make revocation real, and run the three-part test

The test: **can you stop one agent, for one user, against one resource, right now.** Three axes and a deadline, each of which fails independently.

- *One agent* fails when several agents share a credential or a registration.
- *One user* fails when the only lever is the credential itself.
- *One resource* fails almost always, because tokens carry scopes and scopes name classes.
- *Right now* fails whenever the answer is "at the next token expiry".

Work through what the protocols actually give you. [RFC 7009 section 2.1](https://www.rfc-editor.org/rfc/rfc7009#section-2.1) revocation ends a grant — that is agent-and-user granularity, with no resource axis at all, and the RFC itself notes "there could be a propagation delay, for example, in which some servers know about the invalidation while others do not." Tokens produced by an exchange are not cascaded either: [RFC 8693 section 2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.1) states that, absent one-time-use or other semantics specific to the token type, performing an exchange has no impact on the validity of the subject token, and nothing binds the issued token's lifetime to it — so a downstream token minted five minutes ago outlives the revocation of the token it came from, which is why [OAuth flows](/govern/oauth-flows/) argues for measuring exchanged-token lifetimes in minutes. Provider-level tools are blunter still: Microsoft's [documented way](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-continuous-access-evaluation) to apply a policy or group-membership change to one user immediately is `Revoke-MgUserSignInSession`, which revokes all of that user's refresh tokens — every agent and every application at once.

So per-resource, immediate revocation is not a thing the protocols hand you. It exists only if a decision point is consulted on every call. That is the strongest argument in this guide for call-time evaluation, and it is worth saying plainly rather than leaving as an implication: **the resource axis of revocation is a property of your decision layer, not of your tokens.**

In the example, revocation is a table consulted inside `decide()`, keyed on all three parts with `None` meaning "any":

```python
revoke(AGENT, subject="oid:ravi", resource="graph:drive")
```

The next call is refused, and only that cell is:

```text
== after revoke(agent, subject='oid:ravi', resource='graph:drive') ==
  deny  blocked  oid:ravi   graph:drive/runbook-payments.docx      read_file     revoked: ('agent:oncall-assistant@v3', 'oid:ravi', 'graph:drive')
  allow success  oid:ravi   slack:channel/C-INC-402                read_history  agent_grant covers slack:channel:read_history, no user data reached
  allow success  -          slack:channel/C-INC-402                post_message  agent_grant covers slack:channel:post_message, no user data reached
```

Three rows, and the second and third are the ones that make it a test rather than a kill switch: the same user against a different resource still works, and the agent's own unattended action still works. A revocation that stops more than the cell you named is an outage you will be blamed for, which is why the blunt levers do not get pulled.

Two things this does not do, and both need saying:

**A deny in your decision layer does not reach a credential already issued.** If the agent holds a provider refresh token for that user, the deny stops the agent from using it and nothing stops anyone who has stolen it. So revoke in both places — the deny for immediacy, the RFC 7009 call for finality — and keep issued lifetimes short so the two converge. The three durable revocation shapes, and which one you actually have, are laid out in [credential vaulting and rotation](/govern/credentials/): delete the principal at the resource, deny at the authorizer, or rely on expiry.

**Expiry is the only mechanism that works when nobody is watching.** Scoping a credential to one user and one resource pays off only if you find out about the leak. A short lifetime bounds the exposure whether or not anyone notices, which is why it is the control to reach for first if you are only going to implement one.

### 7. Attribute both principals, and name the rule that decided

A complete record answers four clauses at once: which human caused this, through which agent, under what authority, and could you have stopped it. [Audit trails and compliance for agent actions](/govern/audit/) has the field-by-field schema, the storage properties that make it evidence rather than a diary, and the query an investigation actually runs. What the matrix adds is that the record's job is to say **which cell was consulted and what the decision layer said about it**.

The example emits one record per attempt, including the attempts that never became calls. Here is the denial from the revocation above, abbreviated to the fields that carry the answer:

```json
{
  "action": "read_file",
  "agent_id": "agent:oncall-assistant@v3",
  "correlation_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "decision": "deny",
  "decision_reason": "revoked: ('agent:oncall-assistant@v3', 'oid:ravi', 'graph:drive')",
  "grant_id": "grant:01JQ8Z9RMK",
  "occurred_at": "2026-08-18T09:14:22+00:00",
  "outcome": "blocked",
  "policy_version": "oncall-assistant/2026-08-18",
  "principal_id": "oid:ravi",
  "principal_type": "human",
  "resource": "graph:drive/runbook-payments.docx"
}
```

Two fields carry more than their size suggests.

`decision_reason` names the axis that decided. "Denied by policy" is not attribution of a decision; `agent_grant: holds no delete_file on graph:drive` tells the next reader which of the four axes to change, and tells an auditor that a specific rule was evaluated rather than that a default fired. On the allow path this matters more, not less: an allow with no reason is unfalsifiable, because you cannot distinguish a correct decision from a policy that permits everything.

`principal_type` says `service` when there is no human, and the corresponding `principal_id` is explicitly null. "There was no human" and "we failed to capture the human" are different answers, and a record that cannot tell them apart converts an unattributed call into a legitimate unattended one.

**Why partial attribution is close to worthless.** Not merely incomplete — worthless, in three specific ways.

A record naming only the agent cannot narrow anything. During the incident from the top of this page, the question was whether you could stop the agent for one team. If the trail says `oncall-assistant` and nothing else, every user of the agent is in scope, and you cannot even decide who to notify. Attribution granularity and revocation granularity are the same fact seen from two sides: you can only stop what you can name.

A record naming only the human is worse than nothing, because it is a false assertion rather than a gap. It says a person did something a program did, and the investigation six months later has no reason to doubt it. That is the cost of impersonation, spelled out in [delegated user identity](/govern/delegated-identity/).

And a record containing only successes cannot answer the fourth clause. The refusals are the evidence that a control existed and fired; a window with no denials in it means either a well-behaved agent or a policy that is not evaluating anything, and only the count over time tells you which.

### 8. Plan the retirement before you ship

An agent is created, granted, used, changed, and eventually retired, and the last two stages are the ones nobody designs for.

**Changed** is the stage that quietly invalidates your review. A new tool is a permission change even when the scope list does not move, because the model chooses when to call it and you chose what is callable — the argument [least privilege](/govern/least-privilege/) makes for reviewing the tool registry as a permission diff. A new capability class — writing where it only read, sending data somewhere the user did not agree to — is a new grant and needs re-consent, even though OAuth will happily let you reuse the old one. And the version in the agent id has to move, or records written under the old behaviour get read under the new one.

**Retired** is a sequence, and the order is the whole content:

1. **Enumerate what it holds.** Every per-user delegation, every provider grant, every workload identity, every tool registration, every webhook subscription. This is why step 5 insisted the delegation record is not bookkeeping — you cannot revoke a list you cannot produce.
2. **Revoke at the providers.** The RFC 7009 revocation endpoint per grant, then delete the stored tokens, in that order. Reversing them leaves a live grant you can no longer name.
3. **Remove the ability to mint new credentials.** Delete the OAuth client, the app installation, the trust policy that accepts its workload identity. A retired agent with a live trust relationship is one deployment away from being un-retired.
4. **Delete what it accumulated under its own authority.** The retrieval index it built, the cached documents, the derived tables. An index built with an agent's credential outlives the agent and carries its reach, and there is no specification for entitlement-aware retrieval to fall back on — which means enumerating these is yours to do, by hand, from the design rather than from a tool.
5. **Retire the identity last, and keep the id resolvable.** Audit records naming `agent:oncall-assistant@v3` have to stay interpretable for the whole retention period, so the id must resolve to a description long after the agent stops running. Delete the identity first and you lose both the enumeration in step 1 and the meaning of everything it ever did.

The test for whether a retirement finished: no credential can be minted, no delegation row survives, and every provider's own grant listing shows nothing for that client. An agent that is merely switched off still holds everything it held yesterday.

## Decision table

| Model | Attribution | Revocation granularity | Multi-user support | Audit quality | When it wins |
| --- | --- | --- | --- | --- | --- |
| Shared credential | The account, not the caller. Every agent and every human holding the key produces identical records. | The key. Revoking stops every holder at once, which is why it does not get done. | None. The credential's authority must be the union of every user's, and the per-user boundary moves into your query code. | Records exist and prove nothing. You can show that the account acted, never who for. | One agent, one resource, no user axis, and every operator may already read all of it. Honest there, and nowhere else. |
| Per-agent identity | The agent, correctly and specifically. Two agents are two principals. | Per agent. You can stop this one without touching the others, but not for one user or one resource. | Still none. Every caller reaches the agent's ceiling, so the intern reaches whatever the agent can reach. | Good on the agent axis, blank on the user axis. Answers "what did it do", never "for whom". | Genuinely unattended work over data with no per-user access model: reconciliation, archival, the agent's own housekeeping. |
| Per-agent identity with delegation | Both principals in one record, keyed on an immutable subject id, with the grant that permitted it. | All three axes, if the decision is consulted per call: this agent, for this user, against this resource. | Native. The effective permission is the intersection of the agent's grant and the caller's, computed per request. | Complete, and complete is the only useful state — the four clauses are a conjunction. | Any agent with named humans in front of it, which is most of them. The default. |

The middle row is the one people skip past on their way to the third, and it deserves better. When there is genuinely no human behind the work, an agent identity with a small grant of its own is the correct answer and delegation is machinery that buys nothing — there is nobody to consent, nobody to attribute to, and nothing to revoke per user. Choosing it deliberately is different from arriving at it because per-user consent was inconvenient.

The third row is not free, and the costs are concrete rather than architectural: a token exchange in the request path, error branching that has to distinguish a revoked grant from a stale assertion, a consent record per user that has to stay current through re-consent, and a decision point that is now in the path of every call and needs the availability budget of a database.

## Checklist

- [ ] The agent has its own registered identity, with a version in the id, an owner, and a one-sentence job description.
- [ ] No two agents share a registration, an OAuth client, or a credential.
- [ ] Every intended cell is written down as agent, user, resource, and action, and anything not written down is denied by default rather than permitted by omission.
- [ ] Every resource is classified as user-scoped or agent-owned, and a user-scoped resource reached with no subject in the request is refused before the call leaves the process.
- [ ] No credential the agent presents is a long-lived string shared with a human, a cron job, or another agent.
- [ ] Issued tokens are audience-restricted, and resource servers reject a token minted for a sibling service.
- [ ] The effective permission for a delegated cell is an intersection, and no fallback path exists that retries a user's failed call with the agent's own credential.
- [ ] A delegation record exists per agent and user, carrying the granted scopes, the timestamp, and an id that appears in every audit record it authorizes.
- [ ] You can list, right now, every user whose authority this agent currently holds.
- [ ] You have executed the three-part revocation test outside an incident: one agent, one user, one resource, and confirmed every other cell still works.
- [ ] Credential lifetimes are short enough that expiry bounds a leak you never detect, and the number is written down.
- [ ] Every attempted action produces one record, including denials, naming the agent and the human, keyed on an immutable subject id.
- [ ] `decision_reason` names the rule and the axis that decided, on allows as well as denials.
- [ ] `principal_id` is explicitly null for unattended work, and nothing writes `"system"` or `"unknown"` into it.
- [ ] Adding a tool to the registry triggers the same permission review as adding a scope.
- [ ] A written retirement sequence exists, ending with the identity rather than starting with it, and it names the derived artifacts that have to be deleted.

## Failure modes

### You can only turn it off for everybody

**Symptom:** an incident is running, one team's data is involved, and the only available action stops the agent for every team — or stops one person's access to every system.

**Cause:** the permissions live in a credential, so the revocation granularity is the credential's granularity. A token carries scopes, and scopes name resource classes, so there is no per-resource lever anywhere in the stack. Revoking the grant is agent-and-user at best, and revoking the user's sessions is user-and-everything.

**Fix:** put a decision point in the call path and give it a revocation table keyed on all three parts. Then run the test on a quiet Tuesday, because a revocation path first exercised during an incident is an untested one. Keep the provider-side revocation as the durable half — the deny is instant and local, the RFC 7009 call is what survives a restart of your policy layer.

### Nobody can answer what the agent can do

**Symptom:** an access review, a customer questionnaire, or an incident asks for the agent's reach, and the honest answer takes three days of code archaeology across a scope list, a tool registry, and a provider console.

**Cause:** the agent's authority is spread across artifacts that are each owned by someone different and reviewed on different schedules. The scope list is in a config file, the tool registry is ordinary application code, the channel memberships are in a Slack admin screen, and the repository selection is in a GitHub App installation nobody has opened since it was created.

**Fix:** the matrix is the single artifact, and it lives in the repository. Every cell names the provider-side facts that implement it — the scope, the app installation, the channel membership, the selected repositories — so the review reads one table rather than four systems. Then close the loop: enumerate what is callable at runtime and diff it against the table, so a cell that appeared without a row fails a build instead of surfacing in an audit.

### Two agents, one app registration

**Symptom:** revoking or rotating for one agent breaks another, unrelated one. Or an audit trail attributes a call to a shared client id and there is no field anywhere that says which of the two agents made it.

**Cause:** the second agent was shipped by copying the first one's configuration, which is the fastest way to get a working integration and permanently fuses two principals. Provider-side, they are one application; every grant, every consent record, and every log line describes both.

**Fix:** one registration per agent, from the first day of the second agent. Retrofitting is a re-consent for every user, which is exactly the cost that keeps it from happening, so the cheap moment is now. Where a provider genuinely does not support multiple registrations, carry the agent identity as an explicit field through your own decision layer and audit records, and be honest in your documentation that provider-side revocation cannot separate them.

### The decision reason nobody can act on

**Symptom:** the audit trail is complete, well-indexed, and useless. Every row says `allow` with a reason of `policy` or `ok`, and an investigation cannot tell a deliberate permission from a default that fired.

**Cause:** the policy layer returns a boolean and the record stores it. Nobody noticed, because the field is populated and the schema validates.

**Fix:** return the rule, not the verdict, and name the axis in it. `agent_grant: holds no delete_file on graph:drive` and `intersection of agent_grant and grant:01JQ8Z9RMK covers graph:drive:read_file` are both actionable; the first tells you which grant to change and the second tells you which delegation to revoke. Store `policy_version` alongside, so a historical decision is judged against the rules that existed then rather than the rules you have now.

### The agent is off but not retired

**Symptom:** months after a decommission, a provider's grant listing still shows the client, or a refresh token from a deleted agent is found in a backup and still works.

**Cause:** decommissioning stopped the process. Nothing enumerated the grants, because nothing could — the delegation records were deleted along with the service, or were never kept as their own artifact. The agent's authority survived the agent.

**Fix:** the retirement sequence from step 8, run in order, with enumeration first and identity deletion last. Verify from the provider side rather than from your own database: the test is that the provider's own grant listing shows nothing for that client, not that your table is empty. And treat the derived artifacts as part of the agent — an index built under its credential is still holding its reach after it is gone.

### The call-time policy that was really a cache

**Symptom:** a revocation takes effect somewhere between immediately and an hour later, unpredictably, and the delay is different in staging.

**Cause:** the decision function is called per request but resolves its inputs from a snapshot: entitlements loaded at startup, a delegation table refreshed on a timer, a token cached for its full lifetime. The code looks per-call and the data is per-deploy.

**Fix:** decide which inputs must be live and pay for them. Revocation state and delegation validity are the two that have to be, because they are the two that change in response to an incident. Everything else can be cached with the expiry attached and refreshed before it. Then state the resulting revocation latency as a number in your runbook, because "immediately" is a claim somebody will test in front of you.

## Doing this at scale

Everything above is one agent. Read the list of what it required: a registered identity, four cells written down, a decision evaluated per call, a delegation record per user, a three-part revocation table, one audit record per attempt, and a retirement sequence. None of it is difficult. All of it is permanent, and it is implemented once per agent by whoever shipped that agent.

That is where the model degrades, and it degrades in a specific direction. The first agent is built by whoever read this page. The fourth is built by copying the third. The seventh is a vendor SDK you do not control, and it authenticates with one key because that is what its quickstart does. Nothing in your logs reports this, because a missing decision and a decision that allowed look identical from the outside. The weakest agent then defines the fleet's posture, and per-agent implementation guarantees there will be a weakest one.

The structural answer is to make the decision point a property of the call path rather than of each agent. One place resolves the caller, one place holds the per-user delegations, one place evaluates the four axes per request, one place holds the provider credentials, and one place emits the record. Agents receive an already-resolved identity and name a connection instead of holding a credential. The three properties that buys are worth naming precisely: an agent cannot forget to log, because it is not the thing logging; an agent cannot forget to check, because the check is what makes the call; and the emitter is not the audited component, which turns separation of duties from a rota into a property of the architecture.

[Agentic Fabriq](https://agenticfabriq.com) is built as that layer — agent identity, per-request policy evaluation, credential vaulting, and an audit trail attributing every action to an agent and the user it acted for. The three axes of the matrix map onto three calls:

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # The agent axis: the roster of principals an access review starts from.
        agents = await af.list_agents()
        print(f"{len(agents)} registered agents")

        # The action axis: everything callable, which is the permission surface
        # whether or not anyone reviewed it as one.
        for tool in await af.list_tools():
            print(tool)

        # The user axis: same connection, same resource, two subjects.
        for subject in (os.environ["USER_A_OID"], os.environ["USER_B_OID"]):
            result = await af.invoke_connection(
                "runbooks",
                method="read_file",
                parameters={"path": "/runbooks/payments.docx", "on_behalf_of": subject},
            )
            print(subject, bool(result.get("content")))


asyncio.run(main())
```

The runnable version is `examples/govern-index/fabriq_control_plane.py`. Two caveats before you copy it: the published package has at times lagged its own README on the module layout, so confirm the import path against the version you install rather than against this page; and connection names, method names, parameter names, and response keys are per-deployment, so run `afctl tools list` against your own gateway rather than trusting `runbooks`, `read_file`, or the `content` key.

Be clear about what a control layer does not settle. It does not choose your scopes — a gateway that faithfully proxies an over-scoped grant is an over-scoped grant with better logs, and steps 1 through 3 of the least-privilege method are yours either way. It does not decide your retention period or your redaction rules. And it puts a dependency in the path of every call, whose availability, retention, and export model become questions you have to ask before adopting it rather than after. What it changes is that the matrix has one implementation instead of one per agent. Everything on this page stays correct if you would rather own that yourself; a layer like [Agentic Fabriq](https://agenticfabriq.com) is the buy side of the same decision, and doing it yourself is genuinely viable for a handful of agents.

## Further reading

Start with the axis you are weakest on. If you cannot say how a grant is obtained or ended, read [OAuth flows for AI agents](/govern/oauth-flows/). If you cannot answer "for whom", read [delegated user identity for agents](/govern/delegated-identity/). If you cannot justify a scope you hold, run the method in [designing least-privilege scopes](/govern/least-privilege/). If the agent holds a long-lived secret, [credential vaulting and rotation](/govern/credentials/) is the one to read first. And if you could not reconstruct last Monday in front of someone who is not on your team, [audit trails and compliance for agent actions](/govern/audit/) has the schema and the store.

The [connect pillar](/connect/) is where these decisions meet a specific provider, and the scope taxonomies differ enough that a worked example is worth more than a principle — [connecting an agent to Gmail](/connect/gmail/) for the per-user consent lifecycle, [connecting an agent to a database](/connect/databases/) for the case where the resource axis is a row. The [failure pillar](/fail/) is the same material read backwards, from the incident: [token leakage through agent context](/fail/token-leakage/) for what happens when the credential escapes the layer it was supposed to stay below, and [over-scoped OAuth](/fail/over-scoped-oauth/) for what a grant nobody narrowed costs when something goes wrong.

Primary sources for everything asserted above:

- [RFC 6749 — The OAuth 2.0 Authorization Framework](https://www.rfc-editor.org/rfc/rfc6749): [section 4.1](https://www.rfc-editor.org/rfc/rfc6749#section-4.1) the authorization code grant, [section 4.4](https://www.rfc-editor.org/rfc/rfc6749#section-4.4) client credentials, [section 4.4.3](https://www.rfc-editor.org/rfc/rfc6749#section-4.4.3) the refresh token advice for that grant.
- [RFC 6750 — Bearer Token Usage](https://www.rfc-editor.org/rfc/rfc6750#section-1.2) — the definition of a bearer token, which is the precise reason a shared key cannot be an identity.
- [RFC 8693 — OAuth 2.0 Token Exchange](https://www.rfc-editor.org/rfc/rfc8693): [section 1.1](https://www.rfc-editor.org/rfc/rfc8693#section-1.1) delegation versus impersonation, [section 2.1](https://www.rfc-editor.org/rfc/rfc8693#section-2.1) the request and the absence of linkage between input and issued tokens, [section 4.1](https://www.rfc-editor.org/rfc/rfc8693#section-4.1) the `act` claim.
- [RFC 7009 — OAuth 2.0 Token Revocation](https://www.rfc-editor.org/rfc/rfc7009#section-2.1) — what revoking a grant does, and the propagation delay it acknowledges.
- [RFC 9068 — JWT Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068#section-4) — the `aud` rejection rule that makes "issued to one holder" enforceable.
- [RFC 7662 — OAuth 2.0 Token Introspection](https://www.rfc-editor.org/rfc/rfc7662.html) — `active` as a per-call question rather than a grant-time one.
- [RFC 9396 — OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/rfc/rfc9396.html) — `authorization_details`, for the cases where a scope string cannot express the cell.
- [Policies and permissions in AWS IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html) — session policies and the intersection rule, quoted above.
- [Overview of permissions and consent in the Microsoft identity platform](https://learn.microsoft.com/en-us/entra/identity-platform/permissions-consent-overview) — delegated versus application permissions, which is the agent-as-delegate and agent-as-principal split in one vendor's vocabulary.
- [Slack OAuth scopes reference](https://docs.slack.dev/reference/scopes) — `channels:history`, `chat:write`, and `chat:write.public`, including the channel-membership wording that makes membership half the grant.
- [GitHub Apps REST API](https://docs.github.com/en/rest/apps/apps) — installation access tokens and the `repositories` and `permissions` parameters that narrow one at mint time.
- [NIST SP 800-53 Rev. 5](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final) — AU-3 on the content of an audit record, including the "identity of any individuals, subjects, or objects/entities associated with the event" — the clause an agent strains, because there are two identities and the interesting one is not the one making the call.
