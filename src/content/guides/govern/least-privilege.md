---
title: Designing Least-Privilege Scopes for Agents
description: "A repeatable method for scoping an AI agent: enumerate the actions from its tool definitions, map each to the narrowest scope, remove the rest, and ratchet."
pillar: govern
updated: "2026-08-18"
keywords:
  - least privilege ai agent
  - agent scope design
  - oauth scope review
  - agent tool permissions
  - just-in-time access
  - unused scope audit
relatedGuides:
  - govern/oauth-flows
  - govern/audit
  - fail/over-scoped-oauth
howTo:
  - name: Enumerate the actions from the tool definitions
    text: List every tool the agent can call and the exact API call each one makes, because the tool registry is the permission set whether or not anyone reviewed it as one.
  - name: Map each action to the narrowest scope that permits it
    text: Look up the provider's own scope reference and record the smallest scope that covers each call, plus what that scope grants beyond the call.
  - name: Remove everything else and run
    text: Mint a fresh credential holding only the mapped scopes, disable retry on 403, and run the real workload rather than a smoke test.
  - name: Capture the failures as the real requirements
    text: Triage each denial into a scope the agent genuinely needs, a bug in the code, or a tool that should not exist, and delete the third category.
  - name: Ratchet, widening only with a recorded reason
    text: Make every scope a line in a checked-in manifest with a justification and a reviewer, and re-run an unexercised-scope report every 30 days.
faq:
  - question: What does least privilege actually mean for an AI agent?
    answer: That every scope, permission, and tool the agent holds can be traced to a specific API call the agent makes, and that nothing else is granted. The practical test is whether you can name the call that would break if the scope were removed.
  - question: Should I start with broad scopes and narrow them later?
    answer: No, and in practice the narrowing never happens. Widening a grant is cheap and additive, while narrowing means revoking the grant, re-running consent, and risking a production break that nobody is rewarded for preventing. Start narrow, when the cost of being wrong is a stack trace during development.
  - question: How do I measure whether an agent's scopes are least privilege?
    answer: Measure the fraction of granted scopes that produced at least one call, reviewed every 30 days but evaluated over a window long enough to contain every periodic thing the agent does. Unexercised scope is the only over-grant you can remove without an argument, because the evidence, rather than an opinion, says it was unused.
  - question: Are an agent's tool definitions part of its permissions?
    answer: Yes. Every tool an agent can call is a permission you granted, because the model chooses when to call it and you chose what is callable. Adding a tool to the registry widens the agent's authority exactly as much as adding a scope does, so the tool list belongs in scope review.
  - question: What do you do when the narrowest scope a provider offers is still too broad?
    answer: Add a resource-level constraint and a time bound. A scope says what kind of thing the agent may touch; a policy says which specific ones, and a short-lived credential says for how long. Record the residual over-grant in writing so the next reviewer knows the question was already asked.
---

## TL;DR

- Run the method in one direction only: enumerate the actions from the tool definitions, map each to the narrowest scope that permits it, remove everything else, run, and treat the failures as the requirement list.
- Start broad and narrow later does not work, and the reason is organizational rather than technical. Widening is one additive consent; narrowing is a revocation, a re-consent, and a chance of breaking production that nobody gets credit for avoiding.
- Scope granularity varies wildly by provider. Zendesk lets you ask for `tickets:write` and `users:read` and nothing else; Linear's `read` scope "will always be present" and covers the whole account. Same intent, no equivalent expression.
- A scope says what kind of thing; a policy says which ones. Slack's `channels:history` covers "public channels that your Slack app has been added to" — the scope is half the grant and channel membership is the other half.
- Measure the fraction of granted scopes exercised, on a 30-day cadence over a window that contains every periodic thing the agent does. Unexercised scope is the metric that matters because it is the only over-grant you can delete without winning an argument.

## Who this is for

You are about to grant an agent access to a production system, or you have already granted it and someone has asked what it can do. This guide is a method you can run today against a real agent, plus a review checklist a reviewer can apply to a pull request. Skip it if your agent runs entirely against a sandbox with synthetic data, or if you are looking for the OAuth mechanics themselves rather than the question of which scopes to put in the request.

## The problem

The advice is universal and the practice is rare, and that gap is the whole subject. Everyone agrees agents should hold minimum permissions. Then a scope ships that nobody can justify, and nobody removes it. The reason is not that engineers disagree with least privilege; it is that the workflow that produces scopes runs in the opposite direction from the one that would fix them.

Here is how a scope actually gets chosen. An engineer is integrating an agent at 6pm, a call returns 403, and the fastest way to make it stop is the scope at the top of the provider's list — the one that covers everything. The agent works. The pull request describes a feature, not a grant. Nobody in review has the provider's scope reference open, and the diff shows one changed string in a config file.

Now consider what it would take to reverse that. You would have to determine which of the agent's calls needed the wide scope, which means either reading every code path or removing the scope and watching what breaks in production. Then you would revoke the existing grant and send every user back through a consent screen — a visible interruption to people who have no idea why it is happening. Then you would carry the risk that you missed a path, and the failure lands on you, at 3am, in a system that was working.

