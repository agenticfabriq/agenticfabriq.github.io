---
title: How to Give an AI Agent Access to Your Tools
description: The four ways an agent gets access to a system — shared key, per-user OAuth, service identity, brokered — compared on attribution, blast radius, and cost.
pillar: connect
updated: "2026-08-18"
cornerstone: true
keywords:
  - ai agent tool access
  - connect ai agent to apps
  - agent integration guide
  - agent access patterns
  - per-user oauth agent
  - agent service account
  - brokered agent credentials
relatedGuides:
  - connect/gmail
  - govern/oauth-flows
  - fail/over-scoped-oauth
howTo:
  - name: Decide whether the agent acts for a person or as itself
    text: Answer that one question first, because it determines whose authority the call carries, whose name appears in the provider's log, and what revocation means.
  - name: Put all four access patterns against the same integration
    text: Write out how a shared credential, per-user OAuth, a service or app identity, and a brokered call each implement the same requirement, and what each one costs.
  - name: Select scopes read-only first and widen from evidence
    text: Ship the narrowest read scope, run the real workload, and let the failures name the permissions you actually need instead of guessing them upward.
  - name: Decide where the credential lives before you write the integration
    text: Choose the store, the rotation path, and the revocation path first, because the credential outlives the code that first used it.
  - name: Decide what the audit record must contain before the incident
    text: Write the record at the point the access decision is made, carrying the agent, the human it acted for, the authority, and the outcome including denials.
  - name: Land on a pattern and write down why the others lost
    text: Name the chosen pattern, the constraint that eliminated each alternative, and the condition that would make you revisit the decision.
faq:
  - question: What are the ways to give an AI agent access to an API?
    answer: There are four. The agent holds a shared API key or service credential directly; the agent carries a per-user OAuth token a named human granted; the agent has a service account or app identity with permissions of its own; or the agent holds nothing and a control layer resolves the credential and decides per request. They differ mainly in who the provider records as the actor and what revoking access costs.
  - question: Should an AI agent use its own credentials or the user's?
    answer: It depends on whether the agent acts for a person or as itself. An agent doing something a named human asked for should carry that human's grant, so the person can see and withdraw it. An agent doing unattended work with no human in the request — a nightly sync, its own telemetry — should have its own identity, and you then owe an audit trail that records which human, if any, caused the run.
  - question: Is a service account safe for an AI agent?
    answer: It is safe in proportion to how narrowly its own permissions are drawn, and it is a poor fit where it can only reach data by impersonating users. A GitHub App installation token is a service identity with permissions of its own, scoped to named repositories and expiring after one hour. A Google Workspace service account with domain-wide delegation reaches a mailbox only by impersonating its owner, authorized by a super administrator with the users not directly involved.
  - question: Why can't you narrow an agent's OAuth scopes later?
    answer: On most providers a granted scope cannot be removed from an existing token. Slack states outright that there is no way to remove scopes from an existing token without revoking it entirely, so narrowing means revoking and re-consenting every affected user, which is a rollout rather than a code change. GitHub is the exception worth knowing, because removing permissions from a GitHub App takes effect immediately, with no approval needed.
  - question: What is brokered access for an AI agent?
    answer: Brokered access means the agent holds no provider credential at all. It holds a token for a control layer, names a connection, and the layer resolves the credential, evaluates policy for that specific request, makes the call, and records it against both the agent and the human it acted for. It is not a fourth kind of grant — the grant underneath is still per-user OAuth or still a service identity, and every scope decision still applies unchanged.
  - question: What should an AI agent's access audit record contain?
    answer: Four things, because that is what the question after an incident actually asks — which human caused the action, through which agent, under what authority, and whether it was allowed or denied. Write the record where the access decision is made rather than after the call returns, so denials appear in it too.
---

## TL;DR

- One question decides most of the rest: does the agent act **for a person**, or **as itself**? The credential, the audit record, and the meaning of "revoke" all follow from the answer.
- There are four access patterns, not a spectrum. A shared credential the agent holds directly. Per-user OAuth, where the agent carries a token a named human granted. A service account or app identity with permissions of its own. Brokered access, where the agent holds nothing and a control layer decides per request.
- Ship read-only, prove the workflow against real data, then widen. The reverse ordering fails for an organizational reason rather than a technical one: widening is one additive consent, and narrowing is a revocation plus a re-consent rollout that nobody owns.
- Almost nothing about the protocol changes as you grow. Revocation, rotation, offboarding, and the auditor's question — which human caused this, through which agent, under what authority — are what change, and none of them are integration work.
- The failure this pillar exists to prevent is not a breach. It is an incident nobody can size, because access was granted broadly and no record ties an action to an agent and the person it acted for.