The asymmetry is baked into the protocols. Google's OAuth supports incremental authorization: set `include_granted_scopes=true` and, in Google's words, "the new access token will also cover any scopes to which the user previously granted the application access." Widening is one parameter and one consent screen, and the old grant survives. Google documents no decremental equivalent, and I could not find one at any of the providers in this guide: removing a scope means revoking the grant and consenting again from scratch. The cheap direction is up.

Layer the incentives on top. Shipping broken triage is visible in minutes and someone is paged for it. An over-scoped token is invisible until an incident, and the incident may be a year away and land on a different team. Scope reduction appears in no backlog, has no owner, and no test fails when it is skipped. The MCP project's own security guidance names the end state directly, listing "scope inflation blindness: lack of metrics makes over-broad requests normalised" among the risks of poor scope design.

So the only ordering that survives contact with an organization is narrow-first — not because engineers are more virtuous at the start, but because at the start the cost of being too narrow is paid within the hour, by the person who caused it, with a stack trace pointing at the exact call. That is the cheapest possible feedback loop for exactly the same information, and it is available only once.

The concrete damage is worth naming. An agent granted `https://mail.google.com/` to read a support inbox can permanently delete every message in the mailbox, bypassing trash. An agent granted Linear's `write` scope because it needed to file issues holds "write access for the user's account". Linear makes the narrower argument itself for the adjacent case — "if your application only needs to create comments, use a more targeted scope" — and the same reasoning applies to creating issues, which has its own `issues:create` scope. An agent with Zendesk's `write` can DELETE as well as POST, because Zendesk's write scope "gives access to POST, PUT, and DELETE endpoints". None of those capabilities were chosen. They arrived attached to a string, and the difference between a bad day and a catastrophic one is whether a prompt injection finds them. The [failure pillar](/fail/) collects what that looks like when it happens.

## Step by step

The method below is five steps, run in order, against one concrete agent: a support-triage agent that reads a shared support inbox, files a ticket for anything that needs human work, and posts a summary to an on-call channel. It touches three providers with three different scope models, which is what makes it a useful worked example rather than a Gmail tutorial. The artifacts it produces are in `examples/govern-least-privilege/`.

Budget half a day for the first agent and an hour for each one after, because most of the first pass is building the mapping table you will reuse.

### 1. Enumerate the actions, from the tool definitions

Do not start from the scopes the agent currently holds. Start from the tool definitions, because that is where the agent's actual authority is written down.

This is the step most teams skip, and skipping it is a category error. Every tool an agent can call is a permission you granted. The model chooses *when* to call a tool; you chose *what* is callable. A pull request that adds `delete_ticket` to a tool registry widens the agent's authority exactly as much as a pull request that adds a `tickets:write` scope — arguably more, because the scope is at least visible to a security reviewer who greps for scope strings, and the tool registration looks like ordinary application code.

Dump the list. If the agent speaks MCP, `tools/list` gives it to you directly. If tools are registered in code, grep the registration decorator. Then write one row per tool with four columns: tool name, the exact API call it makes, the verb, and the resource class it touches.

| Tool | API call | Verb | Touches |
| --- | --- | --- | --- |
| `list_unread_support_mail` | `gmail.users.messages.list` | read | message ids in one label |
| `read_message` | `gmail.users.messages.get` | read | full message body and headers |
| `find_requester` | `GET /api/v2/users/search` | read | Zendesk user records |
| `create_ticket` | `POST /api/v2/tickets` | write | new Zendesk ticket |
| `post_triage_summary` | `chat.postMessage` | write | one Slack channel |
| `label_message` | `gmail.users.messages.modify` | write | labels on an existing message |

Two things fall out of the table immediately.

First, the widest reachable call matters more than the tool's name. A tool called `find_requester` that passes a caller-supplied string into Zendesk's user search is not "look up one email address" — it is "run an arbitrary user query", and the scope you need is the scope for the general case. Write down the widest call each tool can produce, not the one you had in mind when you named it.

Second, MCP gives you a vocabulary for this but not a control. A tool definition may carry an optional `annotations` object, and the schema defines `readOnlyHint` (default `false`), `destructiveHint` (default `true`), `idempotentHint` (default `false`), and `openWorldHint` (default `true`). Read those defaults carefully: a tool with no annotations at all is, by the schema's own defaults, potentially destructive and open-world. And the specification is explicit that clients "MUST consider tool annotations to be untrusted unless they come from trusted servers." Annotations are documentation you can sort by. They are not a permission boundary, and nothing enforces that a tool marked `readOnlyHint: true` only reads.

### 2. Map each action to the narrowest scope that permits it

For each row, open the provider's own scope reference — not a blog post, not the SDK's default scope list — and find the smallest scope that covers the call. Then, in a second column, write what that scope grants *beyond* the call. The second column is the one reviewers read.

For the triage agent, the Gmail rows resolve cleanly. Reading needs `https://www.googleapis.com/auth/gmail.readonly`, which Google describes as "View your email messages and settings" and classifies as restricted. The tempting narrowing is `https://www.googleapis.com/auth/gmail.metadata` — "View your email message metadata such as labels and headers, but not the email body" — but triage classifies on the body, and the `q` search parameter cannot be used under `gmail.metadata`. So `gmail.readonly` is the honest floor here, and it is worth writing down *why* the narrower option was rejected, because that is the note that stops the next engineer from re-litigating it.

The `label_message` row is more interesting, and it is the one that changed the design. Applying a label is `users.messages.modify`, and Google's reference for that method lists three acceptable scopes: `https://mail.google.com/`, `https://www.googleapis.com/auth/gmail.modify`, and `https://www.googleapis.com/auth/gmail.modify.restricted`. The obvious narrowing, `https://www.googleapis.com/auth/gmail.labels`, is not on that list — it is non-sensitive and covers "See and edit your email labels", meaning the label objects themselves, not their application to messages. So the narrowest scope Google *describes* for putting a label on a message is `gmail.modify`, which it defines as "Read, compose, and send emails from your Gmail account." Labelling one message costs you the ability to send mail as the user.

That is the whole argument for running this step before you ship. The third scope in Google's list is not a narrowing you can take. The same method reference explains what it is for: "for administrators modifying message for users in their organization, requests require authorization with a service account that has domain-wide delegation authority to impersonate users with the `https://www.googleapis.com/auth/gmail.modify.restricted` scope." That is an admin impersonation path, not a smaller per-user grant, and the scope appears on none of Google's Gmail scope-choosing page, its OAuth scopes index, or its restricted-scopes list. The general rule stands even though this instance resolved: read the sentence, and never infer a narrowing from a scope's name.

The triage agent dropped `label_message` instead. Labelling was a convenience, and "the agent can send mail as any user in the mailbox" is not a price worth paying for a convenience. That trade only became visible because the scope was looked up before the code was written.

#### Same intent, fine scopes and coarse ones

The gap between providers is larger than most scope discussions admit. Take one intent — *the agent may create tickets and read requester records, and nothing else* — and express it against three providers.

**Zendesk, reasonably fine.** Scopes take a `resource:access` shape, where read "gives access to GET endpoints" and write "gives access to POST, PUT, and DELETE endpoints". Zendesk's reference documents both a bare form and a per-resource one — its examples include `["read"]`, `["tickets:read"]`, and `["users:read", "users:write"]` — and it is explicit that when a resource is not specified, "access to all resources is assumed". Both forms are supported; the per-resource one is always available, and you have to choose it. The scopeable resources include tickets, users, organizations, macros, triggers and webhooks. The intent becomes:

```json
["tickets:write", "users:read"]
```

Residual over-grant: `tickets:write` also permits PUT and DELETE on existing tickets. There is no create-only Zendesk scope.

**Linear, coarse in a specific way.** Linear has a targeted `issues:create` scope, described as "Allows creating new issues and their attachments" — narrower than Zendesk's write. But `read` is documented as "(Default) Read access for the user's account. This scope will always be present." You cannot drop it. The same intent becomes:

```text
read,issues:create
```

and the token can read every issue, project, and comment the user can see. The narrowing you wanted — read *only* the requester records — has no expression. The correct response is not to pretend otherwise; it is to record in the manifest that the read grant is workspace-wide and unavoidable, and to decide whether that is acceptable for this data.

**Notion, coarse on a different axis.** Notion has no OAuth scopes for this at all. An integration has capabilities: "Read content", "Update content", "Insert content", "Read comments", "Insert comments", and one of "No user information", "User information without email addresses", or "User information with email addresses".

On the create-versus-update axis Notion is *finer* than Zendesk. "Insert content" is "permission to create new content in a Notion workspace", and a connection holding it "is not able to update existing pages"; "Update content" is the mirror image, where a connection with only that capability "is able to call the Update page endpoint, but is not able to create new pages". Zendesk's `tickets:write` cannot make that distinction at all.

Where Notion is coarse is object type and resource. There is no per-object-type capability — no equivalent of asking for tickets and not users — and no capability names a particular page. So the intent becomes:

```text
Insert content
Read content
```

plus a user-information level chosen separately. `Read content` is the line to write down. It is needed because "Insert content" explicitly "does not give the connection access to read full objects", and it "gives a connection access to read existing content in a Notion workspace" — *which* content being determined entirely by the pages the connection has been added to, not by the capability.

Notice that the three blocks above are not written the same way, and that is not a typo. The wire format is per-provider: Linear documents its `scope` request parameter as a "Comma separated list of scopes", Zendesk takes a JSON array, and Google and Slack take space-delimited strings. Notion has no wire format for this at all — capabilities are checkboxes on the integration. Get the scope set right first and the delimiter from the provider's own authorization page second, because a correct scope set in the wrong delimiter fails in a way that looks like a permissions bug.

The lesson is not that some providers are bad. It is that the step-2 output is a *pair*: the narrowest scope, and the honest residual. Reviewers who only see the first half will approve a Linear `read` grant thinking it is scoped, because the word `read` sounds scoped.

#### When the narrowest available scope is still too broad

You reach this point on almost every non-trivial agent. Two mechanisms narrow further, and they compose.

**Constrain the resources, not just the action types.** A scope says what *kind* of thing the agent may touch. A policy says *which ones*. Both are required, and providers split the work differently:

- **Slack** puts it in the scope's own definition: `channels:history` is "View messages and other content in public channels that your Slack app has been added to". Channel membership is half the grant. Likewise, withholding `chat:write.public` — "Send messages to channels your Slack app isn't a member of" — means `chat:write` reaches only the channels you invited the bot into.
- **GitHub** puts it in the token. A fine-grained token or app installation is scoped to selected repositories, and an installation access token can be narrowed further at mint time: the `repositories` or `repository_ids` and `permissions` body parameters restrict the token below the installation, and GitHub is explicit that "the installation access token cannot be granted permissions that the app was not granted." Contrast the classic OAuth `repo` scope, which "grants full access to public and private repositories" — every repository the user can reach, with no per-repository option at all.
- **AWS** makes the split explicit in the policy language: `Action` is the scope, `Resource` and `Condition` are the policy. This is the model everything else is approximating.
- **Notion** puts the entire resource constraint in page sharing. Its capabilities distinguish actions but never name an object, so which content a connection reaches is decided by which pages it has been added to, where "if a connection is added to a page, then the connection can access the page's children." Add the connection at the workspace root and you have granted everything.
- **Zendesk** has the per-resource half and not the per-record half. You can ask for `tickets:write` rather than `write`, but nothing narrows that to a queue, a brand, or a set of ticket ids, and nothing splits POST from DELETE inside it. That constraint has to live outside the token.

That last case is common and deserves a blunt note. When you enforce a resource limit in your own agent code — an allowlist of endpoints, a hardcoded label filter — you have built a boundary against a *mistaken* agent, not a compromised one. A prompt injection that gets the agent to call a different endpoint walks straight through an allowlist that lives in the same process as the prompt. If the constraint has to hold under attack, it must live somewhere the agent cannot edit: a gateway, a proxy, or the provider's own policy engine.

**Bound the time.** When scope and resource constraints both bottom out above what you want, the remaining dimension is duration. This is the answer for capabilities the agent needs occasionally and should not hold continuously.

- GitHub installation access tokens "expire one hour from the time you create them." Minting one per run, with `permissions` cut down to that run's needs, means a leaked token is worth an hour of a subset of the app's authority rather than the app's whole authority forever.
- AWS STS `AssumeRole` accepts `DurationSeconds` from 900 to 43200, defaulting to 3600 and capped by the role's maximum session duration, which "can have a value from 1 hour to 12 hours." More usefully, it accepts session policies: "the resulting session's permissions are the intersection of the role's identity-based policy and the session policies", and they "cannot be used to grant more permissions than those allowed by the identity-based policy". That is a down-scoping primitive — one role, a different narrow slice per run, computed from the task.
- Zendesk is the useful counterexample, because it shows that coarse scopes do not force long-lived credentials. Its access tokens are valid for 30 minutes by default and configurable from 5 minutes to 48 hours, and its refresh tokens 30 days by default, configurable from 7 to 90. A blunt scope axis does not make the time axis blunt.
- Microsoft Entra Privileged Identity Management is the same idea as a product, and its vocabulary is worth borrowing even if you do not use it. An *eligible* assignment "requires a user to perform one or more actions to use the role"; *activation* may require multifactor authentication, a business justification, or approval; and assignments can be time-bound with start and end dates.

The agent translation: the destructive tool is registered but its credential is not held. Calling it triggers an elevation request that a human approves, the approval carries a justification and an expiry, and the credential dies when the run does.

There is also a standards-track answer to the underlying problem, worth knowing even though you cannot assume a given provider supports it. RFC 9396 specifies `authorization_details`, "a new parameter that is used to carry fine-grained authorization data in OAuth messages" — a JSON array of typed objects rather than a space-delimited list of strings, precisely because scope cannot express requests like read access to one directory and write access to one file. If a provider you depend on supports it, that is a narrowing worth taking; check its authorization server metadata rather than assuming either way.

Be honest about the cost, because it is real and it is the reason JIT gets ripped out. Just-in-time access converts a permission problem into an availability problem. An agent that needs approval to escalate a ticket at 3am does not escalate the ticket at 3am. Before you adopt it, answer what happens when nobody approves: the run fails loudly, the run degrades to a narrower path, or the run queues. If the answer is "it fails silently and we find out from a customer", JIT has made things worse.

### 3. Remove everything else and run

Now do the part that feels reckless and is not.

Mint a **fresh** credential holding only the mapped scopes. Do not edit the existing grant. An edited grant leaves you unsure what was actually removed, and on providers where consent is incremental you may find the old scopes still attached. A new OAuth client, a new fine-grained token, a new role — whatever the provider's unit of grant is, make a new one.

Then set the run up so failures are loud:

- Disable retry on 403 for the duration. Retrying a permission error with backoff turns a five-second answer into a twenty-minute mystery, and the two conditions are not distinguishable by status code alone — Google's Gmail error guide says of 403 that "these errors occur when you exceed a usage limit or the user doesn't have the correct privileges", so a blanket retry will swallow exactly the signal you are collecting.
- Log every denial with the tool name, the API call, the status code, and the provider's error reason string. The reason string is the part that tells you which scope was missing.
- Run the real workload, not a smoke test. A smoke test exercises the paths you remembered, which are the paths you already mapped. The value of this step is entirely in the paths you forgot.

Run it against production-shaped data for at least one full cycle of whatever the agent does — a full day for a daily agent, a full week if there is a weekly report path. Anything you do not exercise here you will discover in production instead.

### 4. Capture the failures as the real requirements

The denials are the output. They are a more accurate specification of the agent's permission needs than anything you or the original author could have written from memory, because they were produced by the code rather than by recollection.

Triage each one into exactly three buckets:

1. **The action is legitimate and the scope is required.** Add the scope to the manifest with the tool, the call, and a reason. This is the only bucket that widens the grant.
2. **The action is a bug.** The agent called `users.search` with an empty filter, or fetched a full message when it needed headers. Fix the code. The scope stays out. This bucket is larger than people expect on a first run.
3. **The action was never intended.** A tool exists that no workflow uses, or a code path fires on an input nobody anticipated. Delete the tool. This is the most valuable bucket and the one teams under time pressure convert into bucket one.

The discipline is bucket two and three. If every denial becomes a scope, you have reproduced the original problem with extra steps — you have just derived the over-scoped grant empirically instead of guessing at it.

One trap to plan for: absence of an error is not evidence of permission. Some providers answer a missing permission with an empty result set rather than a denial, and some return a generic not-found for a resource the credential simply cannot see. A run that returned nothing is not proof that nothing matched. Confirm the negative — call the same endpoint with a credential you know is sufficient and compare — before you record a scope as unnecessary.

### 5. Ratchet, widening only with a recorded reason

A scope set that is correct today decays. The ratchet is the mechanism that makes decay visible.

**Every scope is a line in a checked-in file.** Not a console setting, not an environment variable, not a list inside the OAuth client configuration in someone's Google Cloud project. A file, in the repository, that is diffed in review. `examples/govern-least-privilege/scope_manifest.py` is a working one: each grant carries the provider, the scope literal, the tools that need it, the exact calls, a written reason, the residual over-grant, the compensating resource limit, and who signed off.

```python
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
)
```

Running that file with `--check` rejects a row that names no tool, names no API call, offers a reason of only a few words, or carries no reviewer — and, separately, a row that admits an `over_grant` while naming no compensating limit. `resource_limit` is not required on its own: the `users:read` row is narrow enough to need no compensating control and passes with it empty. That last rule is the one that earns its keep, because it puts the burden on the grant that has already confessed to being wider than its action needs, rather than on a reviewer who has to notice.

**Removed scopes stay in the file.** Deleting a line makes the question invisible to the next reader, who will ask it again and may answer it differently. The manifest keeps a retired section: the scope, and why it went. That is how "we tried `gmail.modify` and dropped the labelling feature instead" survives the departure of the person who decided it.

**Measure the fraction exercised.** Every 30 days, ask one question of each agent: *of the scopes granted, what fraction produced at least one call?* Unexercised scope is the metric that matters, and the reason is political rather than technical. Every other over-grant argument is a judgement call — is `tickets:write` too much for an agent that only creates tickets? — and judgement calls stall. A scope with zero calls has no counterparty. Nobody has to be persuaded it is unnecessary; the log says it was unused. Make removal the default outcome and keeping it require the same written justification as adding it.

One caveat on the window, because it is where this metric produces a confidently wrong answer. Thirty days is the review cadence, not the evidence window: a scope exercised once a quarter is indistinguishable from a dead one inside thirty days, and deleting it is how you find out. Before you act on a zero, widen the window until it contains one of every periodic thing the agent does — a month-end close, a quarterly report — which is the same discipline [over-scoped OAuth](/fail/over-scoped-oauth/) applies to its own audit.

Where the evidence comes from, and what it is worth:

- **Google Workspace** has the best of the SaaS options. The Admin SDK Reports API returns OAuth token audit events via `Activities.list()` with `applicationName=token`, and among its events — `activity`, `authorize`, `deny`, `request`, `revoke` — the `activity` event carries `api_name` and `method_name` alongside `client_id` and `app_name`. That is per-call evidence, keyed to the client. Count `activity` events only: an `authorize` event lists every scope the user consented to, which is the grant rather than the usage, and counting it makes every scope look exercised.
- **AWS** goes further than anyone. IAM last accessed information gives service-level and action-level data with a tracking period of "at least 400 days" for services, IAM Access Analyzer unused access analyzers continuously flag unused services and actions across an organization, and Access Analyzer can generate a policy from CloudTrail activity for a date range. Three caveats AWS documents and you should repeat to anyone reading your report: the data "includes all attempts to access an AWS API, not just the successful attempts", action last accessed covers management events and "is not available for any data plane event", and `iam:PassRole` is not tracked at all.
- **Everyone else.** Zendesk, Linear, Notion and most SaaS providers publish no per-scope usage report I could find. For those you instrument the only place that sees every call — your own agent runtime or the gateway it routes through. If neither logs the tool name and outcome per call, that is the first thing to fix, because without it this metric cannot be computed at all.

`examples/govern-least-privilege/unexercised_scopes.py` computes the report from already-fetched activity records, so it runs with no credentials. It does not hardcode the scope set: it imports the manifest and analyses the pre-narrowing grant — everything currently granted plus everything in the retired section — which is how the two removals recorded above were justified in the first place. Run it and the two scopes it flags are exactly `gmail.modify` and `channels:history`. Read its two caveats before you trust a number it prints: the call-to-scope mapping is derived from each manifest row's own `calls` field, so an incomplete row makes a live scope look dead, and an attempted call is not a successful one.

### The scope set this produced, with a line of justification each

End state for the triage agent, which is the artifact to paste into the pull request:

| Provider | Scope | Why it is there |
| --- | --- | --- |
| Google | `https://www.googleapis.com/auth/gmail.readonly` | `messages.list` and `messages.get`. Triage classifies on the body, so `gmail.metadata` cannot be used; `q=` is also unavailable under that scope, and the list call filters on `q=label:support is:unread`. |
| Zendesk | `tickets:write` | `POST /api/v2/tickets`. No create-only scope exists; the write scope also permits PUT and DELETE, which the gateway denies at the endpoint level. |
| Zendesk | `users:read` | `GET /api/v2/users/search`, to resolve the requester id a ticket needs. |
| Slack | `chat:write` | `chat.postMessage` to the on-call channel. `chat:write.public` deliberately withheld, so the bot reaches only channels it was invited to. |

And the two grants that are *not* there, with their reasons, because a reviewer needs those more than the four above:

- `https://www.googleapis.com/auth/gmail.modify` — the narrowest scope Google describes for `users.messages.modify` in a per-user flow, but it also grants compose and send. The `label_message` tool was dropped instead. `gmail.modify.restricted` is the service-account domain-wide-delegation path for administrators, not a narrower alternative here.
- `channels:history` — added for a channel-reading feature that was cut before launch. Zero calls in 30 days; removed by the unexercised-scope report.

Six lines. Every one of them names a call, and the two most useful lines are about scopes the agent does not hold. That is the deliverable.

## Decision table

| Approach | When it wins | Blast radius | Friction | Operational overhead |
| --- | --- | --- | --- | --- |
| Broad scope with policy enforcement | The provider's scopes are coarse and there is no narrower expression, or one credential serves many agents whose needs differ. | Determined entirely by the policy engine. If the policy is bypassed, misconfigured, or lives in the agent's own process, it is the full scope. | Lowest for developers: one grant, no re-consent when a workflow changes. | Highest and permanent. You now operate a policy engine, and the policy is a second source of truth that drifts from the code. |
| Narrow scope per action | The provider offers real granularity and the agent's action set is stable. Default choice. | Small and legible. The grant itself is the bound, so it holds even if your code is compromised. | Real: every new action is a re-consent or a new credential, and users see it. | Lowest ongoing. A manifest and a 30-day report. Cost is front-loaded into the mapping step. |
| Just-in-time elevation | A capability is genuinely needed but rarely, and it is destructive — deletion, refunds, production writes, anything with no undo. | Smallest of the three, bounded by session duration as well as scope. A leaked credential expires. | Highest, and it lands on whoever approves. This is the reason it gets removed six months later. | Moderate: an approval path, an audit trail, and a documented answer for what happens when nobody approves. |

Most real agents use all three: narrow scopes for the routine path, a policy layer for the coarse providers where nothing narrower exists, and JIT for the two or three actions that can destroy something. The failure is picking one and applying it everywhere — a policy engine in front of scopes you could simply have not requested is expensive theatre, and JIT on a read that runs 400 times a day is an outage waiting for a quiet weekend.

## Checklist

Run this against the pull request, not against the running system. Everything down to the last item is checkable from the diff plus the provider's scope reference.

- [ ] Every scope in the diff names a specific API call the agent makes, in a comment or a manifest entry.
- [ ] For each scope, the reviewer can state what it permits *beyond* that call, from the provider's own scope page.
- [ ] The narrower scope that was rejected is named, with the reason it was rejected.
- [ ] No scope string in the diff is undocumented on the provider's scope reference.
- [ ] The tool registry diff was reviewed as a permission diff. Every added tool has a workflow that calls it.
- [ ] No tool exposes a caller-supplied string that reaches a broader API call than the tool's name implies.
- [ ] Resource-level constraints are stated where the provider supports them: selected repositories, channel membership, page-level connection, `Resource` and `Condition` in an IAM policy.
- [ ] Any resource constraint enforced in the agent's own process is labelled as such, and is not being counted as a security boundary.
- [ ] Destructive capabilities — permanent deletion, DELETE endpoints, admin scopes — are either absent or behind an elevation path with an expiry.
- [ ] Credential lifetime is stated, and it is the shortest the provider allows for this access pattern.
- [ ] The grant is a new credential rather than an edit to an existing one, so revocation does not affect anything else.
- [ ] Scopes removed in this change are recorded as retired with a reason, not deleted from the file.
- [ ] The last unexercised-scope report is attached or linked, and no scope on it at zero calls is being retained without a written reason.
And one thing the diff cannot tell you, which is worth asking anyway before the grant ships:

- [ ] You can answer, from logs, which agent used which scope against which resource, and on whose behalf. If not, see the [governance pillar](/govern/) for the attribution side of this.

## Failure modes

### The scope nobody can explain, found during an incident

**Symptom:** an incident review asks what the agent could have reached, and the answer takes three days and a code archaeology exercise. The grant includes a scope no current team member recognises.

**Cause:** the scope was added to make a 403 go away and never had a written reason. Every subsequent reviewer inherited it as the status quo, and the bar for removing an existing grant is higher than the bar for adding one.

**Fix:** the manifest with a mandatory justification field, enforced in CI. The value is not the file; it is that an unjustified widening becomes a failing build instead of a review comment somebody defers. Retroactively, run step 3 against a copy of the agent with the unexplained scope removed and see what breaks — that is the only reliable way to recover the reason, and it is much cheaper before the incident than during it.

### Narrowing broke production at 3am