## Who this is for

You are giving an agent access to systems that hold other people's data — mailboxes, workspaces, repositories, documents, databases — and at some point someone will ask what it could reach and on whose authority. This page is the pillar's front door: it names the four patterns, works one requirement through all of them, and hands off to the guide that covers each system in detail. Skip it if your agent only touches its own working data under a single identity that will never change, because most of what follows is about the moment that stops being true.

## The problem

The failure this pillar exists for is not the one people brace for. It is not a stolen token or a dramatic breach. It is an incident that nobody can size.

Here is how it assembles, and every step is reasonable in isolation. An engineer wires an agent to a mailbox. The narrow scope returns a 403 on the third call, late in a sprint, and the error does not say which permission is missing — Gmail returns insufficient scope as a 403 in practice, alongside its throttling errors, and GitHub returns the generic `Resource not accessible by integration`. The fastest way to find out whether scope is the problem at all is to widen the scope and see if the failure goes away. It goes away. That diagnostic experiment is now the configuration, and the change that shipped it was three characters long.

The credential goes in an environment variable, because that is where credentials go. A second agent needs the same system a month later and reads the same variable, because minting a second credential means a second approval from a team that is busy. A third one arrives with a feature nobody wrote a design doc for. None of this is carelessness. Each step is the cheapest correct-looking move available at the moment it is made.

Then something happens. It does not matter much what: a dependency ships a postinstall script, a prompt log turns up in a support ticket, a model follows an instruction it read in the body of a customer's email. The first question is always the same one, and it is asked within an hour: *what could it have touched?*

You cannot answer it, and the reason splits cleanly in two.

The first half is reach. The grant is per-mailbox, per-account, or per-repository, because that is the unit providers sell. There is no scope that means "only the records this task needs". So the honest upper bound on what the agent could read is everything the credential can read, which on a mailbox is every message including the ones about a lawsuit, a salary, and a customer's bank details.

The second half is attribution, and it is the one that turns a bad afternoon into a bad quarter. One credential was used by three agents on behalf of an unknown number of people. The provider's log records one principal — the account the key belongs to, or the human whose token it was — for all of it. Nothing in that record says an agent was in the middle, and nothing says which request from which person caused which call. So you cannot subtract the legitimate traffic from the total, which means you cannot bound the incident, which means the response has to be sized for the maximum: rotate everything, notify everyone, and tell a customer you are unable to say whether their data was in scope.

That last sentence is the actual cost of getting access wrong, and it is paid in an incident review rather than in an outage. It is also why this pillar treats attribution as a property of the integration rather than a logging concern bolted on afterwards. The [failure pillar](/fail/) collects what these incidents look like from the inside, and [over-scoped OAuth](/fail/over-scoped-oauth/) is the specific diagnostic path for a grant that is already live and already too wide.

Everything downstream of that failure is decided by one question, so ask it before you choose a mechanism: **does the agent act for a person, or as itself?**

## Step by step

This is a decision walkthrough rather than one integration, because the integration is the easy part. Take a real requirement and work it end to end:

> The agent should triage support email for each of our customers' users.

Read that sentence carefully, because two words in it eliminate half the options before you write any code. *Each* means per-user, not a shared inbox. *Our customers' users* means the mailboxes belong to people who do not work for you, in Google Workspace domains you do not administer.

### 1. Answer the identity question before you choose a mechanism

The test is not philosophical. Ask what should happen when the person leaves the company. If the correct answer is "the agent's access to their data ends", the agent was acting for them, and its authority has to be traceable to a grant that person made. If the correct answer is "nothing changes", the agent was acting as itself, and it needs an identity of its own.

Ask the same question a second way, from the audit side: after the fact, who should the provider's own log name? If the honest answer is a human, the agent needs that human's authority. If the honest answer is "the automation", the agent needs its own.

For the support-email requirement, both tests come back the same way. The mailbox belongs to a person, that person's departure should end the agent's access to it, and the record should say the agent read their mail with their permission. The agent acts for a person. That single answer removes the shared-credential and service-identity patterns from serious consideration, and the rest of this walkthrough is about what remains and what it costs.