**Symptom:** somebody finally removed the over-scoped grant, and a path nobody knew about failed overnight, in a system that had worked for a year.

**Cause:** the narrowing was done by deduction rather than by evidence. Reading the code finds the paths you can see; it does not find the retry branch, the error handler, or the quarterly report job.

**Fix:** never narrow blind. Run step 3 on a fresh credential in a shadow environment against real traffic shape first, and only then swap. If a shadow run is impossible, narrow one scope at a time, ship each on its own, and give it a full business cycle. And take the lesson forward: this is exactly the cost that makes narrow-first the only ordering that works, because none of this machinery is needed if the scope was never granted.

### The permission that arrived as a tool

**Symptom:** the scope list has not changed in months and the security review is clean, but the agent can now delete records. The audit log shows a delete call nobody authorised.

**Cause:** a tool was added to the registry under a credential that already had the scope. The scope diff was empty because the scope was already there. Reviewers watched the wrong file.

**Fix:** treat the tool registry as a permission surface with the same review requirements as the scope manifest. Enumerate `tools/list` in CI and diff it against a checked-in expected list, so an added tool fails the build. Do not rely on annotations to classify the new tool for you: the MCP schema defaults an unannotated tool to destructive, and the specification requires clients to treat annotations from untrusted servers as untrusted.

### The narrow-looking scope that is not narrow

**Symptom:** the grant reads `read` or "Read content" and the review passes, then an audit finds the agent could read the entire workspace including material it has no business seeing.

**Cause:** the scope's name described the verb, and the reviewer read the verb as the whole grant. Linear's `read` is account-wide and "will always be present". Notion's "Read content" spans everything the connection has been added to. Neither is narrowed by being called read.

**Fix:** require the second column. A scope entry that does not state what the scope grants beyond the intended call is incomplete, and for coarse providers that column is where the resource-level constraint has to appear — which pages the connection is added to, which channels the bot is in, which repositories the token selected.

### The exercised scope that only produced denials

**Symptom:** the 30-day report shows every scope exercised, so nothing gets removed. Meanwhile the agent's error rate is unchanged.

**Cause:** the usage signal counts attempts, not successes. AWS states this outright for last accessed data — it "includes all attempts to access an AWS API, not just the successful attempts" — and any log built from request records rather than responses has the same property. A scope can look busy entirely on the strength of calls that were denied, or of a retry loop hammering a call that never worked.

**Fix:** record the outcome alongside the call and count successful calls only. Where the provider gives you attempts and nothing else, say so in the report rather than letting the number read as usage. A metric that quietly overstates usage is worse than no metric, because it retires the question.

### Just-in-time elevation that nobody approves

**Symptom:** the elevation path works in the demo and is dead within a quarter. Either the approval queue is ignored and work stalls, or somebody grants a standing exception and the agent is back to holding the credential permanently.

**Cause:** JIT was designed around the security property and not around the operational one. The approver was never named, the response time was never agreed, and the out-of-hours case was never answered.

**Fix:** before adopting elevation, write down the approver, the target response time, and the documented behaviour when the timeout expires. If the honest answer is that nobody is available at 3am, do not put JIT on the 3am path — put a narrower always-on scope there and reserve elevation for actions a human should be in the loop on anyway. And treat a standing exception as an incident: it is the control failing open, not the control working.

## Doing this at scale

The method above is a day's work for one agent. The thing it does not survive is multiplication. Ten agents across six providers is sixty grants, each with its own scope vocabulary, its own revocation path, and its own answer to "was this used". The manifest still works, but two properties start to matter more than the manifest does.

The first is **policy evaluated per request rather than per grant**. A scope is decided once, at consent time, by whoever was integrating that afternoon, and it then applies uniformly to every call the agent makes forever. That is a poor fit for how agents actually behave, because the same tool is appropriate in one context and not in another — reading a support message is fine; reading the CEO's thread through the same `gmail.readonly` grant is not, and the scope cannot tell the difference. Per-request evaluation moves the decision to the moment of the call, where the resource, the acting user, and the task are all known. AWS session policies are the shape of this in one vendor's terms: the effective permission is the intersection of a standing role and a policy computed for this session. Generalising that across providers is what a control layer is for.

The second is **evidence of which scopes were exercised**, gathered in one place. The 30-day report is straightforward when one provider is Google and you can query the Reports API. It becomes a data-integration project the moment you have five providers, three of which log nothing you can query. The only place with a complete view is whatever every call passes through — which is an argument for having such a place before you need the report.