Delegation has a precise meaning here that is worth taking from the [govern pillar](/govern/) rather than inventing: acting *for* a user keeps the agent's own identity and carries two principals in the request, while acting *as* a user makes the call indistinguishable from one the user made. [Delegated identity](/govern/delegated-identity/) works through the mechanism, including the rule that the agent's effective permissions should be the intersection of what the agent may do and what the user may do, never the union.

### 2. Put all four patterns against the same integration

Four patterns, one requirement.

**A shared credential the agent holds directly.** One API key, one database password, one app password, read from the environment at startup. For the mailbox requirement this is an IMAP or SMTP app password on a shared `support@` address, and it is the only pattern you can ship this afternoon. It costs almost nothing to build and everything to reason about later. There is no scoping, because a password-equivalent secret has no scope mechanism to narrow: whatever the account can do, the holder can do, and there is no read-only variant to ask for. There is no per-action audit — the provider sees one client authenticating as one account. Who gets blamed: nobody, or whoever's name is on the account, which in practice is the engineer who created it. It stops working at the size where more than one person can deploy: the moment two agents or two humans need different reach, one credential cannot express the difference, and nothing in the provider's record can separate them afterwards. It also fails the requirement outright here, because there is no single mailbox to point it at. The database version of this pattern is the one that lasts longest and hurts most: a connection string with a password in it is a bearer token with no scope, no expiry, and no per-action audit, which is why [letting an agent query your database safely](/connect/databases/) spends its length on roles, column grants, and row-level security keyed to the acting user instead of on the connection.

**Per-user OAuth, where the agent carries a token the user granted.** Each user is sent through a consent screen, grants a named set of scopes, and the agent stores a refresh token per user per agent. For the mailbox requirement this is the real shape: `https://www.googleapis.com/auth/gmail.readonly` to triage, plus `https://www.googleapis.com/auth/gmail.send` later if the agent replies, with the refresh token encrypted at rest and keyed by user. The properties are the ones you want. The grant is visible to the person who made it and revocable by them without an administrator. It is bounded by their own access, so the agent inherits their permissions rather than exceeding them. Who gets blamed: the user, and that is the pattern's own weakness — the provider's record names the human, and nothing in it says an agent was in the middle, so the binding from action to agent is yours to write. What it costs is storage and lifecycle, and the cost is linear in users times providers. It stops working when that multiplication outgrows the team that maintains it; [connecting an agent to Gmail](/connect/gmail/) puts the inflection at around fifty users, which is a judgement rather than a measurement, and it is the guide to read for the full path from consent screen to reply, including the seven-day refresh token that catches everyone once — a property of an OAuth client with an external user type left in Testing publishing status, not of Gmail, and fixed by publishing to production.

**A service account or app identity with permissions of its own.** The agent is a first-class actor: it has an account or an installed application, permissions are granted to that actor, and its actions appear under its own name. GitHub's version is the clean one — an app registered for one agent, installed on selected repositories, minting an installation access token that expires after one hour and can be narrowed further at mint time to named repositories and a subset of permissions. [Giving an AI agent access to GitHub](/connect/github/) walks that mint flow claim by claim and explains why a classic personal access token is the wrong shape for an agent. Google's version is not the same thing wearing different words. A Workspace service account reaches a mailbox only by impersonating its owner through domain-wide delegation, which a super administrator authorizes and in which, as Google's own page puts it, users aren't directly involved. That is per-user access with the consent removed, which is exactly why reaching for it because consent is inconvenient is the wrong instinct. It also settles the revocation column of the table below. Google's admin documentation describes domain-wide delegation as granting client applications access to your Workspace users' data "without requiring their consent", and says you must be signed in as a super administrator to manage or delete a delegation. So the only lever is the Admin console, and because the user never consented there is nothing on their side to withdraw. Who gets blamed: the app, in the clean case, which is correct for unattended work and wrong for user-initiated work — GitHub's own best-practice guidance is that an app taking an action on behalf of a user should use a user access token rather than an installation token. It stops working when one app serves several agents, because the app's permission set becomes the union of what all of them need and suspending it for one misbehaving agent stops the rest. For the support-email requirement it fails on arithmetic: domain-wide delegation would need a super administrator in every customer's domain to authorize your client id, and would make you a standing impersonator in each one.

**Brokered access, where the agent holds nothing.** The agent holds a token for a control layer and names a connection. The layer holds the provider credential, evaluates policy for that specific request — this agent, acting for this user, calling this method with these parameters — makes the call, and records it against both principals. The pattern is not an alternative to the other three so much as a different answer to *where the credential lives and who decides*: the grant underneath is still per-user OAuth, or still a service identity, and every scope decision above still applies unchanged. What you get is that a compromised agent process leaks a revocable gateway token instead of a standing grant on somebody's mailbox, and that the answer to "which agent did what for whom" comes from one place rather than being reassembled from three log streams. Who gets blamed: the agent and the user together, in your record. Be honest about the limit — the provider's own log still shows whatever credential the layer presented, so brokering fixes your record, not theirs. What it costs is a dependency, a policy model somebody maintains, and a hop in the call path. It stops making sense in the other direction from the rest: at one integration and one user it is more machinery than the problem deserves.

### 3. Select scopes read-only first, and know why the reverse does not work

Now the permission list, and there is only one ordering that survives contact with a security review. Request the narrowest read scope the workflow could possibly run on, run the real workload against real data, and let the failures tell you what you need. That gives you a permission list derived from the agent's actual calls rather than from someone's estimate of them. GitHub even hands you the derivation: a 403 carries an `X-Accepted-GitHub-Permissions` header naming the permission sets that would have worked.

The reverse ordering — grant broadly, narrow once it works — is the intuitive one, and it fails for a reason that has nothing to do with engineering. Widening a grant is one additive consent screen. Narrowing it is a revocation, a re-consent, and a rollout: Slack states plainly that there is no way to remove scopes from an existing token without revoking it entirely, so getting from `chat:write.public` back to `chat:write` means every installed workspace goes through the flow again. That work needs a prompt, a support plan, and an owner, and the engineer who noticed the excess scope owns the code rather than the users. So the ticket has no home and does not move. The [least-privilege guide](/govern/least-privilege/) makes this its central argument and gives you the metric that survives an argument — the fraction of granted scopes exercised over a window long enough to contain everything the agent does periodically — because unexercised scope is the only over-grant you can delete without convincing anybody.

One exception is worth knowing because it is free money: GitHub documents that removing permissions from a GitHub App takes effect immediately, with no approval from installing accounts. Removal is unilateral and instant there, which makes it the cheapest narrowing available anywhere and the one with no excuse for being deferred.

Then the part the scope list does not tell you. A scope says what kind of thing the agent may touch; something else decides which ones. On Slack the second gate is channel membership — `channels:history` grants messages in public channels "that your Slack app has been added to", so an `/invite` typed by anyone extends the agent's reach without a deploy, and [connecting an agent to Slack](/connect/slack/) covers both gates plus the request-signature verification that keeps your events endpoint from being an unauthenticated instruction channel. On Notion the second gate is a page share that is inherited: sharing a parent page with a connection grants access to all of its child pages, so one click by someone in operations can hand over a subtree nobody has inventoried. [Agent access to Google Drive and Notion](/connect/drive-and-notion/) is the guide for grants that arrive by container rather than by scope string, and for the `drive.file` plus Picker pattern that grows a Drive grant one file at a time.

For the support-email requirement, the scope answer is `gmail.readonly` first and `gmail.send` only once you can name every message the agent would have sent. It also carries a schedule consequence that belongs in the plan rather than in a surprise: Google classifies the Gmail scopes that read a mailbox as restricted, so a public production launch across your customers' domains needs OAuth verification, plus a security assessment because message text reaching a hosted model is restricted-scope data reaching a third-party server. Put weeks on the calendar, not days.

### 4. Decide where the credential lives before you write the integration

The credential outlives the code that first used it, so choose its home first. Three properties matter, and none of them are about encryption.

The first is whether the store is per-user. An environment variable is a per-process constant; a per-user entitlement is a per-request variable. They cannot be the same object, which is the whole reason a module-level API key is a single-tenant design wearing a multi-tenant agent. [Tool integration in LangChain, CrewAI, and OpenAI Agents](/connect/frameworks/) shows the per-invocation seam each framework gives you for this — `ToolRuntime[Ctx]`, `RunContextWrapper[Ctx]`, and in CrewAI the tool instance itself — and why the identity must arrive on a channel the model cannot write to.

The second is whether the store has a revocation path that somebody has executed. Rotation and revocation are different levers, and rotation on its own leaves a leaked credential working until the overlap window closes. [Credential handling for agents](/govern/credentials/) works through the ordering, including the point that pulling a long-lived secret from a vault at startup gets it out of your repository without changing what it reaches or how long it lives.