That is the shape of [Agentic Fabriq](https://agenticfabriq.com): agents route through a control layer instead of holding provider credentials, policy is evaluated per request rather than baked into a grant, and every call is attributable to an agent and the user it acted for. For this guide's purposes the useful consequence is that step 1 stops being a code review. Ask the gateway what the agent can call, and diff the answer against the manifest:

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

APPROVED_TOOLS = {
    "list_unread_support_mail",
    "read_message",
    "find_requester",
    "create_ticket",
    "post_triage_summary",
}


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        tools = await af.list_tools()
        agents = await af.list_agents()
        names = {tool.get("name") for tool in tools if isinstance(tool, dict)}

        print(f"agents registered: {len(agents)}")
        # A tool dict with no "name" key puts None in the set, and sorted()
        # raises TypeError comparing None to str. Drop the falsy entries first.
        for name in sorted(n for n in names - APPROVED_TOOLS if n):
            print(f"UNDECLARED  {name}  -- callable but not in the manifest")

        result = await af.invoke_connection(
            "support_inbox",
            method="get_emails",
            parameters={"q": "label:support is:unread", "max_results": 10},
        )
        for email in result.get("emails", []):
            print(email)


asyncio.run(main())
```

The runnable version is `examples/govern-least-privilege/gateway_scope_audit.py`. Connection names, method names, and response shapes are per-deployment, so run `afctl tools list` against your own gateway rather than copying `support_inbox`, `get_emails`, or the `emails` key on faith.

Two properties are worth separating from the code. The provider credential stays in the control layer, so the blast radius of a compromised agent process is a revocable gateway token rather than a standing `gmail.readonly` grant on a real mailbox — which is a different kind of narrowing than scope design, and complementary to it. And because every call passes one point, the exercised-scope report is a query rather than an integration, across every provider at once including the ones that publish nothing.

None of that removes the work in steps 1 through 5. A gateway that faithfully proxies an over-scoped grant is an over-scoped grant with better logs. What a layer like [Agentic Fabriq](https://agenticfabriq.com) changes is the cost of running the method repeatedly across a fleet, and everything above stays correct if you would rather own that machinery yourself. Doing it yourself is genuinely viable at small scale — the manifest, the 30-day report, and a habit of reviewing tool registries will get a handful of agents to a defensible place with no new infrastructure.

## Further reading

The [governance pillar](/govern/) covers the two problems adjacent to this one: OAuth flows, which decide how a grant is obtained and revoked, and audit, which decides whether you can answer what the agent did with it. Scope design without revocation is a grant you cannot take back, and without audit it is a claim you cannot verify. For a worked single-provider version of steps 1 and 2, [connecting an agent to Gmail](/connect/gmail/) runs the same reasoning against one scope taxonomy in detail, and the [failure pillar](/fail/) has what the over-scoped version looks like after it goes wrong.

Primary sources for the literals above:

- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) — the exact Gmail scope strings, their descriptions, and their non-sensitive, sensitive, and restricted classifications.
- [Method: users.messages.modify](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify) — the three scopes that permit applying a label to a message.
- [Handle Gmail API errors](https://developers.google.com/workspace/gmail/api/guides/handle-errors) — what a 403 actually covers, which is why you disable retry on it while running step 3.
- [Using OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server) — incremental authorization via `include_granted_scopes`, and the requirement to verify which scopes were granted.
- [OAuth Token Audit Activity Events](https://developers.google.com/workspace/admin/reports/v1/appendix/activity/token) — the `activity`, `authorize`, `deny`, `request`, and `revoke` events and the `api_name`, `method_name`, `client_id`, `app_name`, and `scope` parameters.
- [Zendesk OAuth Tokens reference](https://developer.zendesk.com/api-reference/ticketing/oauth/oauth_tokens/) — the `resource:access` scope shape, what read and write map to in HTTP verbs, the "access to all resources is assumed" default, and the access and refresh token lifetimes with their configurable ranges.
- [Linear OAuth 2.0 authentication](https://linear.app/developers/oauth-2-0-authentication) — the `read`, `write`, `issues:create`, `comments:create`, and `admin` scopes, the note that `read` is always present, the comma-separated `scope` parameter format, and the targeted-scope advice for comments.
- [Slack OAuth scopes reference](https://docs.slack.dev/reference/scopes) — `chat:write`, `chat:write.public`, and the channel-membership wording in `channels:history`.
- [Notion integration capabilities](https://developers.notion.com/reference/capabilities) — the content, comment, and user capability set, the Insert-versus-Update split and what each one cannot do, and how page-level access is granted.
- [Scopes for OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) — the classic `repo` scope and what "full access" covers.
- [Permissions required for fine-grained personal access tokens](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) — the per-permission endpoint mapping for fine-grained tokens.
- [GitHub Apps REST API](https://docs.github.com/en/rest/apps/apps) — installation access tokens, their one-hour expiry, and the `repositories` and `permissions` down-scoping parameters.
- [AWS STS AssumeRole](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html) — `DurationSeconds` bounds and the intersection semantics of session policies.
- [Refine permissions in AWS using last accessed information](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_last-accessed.html) — tracking periods, the attempts-not-successes caveat, and the management-events limitation.
- [Using AWS IAM Access Analyzer](https://docs.aws.amazon.com/IAM/latest/UserGuide/what-is-access-analyzer.html) — unused access analyzers and policy generation from CloudTrail activity.
- [What is Privileged Identity Management?](https://learn.microsoft.com/en-us/entra/id-governance/privileged-identity-management/pim-configure) — eligible versus active assignments, activation, and time-bound access.
- [MCP security best practices](https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices) — the scope minimization section, including scope inflation blindness and progressive elevation.
- [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) — `tools/list`, the `annotations` field, and the requirement to treat annotations as untrusted.
- [MCP schema, ToolAnnotations](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/schema/2025-06-18/schema.ts) — the `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` fields and their defaults.
- [RFC 9396, OAuth 2.0 Rich Authorization Requests](https://www.rfc-editor.org/rfc/rfc9396.html) — the `authorization_details` parameter, for when scope strings cannot express the grant you need.