The third is whether the credential can reach anything without a request in hand. If a background thread, a debug endpoint, or a retry worker can pick up the credential with no user attached, then at some point it will act for the wrong person.

For the support-email requirement this is the decision that actually costs money. You will hold one Gmail refresh token per end user of every customer, encrypted at rest under a key you rotate, excluded from logs and crash reports, and deleted the same day that user is offboarded from a company you do not run. That is not a Gmail problem. It is the credential lifecycle problem, and it will be identical for the second provider you add.

### 5. Decide what the audit record must contain, before the incident

Write the record before you need it, and write it where the access decision is made rather than after the call returns — a trail with no denials in it is not evidence that nothing was denied. The [audit guide](/govern/audit/) grades any log by whether it answers four clauses: which human caused this, through which agent, under what authority, and could you have stopped it. Treat a clause your log cannot answer as a missing field rather than a hard query.

Two provider-side gaps are worth knowing now rather than during an investigation. Under per-user OAuth the provider records the human and not the agent, so the agent side of the binding is yours to supply. Under a service or app identity the provider records the app and not the requester, so the human side is yours. Neither gap closes on its own, and reconstructing either one from chat history six months later is the work nobody has time for.

For the support-email requirement, the record has to answer "which agent read whose mail, when, and under whose grant" without anyone grepping application logs — and it has to answer it for a customer's auditor, not just your own.

### 6. Land on a pattern, and write down why the others lost

Here is the decision for the stated requirement, with reasons.

**Per-user OAuth is the grant.** Step 1 established that the agent acts for a person. The mailbox belongs to that person, they can see and withdraw the grant themselves, and the agent's reach is bounded by their own access rather than by an administrator's. A shared credential lost immediately because there is no shared mailbox and no read-only app password. A service identity lost because on Gmail it only reaches a mailbox by impersonating its owner, and doing that across many customer domains means a super administrator in each one authorizing you as a standing impersonator — a larger, less revocable grant obtained to avoid a consent screen.

**Brokered is where the credential lives.** This is the part people treat as an either/or and it is not one. The grant is still per-user OAuth; the question step 4 asked is where the refresh tokens sit and who evaluates the request. Holding thousands of standing mailbox grants inside an agent process, across customers, with rotation and same-day offboarding, is the lifecycle problem rather than the integration problem, and it is the same problem again for every provider you add next.

**Scopes are `gmail.readonly` until the reply feature is real**, with `gmail.send` added by a second consent when it is. Restricted-scope verification is on the schedule from day one, not discovered at launch.

**What would change the decision.** If the product moved to a single shared `support@` mailbox per customer with no per-user requirement, a service identity becomes defensible and the per-user token store disappears. If you only ever served one Workspace domain that you administer, domain-wide delegation stops being a standing cross-tenant impersonation and becomes an ordinary internal automation. Write the trigger down next to the decision, because the decision will outlive the person who made it.

## Decision table

| Pattern | Attribution | Blast radius | Revocation | Per-user support | Operational cost |
| --- | --- | --- | --- | --- | --- |
| **Shared credential the agent holds** — wins when one agent does one job against a system that holds nothing worth attributing. | One principal for every agent and every user. The provider cannot separate them. | Everything the credential can reach, with no filter available. | Rotate the secret and restart everything that holds it. All-or-nothing. | None. One identity by construction. | Lowest to build, highest to reason about. No expiry, no scoping, no per-action record. |
| **Per-user OAuth** — wins whenever the agent acts for a named person who should be able to see and withdraw the grant. | The human. The agent is invisible in the provider's record unless you record it yourself. | Bounded by that user's own access and the granted scopes. | The user can withdraw their own grant, and you can revoke the token. Per user. | Native. This is what the pattern is for. | Linear in users times providers: consent flow, encrypted token store, refresh, re-consent, offboarding. |
| **Service account or app identity** — wins for unattended work with no human in the request, where the provider's record should name the automation. | The app or account. The requesting human is missing unless you record it yourself. | The permissions granted to that actor, which drift wider as it is installed in more places. | Suspend or uninstall the installation, or disable the account. Per actor, not per user. | Only by impersonation, which removes the user's consent and their ability to revoke. | Moderate: one key or app to protect, permission review, and a mapping from action to requester that you build. |
| **Brokered through a control layer** — wins once the credential lifecycle across users and providers costs more than the integrations do. | The agent and the user it acted for, in your record. The provider still sees the credential the layer presented. | Unchanged underneath; what shrinks is what a compromised agent process holds. | Revoke the agent's gateway token immediately; the provider grant is revoked in one place. | Yes, and it is the reason to adopt it. | Highest up front, flattest afterwards. A dependency, a policy model, and a hop in the call path. |

Two honest readings of that table.

The first: it is not a ranking. A shared credential on a system that holds nothing sensitive, used by one agent for one job, is a reasonable engineering decision and pretending otherwise costs you credibility when you argue about the case that matters. The pattern is wrong in proportion to how much the system holds and how many actors touch it.

The second: the last row is not a fourth alternative to the three above it. Rows one through three answer *whose authority does this call carry*. Row four answers *where does the credential live and who decides per request*, and it can sit under any of the others. You pick one answer to the first question and then decide the second separately, which is why the walkthrough above landed on two rows rather than one.

## Checklist

- [ ] For each integration, you can state in one sentence whether the agent acts for a person or as itself, and the sentence matches the credential you chose.
- [ ] No agent shares a credential with another agent, and no agent shares an OAuth client or app registration with a user-facing product.
- [ ] Every scope, permission, or grant in the request maps to a specific API call you can name, and a reviewer can check that mapping in the diff.
- [ ] The first production run used read-only permissions, and the widening after it was a separate, reviewed change.
- [ ] The code reads back what was actually granted — the `scope` field of a token response, the `permissions` and `repository_selection` of an installation token — instead of assuming the request was honoured in full.
- [ ] Credentials are stored per user where the grant is per user, encrypted at rest, and excluded from logs, traces, and crash reports.
- [ ] No identity the agent acts under can be supplied as a tool argument, and a grep for arguments named `user_id`, `tenant`, `account`, or `on_behalf_of` returns nothing you cannot justify.
- [ ] Revocation has been executed at least once by a human, not just designed, and offboarding a user revokes their grant the same day.
- [ ] Rotation has a runbook someone has followed end to end, including the overlap window and the step that retires the old credential.
- [ ] A terminal authorization failure — `invalid_grant`, `token_revoked`, `account_inactive` — stops the run and marks the connection dead instead of entering a retry loop.
- [ ] You can answer, from a queryable record rather than application logs, which agent took which action for which human under which grant, and which actions were denied.
- [ ] Content fetched from the connected system is truncated or redacted before it reaches a model prompt, and no tool call is issued because retrieved content said to issue it.
- [ ] Each integration has a written trigger for revisiting its access pattern, so the decision survives the person who made it.

## Failure modes

### The grant is wider than the job and nothing ever errors

**Symptom:** none, operationally. The tell is administrative: an access review that answers "full access" instead of naming a resource set, and an incident whose blast radius cannot be stated as a number.

**Cause:** the broad grant was a debugging result rather than a decision, as in *The problem*, and it survived because narrowing is a rollout rather than a diff.

**Fix:** compute the unexercised set — granted scopes from the provider's grant listing, exercised methods from an audit log, subtract — and retire what is left, through the staged re-consent rollout in [over-scoped OAuth](/fail/over-scoped-oauth/) — unexercised scope is the one over-grant that needs no argument, but removing it is still a revoke, a re-consent, and a cohorted rollout with an abandonment state to handle, not a delete. Then close the door behind you: alert at grant time when a new grant carries a scope outside the declared allowlist for that client.

### The audit trail names a person for something software did

**Symptom:** an investigation into an action nobody claims lands on a named employee. The provider's transcript, its export, and its search results all show that person.

**Cause:** the agent was given a user-scoped credential for work it did on its own initiative. Slack's rule is the sharpest version: write actions taken with a user token are performed as if by the user themselves, and Slack's messaging documentation names `user` and `bot_id` as a message's author fields and nothing that records an app acting on someone's behalf.

**Fix:** where the agent acts on its own initiative, give it its own identity — a bot token, an app installation — so the provider's record is honest. Where a user-scoped credential is genuinely required, keep your own request log binding each call to the agent, the human it acted for, and the reason, because the provider's record will not contain that binding and no logging you add downstream can retrofit it into their transcript.

### The identity arrived as a tool argument

**Symptom:** the agent acts for a user nobody asked it to act for, and the run that did it had just read a ticket, an email, or a retrieved document. Nothing errored, because the call was well formed and the credential was valid.

**Cause:** someone added `user_id` as a tool parameter so the tool could look up the right credential. That parameter now sits in the same context window as text the agent read from a ticket, an email, or a retrieved document, so anything that can influence the model can influence which user's credentials get used. This is the confused deputy in its plainest form: the program has authority, and something else chose the target.

**Fix:** the identity must arrive on a channel the model cannot write to — the framework's per-invocation context object, a validated access token, or the host-controlled process environment — and every target must be resolved against that identity's own entitlements rather than accepted as an argument. [Building and securing MCP servers for agents](/connect/mcp-servers/) works the same rule through a protocol that defines an optional OAuth 2.1 framework for authenticating the caller on HTTP transports and never decides whether that caller may run that tool with those arguments, including the argument classes a model must never supply. A second copy of the rule lives in the database layer: carry the acting user into the session as a bound parameter and let row-level security enforce it, never let the model choose the value.

### Offboarding removed the human and left the grant working

**Symptom:** an employee left three months ago and an agent is still reading their mailbox, their Drive, or their Notion pages. Nobody notices until an access review.

**Cause:** the grant is an object in your storage, not in their employment record. Deprovisioning ran against the identity provider and never touched the per-user refresh token you hold, the Notion connection somebody added to a page, or the shared drive membership a service account still has.

**Fix:** make revocation a step in the offboarding runbook with a named owner, covering every provider the agent touches in one pass, and verify it by trying to use the credential afterwards rather than by checking that the delete returned 200. Then schedule the inverse audit — enumerate what each agent can currently reach and compare it against intent — because grants that arrive through a UI, like a Notion page share or a Slack channel invite, will never appear in a deprovisioning script.

### Revoking one agent takes the others down with it

**Symptom:** an incident requires you to cut off one misbehaving agent, and doing so stops three unrelated automations and signs users out of your product.

**Cause:** one OAuth client, one app registration, or one API key serving several consumers. Suspension, revocation, and rotation are all per-credential, so the blast radius of the fix is the union of everything that credential serves.

**Fix:** one credential per agent, always, and one app or OAuth client per agent where the provider's registration model allows it. The cost is a few minutes of setup at the start and it buys you the ability to act during an incident without negotiating. Where a credential is already shared, splitting it is cheaper before the incident than during one.

### The broker becomes the thing that holds everything

**Symptom:** the layer has a bad ten minutes and every agent in the company stops at once, or a review asks who can read the credential store and the list is longer than anyone expected.

**Cause:** brokering moves credentials out of agent processes and into one layer, which is the intended trade. The layer is now a high-value target, its availability is on the critical path of every agent call, and its policy model is a piece of software with its own bugs.

**Fix:** treat the layer as production infrastructure rather than as a convenience — its own access controls, its own audit trail, its own rotation schedule, and a tested answer to what happens when it is unavailable. And keep the parts brokering cannot do: no layer can know that `account_id` should not have been a tool parameter, and none of the scope and permission decisions in step 3 become less important because a broker holds the token.

## Doing this at scale

Almost nothing about the wire protocol changes as you grow. What changes is everything around it.

Revocation stops being a button and becomes a fan-out: one departing user, one compromised agent, or one customer leaving has to reach every provider that holds a grant for them, in one pass, with evidence it worked. Rotation stops being a task and becomes a schedule with an overlap window whose length is the longest lifetime any outstanding credential can have — retire the old credential before that window closes and you have caused the outage you were preventing. Offboarding stops being a checkbox in your identity provider and becomes a query across grants that were made in five different user interfaces by people who do not work for you. And the auditor's question arrives in a form your application logs cannot answer: not "show me the logs" but "show me every action this agent took for this person last quarter, and every action you denied it."

None of those four are integration work. They are the same work again for every provider you add, which is why the per-integration cost is small and the total is not. That is the shape of problem a control layer exists for, and it is worth stating what one does before saying who makes one: the agent holds a token for the layer rather than provider credentials, it calls a connection by name, the layer resolves the credential and evaluates policy for that specific request, and it records the call against both the agent and the human it acted for.

[Agentic Fabriq](https://agenticfabriq.com) is that layer, built for agent identity, permission policy, credential vaulting, and audit. The property worth the dependency is not the shorter code but the two named in step 2, arriving at the scale where the fan-out above is the actual work.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # What this agent is permitted to reach, decided centrally rather
        # than by whatever credentials happen to be in its environment.
        for tool in await af.list_tools():
            print(tool)

        for agent in await af.list_agents():
            print(agent)

        result = await af.invoke_connection(
            "support_mail",
            method="get_emails",
            parameters={"max_results": 10, "q": "is:unread"},
        )
        for message in result.get("emails", []):
            print(message)


if __name__ == "__main__":
    asyncio.run(main())
```

The runnable version is `examples/connect-index/brokered_access.py`, which is the block above with the two literals lifted into constants and these caveats repeated in its docstring. Two caveats before you copy it. `af_sdk.fabriq_client` comes from the SDK's published documentation, and the published package has lagged its own README, so check the module exists in the version you installed with `python -c "import af_sdk.fabriq_client"` before writing code against it. And connection names, method names, and response shapes are per-deployment: run `afctl tools list` against your own gateway rather than trusting `support_mail`, `get_emails`, or the `emails` response key here.

Be exact about the division of labour, because a layer is not a substitute for the work in steps 3 through 5. The scope you request, the permission you grant, the row-level policy you write, and the argument you refuse to expose as a tool parameter are all still yours, and they are still the only thing standing between a rewritten request and your data. What a layer like [Agentic Fabriq](https://agenticfabriq.com) removes is the credential lifecycle and the attribution gap. Everything above this section stays correct if you would rather own that lifecycle yourself, and plenty of teams should.

## Further reading

Each of this pillar's seven guides is linked above at the point it is earned, so read them in the order your integration needs rather than front to back. If you want a single starting point, [connecting an agent to Gmail](/connect/gmail/) is the most complete worked example of per-user OAuth on this site, from consent screen to reply, and every other provider is a variation on it.

Two pillars carry the parts that are not per-integration. The [govern pillar](/govern/) treats identity, scope design, credential handling, and audit as policy problems you solve once rather than decisions you re-make per provider — [OAuth flows for AI agents](/govern/oauth-flows/) is the one to read next if your agent already holds a user's token and calls a second service with it, because forwarding that token is the mistake this whole pillar is arranged against. The [fail pillar](/fail/) collects what these integrations look like after they go wrong, including [token leakage](/fail/token-leakage/) for credentials that reach a context window and never come back out.

Primary sources for the external claims made on this page:

- [Gmail API OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) — the `gmail.readonly` and `gmail.send` scope strings and their restricted and sensitive classifications.
- [Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) — OAuth verification, the security assessment when restricted-scope data reaches a third-party server, and the exemptions.
- [Handle Gmail API errors](https://developers.google.com/workspace/gmail/api/guides/handle-errors) — the 403 reason strings, which is where the insufficient-scope failure lands in practice even though Google's error guide documents token problems under 401.
- [Using OAuth 2.0 for server to server applications](https://developers.google.com/identity/protocols/oauth2/service-account) — domain-wide delegation, the super-administrator authorization steps, and the statement that users aren't directly involved.
- [Control API access with domain-wide delegation](https://knowledge.workspace.google.com/admin/apps/control-api-access-with-domain-wide-delegation) — that delegation grants access to your Workspace users' data "without requiring their consent", and that managing or deleting a delegation requires signing in as a super administrator.
- [Installing with OAuth](https://docs.slack.dev/authentication/installing-with-oauth/) — scopes being additive on reinstall, and that there is no way to remove scopes from an existing token without revoking it entirely.
- [Token types](https://docs.slack.dev/authentication/tokens/) — the rule that write actions with a user token are performed as if by the user themselves.
- [Retrieving messages](https://docs.slack.dev/messaging/retrieving-messages/) — that a retrieved message carries author fields "such as `user` or `bot_id`", which is the whole of what Slack documents about message attribution.
- [Permission scopes reference](https://docs.slack.dev/reference/scopes/) — `channels:history` as messages in public channels the app has been added to.
- [Generating an installation access token for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app) — the one-hour expiry and the `repositories` and `permissions` narrowing parameters.
- [Editing a GitHub App's permissions](https://docs.github.com/en/apps/maintaining-github-apps/editing-a-github-apps-permissions) — that removing permissions takes effect immediately, while adding them requires each installing account to approve.
- [Best practices for creating a GitHub App](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app) — the guidance that an app acting on behalf of a user should use a user access token rather than an installation access token.
- [Troubleshooting the REST API](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api) — the `Resource not accessible by integration` message and the `X-Accepted-GitHub-Permissions` header.
- [Notion authorization](https://developers.notion.com/docs/authorization) — that access to a parent page provides access to all available child pages.
- [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) — indirect prompt injection, and the privilege-control and human-approval mitigations that make the argument in *The identity arrived as a tool argument*.
