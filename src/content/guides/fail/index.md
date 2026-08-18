---
title: How Agent Integrations Fail in Production
description: "The six classes of agent failure: permissions, credentials, control flow, trust boundaries, attribution, lifecycle. The symptom, the detection, the fix."
pillar: fail
updated: "2026-08-18"
cornerstone: true
keywords:
  - ai agent failures
  - agent production incidents
  - agent security failure modes
  - agent incident response
  - prompt injection privileged tool
  - agent blast radius
relatedGuides:
  - fail/over-scoped-oauth
  - fail/token-leakage
  - govern/audit
howTo:
  - name: Instrument the six things that make an incident scopeable
    text: Emit one record per attempted tool call at the policy decision point, carrying both principals, the decision, the outcome, a taint flag, and a correlation id shared by every call in the task.
  - name: Notice it, from a per-run aggregate rather than a rate
    text: Alert on tool calls per run against a measured p99 and on successful writes taken while untrusted content was in the context, because neither shows up in a per-second graph.
  - name: Stop the agent before anything else
    text: Flip a kill switch keyed by agent and by user, read outside the deploy artifact, so the window you are about to measure stops growing while you measure it.
  - name: Revoke the credential, and do not rotate yet
    text: Post the token to the provider's revocation endpoint so the leaked value stops authenticating, because issuing a replacement does nothing to the old one.
  - name: Scope the reach from the audit log
    text: Query everything that agent did for that principal between exposure and revocation, and refuse to publish a number the records cannot support.
  - name: Rotate, then make the three structural changes
    text: Issue the replacement credential once you know what it is replacing, then narrow the grant, put the second principal in the request, and emit the record at the decision point.
faq:
  - question: How do AI agents actually fail in production?
    answer: In six recurring classes - over-broad permissions, leaked or unrotated credentials, runaway control flow, untrusted content reaching a privileged tool, actions nobody can attribute to a human, and agents that outlive their purpose or their owner. All six are the same underlying mismatch, which is a system designed for human users or for fixed services meeting a principal that is neither.
  - question: Why do over-permissioned AI agents not show up in monitoring?
    answer: Because they do not degrade anything. There is no latency cost, no error rate, and no alert; an over-scoped agent succeeds at more than it was asked to do, and success and catastrophe emit the same 200. Detection has to be deliberate rather than reactive - a scheduled diff of granted against exercised permissions, and a per-run aggregate compared against a measured baseline.
  - question: In what order should you respond to an AI agent incident?
    answer: Stop the agent, revoke the credential, scope the reach from the audit log, then rotate. Stopping first is what stops the window growing while you measure it. Revoking before rotating matters because issuing a replacement credential does not invalidate the old one, so a credential that was only rotated keeps working. If the integration must keep serving, rotate immediately after revoking rather than before.
  - question: Is prompt injection the main way AI agents fail?
    answer: It is one class of six, and it is the one that supplies capability rather than reach. An injection against a narrowly scoped agent reaches what the agent could already reach; the same injection against a broadly scoped one reaches everything the grant covers. Spend on the deterministic control first, because a call the credential cannot make does not happen regardless of what the model was persuaded to attempt.
  - question: What should you instrument on day one for an AI agent?
    answer: A correlation id per task propagated to every tool call; one record per attempted call written at the policy decision point with both principals, the decision, and the outcome; a taint flag saying whether untrusted content was in the context; per-run counters for calls, tokens, spend, and wall clock; the granted permission set read from the provider rather than from source; and a kill switch read before every call.
  - question: Can one control fix more than one class of agent failure?
    answer: Yes, but not all six. Routing every tool call through one point collapses four things into single operations - credential sprawl, attribution, fleet-wide quotas and stop switches, and the inventory of what is registered and what it can reach. It does not narrow a grant that still exists, it does not stop untrusted content from being read as instructions, and it does not fix a planner with no terminating condition.
---

## TL;DR

- Agents fail where a system designed for human users, or for fixed services, meets a principal that is neither. That single mismatch is what all six classes below are made of.
- The six worth naming separately, because they detect and prevent differently: permission failures, credential failures, control-flow failures, trust-boundary failures, attribution failures, and lifecycle failures.
- They are quiet. The correct call and the catastrophic one both return `200`, so no latency graph, error-rate alert, or exception tracker you already run will find them. Control flow is the partial exception, and even there what changes is a graph's shape rather than its level. Detection has to be deliberate looking, on a schedule, at something that is not broken.
- Deterministic controls beat probabilistic ones, and the ordering is not a preference. A call the credential cannot make does not happen no matter what the model was persuaded to attempt; a filter that tries to recognise a malicious instruction is a probability.
- When it happens: stop the agent, revoke the credential, scope the reach from the audit log, then rotate. Rotation never precedes revocation, because a credential that was rotated and not revoked keeps working.

## Who this is for

You have agents in production, or you are about to, and you want the map before you need it: what breaks, what it looks like on the day, and which control actually closes each one. This is the front door to the failure pillar — every class here has a guide behind it, and every claim about a specific scope string, error code, or measured number comes from one of them.

Skip it if you are choosing an integration's permissions for the first time. That is a cheaper problem solved before the grant exists, and the [connect pillar](/connect/) works it per provider. Come back when something is live, because from that point every one of these gets more expensive to fix than it was to avoid.

## The problem

A meeting-brief agent reads the agenda document for your next meeting and writes a summary into a channel. It has held `https://www.googleapis.com/auth/drive.readonly` since a 403 in the week before launch, because the file-picker integration was a week of work and a filename search was an afternoon. On a Monday it reads an agenda document that somebody outside your company shared into a folder, and that document contains a paragraph addressed to the assistant. The agent does what the paragraph says. It searches Drive, finds what it was told to look for, and posts a summary into a channel it has been able to write to since day one.

Nothing threw. The Drive calls were well-formed and inside quota. The channel write returned `200`. Every span is green, the run finished faster than average, and the user got a meeting brief. You find out eleven days later, and the first question is the one you cannot answer: what did it reach?

That incident is not one failure. It is three of the six below stacked on each other — a permission failure that decided the reach, a trust-boundary failure that supplied the instruction, and an attribution failure that is why you are reading a log instead of an answer — and each of the three was individually invisible.

### The one mismatch underneath all of them

Software has had two kinds of principal for thirty years, and almost every access control, audit format, and operational safeguard you will reach for was calibrated against one of them.

**The human model** assumes a person decides, at consent time, about their own data; that the decision is durable because their job is durable; that they act at the speed of hands and attention; that they are enrolled by a hiring process and removed by an offboarding one; that naming them in a log is the whole of attribution; and — the load-bearing one — that when instructions arrive inside content, a person's judgement is the boundary that separates the two.

**The service model** assumes the opposite of most of that. The call graph is fixed at deploy time and was reviewed as code. Permissions are chosen once and exercised deterministically, so a scope granted is a scope used. The credential is a process-level constant that no code path reads back out to a caller. Behaviour changes only through a deploy, which means a deploy is also the stop button. There is no person behind it, so attributing an action to the service is a complete answer. And it parses data with a parser that cannot be talked into executing it.

An agent borrows the grant machinery of the first and the operational machinery of the second, and violates a load-bearing assumption of each.

It holds a human's delegated authority, so the OAuth consent screen, the scope string, and the access review all apply to it — but there is no human in the loop at call time to exercise the judgement those controls assume, no manager who notices it behaving oddly, and no offboarding process that reaches it. It runs at service speed and service volume, so the quotas, retries, and rate limits all apply to it — but its call graph is not fixed at deploy time. It is decided per run by a model conditioned on text the agent did not write and you did not review. Its credential sits in a process that has a component whose entire function is to reproduce its inputs. And it has two principals, always: the software that acted and the human whose authority it borrowed, where every log format ever written has one field.

This is worth more than a framing device, because it tells you which controls to distrust. Any control that assumes a human will notice — a consent screen listing capabilities, a quarterly access review, an attestation someone clicks through — fails on the human side of the mismatch, and it fails silently, because the human is not lying. Any control that assumes fixed behaviour — a code-reviewed call graph, a permission chosen once at integration time, a deploy as the way to stop something — fails on the service side. What survives is the class of control that binds *per request*: a policy evaluated with the resource and the caller in hand, a credential issued for one subject and one resource, a budget checked before each attempt, a record written at the decision point. Per request is the only granularity at which an agent is a knowable thing, because it is the only granularity at which its behaviour has stopped moving.

### Why these failures are quiet

An over-scoped agent works perfectly until it does not, and the reason is mechanical rather than unlucky. Broad permissions do not degrade anything: no latency cost, no quota cost, no error rate, no alert. The [over-scoped OAuth guide](/fail/over-scoped-oauth/) puts the consequence exactly — over-scoped integrations "do not throw errors; they succeed at more than they were asked to do".

Generalise that and you have the property that makes this whole pillar necessary. Across these classes the healthy state and the catastrophic state produce identical telemetry. A `files.list` inside the job description and a `files.list` walking a corpus are the same request with a different argument. One refund and the same refund committed six times are all `200 OK`. A record naming the right principal and a record naming the wrong one are both a record. Every instrument an engineering organisation uses to notice a mistake reads the difference between working and broken, and none of these are broken.

Worse, several of them are *absences*. There is no positive artefact to alert on when a denial was never recorded, when a scope was granted and never exercised, when a grant was never revoked, when an owner left and nothing pointed at the agent they owned. You cannot page on a row that does not exist. This is why every detection below is a scheduled comparison — granted against exercised, this run against a measured baseline, stored delegations against the directory, registered tools against a declared list. Something has to go looking, on a cadence, at a system that is not complaining.

Control-flow failure is the partial exception, and only partial. It does eventually change a graph — call volume, throttling rate, or spend — but it changes the graph's *shape* rather than its level, which is not what anyone's alert is watching. Miss the shape and the next signal is an invoice for a month that has already happened, or an email from a partner who noticed before you did.

### Permission failures

**Symptom, as it presents.** Administrative rather than operational. Someone asks what the agent can reach and the honest answer is a capability rather than a count — "it has `repo`", "it has full mailbox access". A consent screen that lists more than the product does. An incident whose blast radius nobody can bound, which is operationally identical to assuming everything.

**Root cause.** Almost nobody chooses a broad scope. A narrow one returned a 403 late in a sprint, the broad one worked, and the pull request that widened it changed one string. Then it is never narrowed, because narrowing is a rollout rather than a diff: on most providers a granted scope cannot be subtracted, so it means revoking and re-consenting every user, which needs an owner, a support plan, and a date.

**Detect before it hurts.** Diff granted against exercised. Get the granted set from the provider's grant listing rather than from your source tree — they diverge, and the divergence is the finding. Get the exercised set from an audit log of calls, map each method to the narrowest scope that would have satisfied it, and subtract. Then alert on the *delta* at grant time rather than on the existence of a broad scope, because the delta is new information and a permanent red light is one people learn to ignore.

**Structural prevention.** Choose narrow first, when the cost of being wrong is a stack trace during development rather than a re-consent rollout. Keep the requested set in one reviewed file and fail the build when it grows. Where the provider's granularity bottoms out above what you need, add the resource constraint and the time bound, and enforce them somewhere the agent cannot edit.

*The mismatch:* a scope is a decision made once, by a human, about a kind of thing. An agent's appropriate reach is decided per call, by context — which resource, for which user, in which task — and that is exactly what a scope string has no way to say.

### Credential failures

**Symptom.** A credential somewhere that is not a vault: a span attribute, a log line, a support ticket, a model response, a prompt a vendor retained. By the time you see it, it has been valid for as long as it has been visible. The quieter version has no symptom at all — a secret that has never rotated, held by every replica, reaching everything every user may reach.

**Root cause.** Two, and they compound. The first is that a process has one environment, so a process-wide credential must carry the union of what every user may reach; authorization then moves out of the credential and into your code, where an agent generates a large share of the queries. The second is that anything inside the model's context is inside the set of strings the model may emit — and a credential reaches the context by more paths than anyone tracks: a system prompt, a tool description, an echoed provider error, an inherited subprocess environment, an observability span with content capture left on from a debugging session eight months ago.

**Detect before it hurts.** Exact-value scanning over outbound payloads, fed by whatever issues the credentials, so it cannot miss a format it has never seen. Pattern scanning as the second net, knowing it finds only what it has been taught. A canary credential planted where a leak would reach it, understanding that it fires on use, which is late. And a startup check that enumerates every log handler and fails the boot if one is unguarded, because the one you did not install is where the leak leaves.

**Structural prevention.** Remove the secret rather than guarding it. The agent names a connection; something else holds the credential and makes the call; the model sees a name it can leak all day at no cost. Then issue per request, scoped to one subject and one resource, with a lifetime short enough that expiry bounds a leak you never detect — which is the only property that does not depend on your detection speed.

*The mismatch:* the service model treats a credential as a process constant that inputs never touch and outputs never carry. An agent process has a component whose function is to reproduce its inputs, and per-user entitlement is a per-request variable. A per-process constant cannot express it.

### Control-flow failures

**Symptom.** A graph changing shape. A staircase in call volume is retries multiplying across layers; a clean ramp with no errors at all is a loop with no terminating condition; a flat plateau at a constant rate is pagination that never ends. Or no graph at all: a write retried without a stable idempotency key commits twice, no threshold is crossed, the run reports success, and somebody reconciling a ledger finds it a month later.

**Root cause.** In ordinary software the call count is decided at deploy time. A hand-written integration that fetches 25 records fetches 25 records on every run. An agent decides how many times to look, and that decision is a function of a prompt, a tool description, and whatever the last call returned — so it can be influenced, including by the content the agent is reading. Underneath that sit two amplifiers that are not agent-specific but are worse here: retries compose by multiplication across layers, and a per-process budget silently multiplies by the replica count.

**Detect before it hurts.** Per-run aggregates, not per-second rates. Ten times the tool calls at the same requests-per-second, spread over ten times the duration, is identical on a rate graph. Alert on tool calls per run, tokens per run, and cost per run against a measured p99, add a cumulative daily spend alert per user below the hard cap, and emit which limit terminated each run so a rising count of one of them is a bug report rather than a surprise.

**Structural prevention.** A hard run budget over four quantities at once — provider calls, tokens, money, wall clock — checked before every attempt, with retries counted against the same budget. One retry layer only. Backoff with full jitter, treating any server-supplied delay as a floor with randomness above it. A stable idempotency key derived from the operation rather than the attempt. A circuit breaker per dependency, shared across the pool. And a kill switch that stops one agent for one user without a deploy.

*The mismatch:* every quota, retry policy, and capacity plan you own assumes the call graph was fixed when the code was reviewed. Here it is generated at runtime by something that can be argued with.

### Trust-boundary failures

**Symptom.** The hardest one to see, because it presents as correct operation. A well-formed action with plausible provenance: a search that ran, a message that sent, a ticket that got created. Where it does surface, it surfaces sideways — a request to a domain nobody recognises in the egress log, originating from a rendering surface rather than from the agent's own HTTP client, or a user reporting an answer drawn from a document they cannot open in a browser.

**Root cause.** Two shapes of the same confusion. The first is indirect prompt injection: [OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) defines it as an LLM accepting input from external sources "such as websites or files", where content inside that source alters the model's behaviour. Any agent that summarises a document somebody else wrote, triages inbound mail, or fetches a URL is inside that definition as its normal operating mode, not as an edge case. The second is the confused deputy: an intermediary with more authority than its caller, doing what its caller asked. An agent holding an application-level permission answers a question its user was never entitled to ask, and nothing anywhere is wrong except the number of principals in the request.

The two combine into one product rather than two risks. [OWASP LLM06](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/) gives the root causes of excessive agency as "excessive functionality; excessive permissions; excessive autonomy", and the [lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) names the combination that makes exfiltration easy: access to private data, exposure to untrusted content, and the ability to communicate externally. The injection is the capability. The grant is the reach. Held by one process, they multiply.

**Detect before it hurts.** Record a taint flag on every action saying whether untrusted content was in the context, default it to true, and run two queries: denials where it is true, which are your controls catching what the content asked for, and successful *writes* where it is true, which is the list a human has to read. Alongside that, an egress record of the destination host of every outbound request the agent caused, so a domain nobody recognises is a query rather than an archaeology exercise.

**Structural prevention.** Accept that the probabilistic half stays probabilistic — there is no known fool-proof prevention for prompt injection — and spend on the deterministic half. Narrow the grant, so a successful injection reaches less. Put the second principal in the request, so the agent's effective permissions are the intersection of what it may do and what its caller may do, never the union. Keep the destination list for anything that leaves outside the agent's own process, and do not auto-fetch URLs the model constructed from untrusted input.

*The mismatch:* the human model assumes judgement separates instructions from data, and the service model assumes a parser does. A language model has one channel, and everything on it is eligible to be either.

### Attribution failures

**Symptom.** An incident nobody can scope. You can list every action the agent took and attribute not one of them to a person; or you can attribute all of them to a person and see no evidence an agent was involved. The postmortem stalls on "on whose behalf", and the answer arrives in three days as a join across four systems, or never.

There is a second symptom that reads as good news and is not: a trail with no denials in it. That is not evidence nothing was refused. It is evidence that refusals are not recorded, which means the fourth question — could we have stopped it — has no data at all.

**Root cause.** Two identities and one field. The agent authenticates as itself with one service account, and the human who triggered the run is a detail of the inbound request that was never carried into the tool call. Or the reverse: the agent impersonates the user, and downstream nothing can tell an agent was there. Both produce logs; neither produces an answer. Compounding it, the record is usually written after a successful call returns, which structurally excludes every refusal and every error from the evidence.

**Detect before it hurts.** Try the query cold, today, on a normal Tuesday: everything agent X did for principal Y between two timestamps. If it is not an index scan against a single store, you have found the gap before an incident did. Then monitor the denial count: a window with zero denials means either a well-behaved agent or a policy that is not evaluating anything, and only the count over time distinguishes them.

**Structural prevention.** [NIST SP 800-53](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final) control AU-3 sets the baseline for any audit record — what happened, when, where, its source, its outcome, and the "identity of any individuals, subjects, or objects/entities associated with the event" — and an agent record is that baseline with the second identity added. So: one record per *attempted* action, written at the policy decision point rather than after the call, carrying both principals, the resource, the action, the decision and the reason for it, the outcome, and a correlation id shared by every call in the same task. Make the delegated principal a required argument on the tool-call path, so an unattributed call is a type error rather than a null. Keep the store append-only and separate from the component being audited.

*The mismatch:* an audit format that names one principal was correct when there was one. An agent action has two, and the interesting one is not the one making the call.

### Lifecycle failures

**Symptom.** Output arriving for somebody who left the company weeks ago, usually noticed because a report has a departed employee's name on it. A tool still callable long after the workflow that needed it was cut. A grant on a client id nobody recognises, from an integration that was decommissioned. An agent running against a credential whose owner cannot be identified.

**Root cause.** Humans have joiners, movers, and leavers, and a process that reaches every system when one of those happens. Services have deploys and decommissioning, and a change to either is a change to code. Agents have neither. Deprovisioning reaches the directory and does not reach the agent, which still holds a stored delegation and, if that delegation carries a refresh token, keeps minting access tokens until something breaks. Nobody offboards an agent because nobody thinks of it as a thing that can be offboarded, and no deploy retires a grant, because the grant lives at the provider.

**Detect before it hurts.** Two independent mechanisms, because each alone fails silently. Subscribe to deprovisioning events from the identity provider and treat one as a delete of every delegation you hold for that subject. Then reconcile on a schedule — walk your stored delegations, resolve each subject against the directory, and alert on any that no longer resolve. The webhook is the fast path; the reconciliation catches the webhook you missed. Do the same for the tool registry and the agent registry: enumerate what is callable and diff it against a checked-in list of what is supposed to be.

**Structural prevention.** Give every agent a named human owner and an expiry, and make both a row that a review can fail. Delete delegations rather than marking them. Make revoked-grant errors terminal in code, so a missed deprovisioning event stops the agent instead of starting a retry loop. And keep the tool registry under the same review discipline as the permission manifest, because a tool added to a registry widens authority exactly as much as a scope does.

*The mismatch:* every offboarding process ever written assumes the thing being offboarded is either a person, who is in the directory, or a service, which is in the deploy pipeline. An agent is in neither, so it survives both.

## Step by step

One incident, end to end: the meeting-brief agent from the top of this guide, a broad Drive scope, an injected instruction inside a document it was asked to read, and a privileged tool called as a result. Detection, containment, scoping, and the three changes that stop the next one. Every mechanism below is worked in full in a sibling guide, linked in place — this is the ordering, not a second implementation.

The runnable artefact is `examples/fail-index/incident_scope.py`, which scopes one incident from an audit export and refuses when the records cannot support an answer. It touches no network.

### 1. Instrument the six things that make an incident scopeable

Not a monitoring strategy. Six concrete emissions, all cheap, all worth having before the first agent ships, each answering one of the classes above.

1. **A correlation id per user-visible task**, propagated to every tool call including across queue boundaries. Reuse the 32-hex-digit trace-id from the [W3C Trace Context](https://www.w3.org/TR/trace-context/) `traceparent` header you are already propagating rather than inventing a second one. Without it, nine records are nine unrelated rows.
2. **One record per attempted tool call, written at the policy decision point**, carrying the agent id, the delegated principal id, the resource, the action, the decision and its reason, and the outcome. Written at the decision point, not after the call, so refusals exist.
3. **A taint flag on every record** saying whether untrusted content was in the context, defaulting to true so an emitter that forgets over-reports exposure rather than quietly asserting the input was clean.
4. **Per-run counters** emitted when a run ends: provider calls, tokens, spend, wall clock, and which limit stopped it. These are the only numbers that catch a runaway, because rates do not.
5. **The granted permission set per client id**, snapshotted on a schedule from the provider rather than read from your source tree, so drift between what you request and what exists is visible.
6. **An estate row per agent**: an owner, a purpose, an expiry, and the tools it can call. This is the one that feels like paperwork until the day somebody leaves.

Add a kill-switch flag read before every tool call and cached for a few seconds, which is not instrumentation but belongs on the same day-one list, because the containment step below is unavailable without it.

### 2. Notice it

Nothing detects the injection. Be clear about that before designing anything: the model read a document and complied with a sentence in it, which is not an error condition and produces no signal of its own. What you detect is the *shape* of what followed, and there are two alerts that fire on this incident.

The first is a per-run aggregate. The run made 22 Drive calls where the measured p99 for this agent is a handful, so tool calls per run crosses its threshold. This is the alert that fires within minutes, and it is worth having precisely because it is agnostic about cause — a runaway planner, a pagination bug, and an injection all present as the same anomaly. The [runaway loops guide](/fail/runaway-loops/) has the method for deriving the threshold from a recorded baseline rather than from taste.

The second is the one that names the incident. Query for successful writes taken while the taint flag was true, and this run has exactly one: a `send` into a channel, in a task whose reads began with a document from outside the organisation. That query is a standing report, not an incident tool — you run it daily and read the list by hand, because the whole point is that no automated check can tell an appropriate write from an instructed one.

If neither alert exists yet, the realistic detection path is a person noticing something odd, eleven days later, and that is the timeline this whole section is trying to change.

### 3. Stop the agent

First action, before diagnosis. Flip the kill switch for that agent and that user pair, which is read outside the deploy artifact and takes effect within the cache TTL. Not a deploy: a deploy is a build, a rollout, and a worker restart, and the restart can re-queue exactly the work you were trying to stop.

Why first rather than second: every later step measures a window, and the window keeps growing while the agent runs. Scoping an incident whose end timestamp is still moving is not scoping. It also costs almost nothing to be wrong — a stopped agent is an outage you can end in seconds, and the three-granularity switch means you can stop one pair rather than the product.

### 4. Revoke the credential

Second, and do not skip ahead to rotation. Rotation issues a new credential; it does nothing to the old one, and most providers keep both valid by design so that rotation does not cause an outage. A key that has been rotated but not revoked keeps working until its natural expiry, which for a long-lived grant is never.

For a Google OAuth grant that means posting the token to `https://oauth2.googleapis.com/revoke`, which takes a token and not a scope list — there is no partial revoke. Know the cost before you pull it: revoking the client's grant for one user stops that user's agent, and the blunt lever at the client level stops it for everyone. Decide which you are pulling and say so in the incident channel, because somebody is about to ask why the agent stopped working for a team that was not involved.

Do this before you know the blast radius. The cost of revoking early is an outage you can fix in minutes; the cost of revoking late grows while you investigate. The [token leakage guide](/fail/token-leakage/) works this ordering in full, including the case where the credential is a pre-signed URL with no per-URL revoke button.

### 5. Scope the reach from the audit log

Now the window is closed at both ends: from the earliest moment the document could have been read to the revocation timestamp. The question is what the agent did inside it, for whom, and which of those actions were taken with untrusted content in context.

```text
== agent:meeting-brief@v3  2026-08-17T09:00:00Z .. 2026-08-17T09:45:00Z ==
  records in window ......... 24
  principals ................ user:dana@example.com
  tasks (correlation ids) ... 1
  reads ..................... 22 over 22 distinct resources
  writes .................... 2
  denied .................... 1
    slack.channel:C0GENERAL  channel_allowlist: C0GENERAL is not a declared destination
  untrusted input in context. 23 record(s)
  writes taken with untrusted input in context: 1   <- review by hand
    2026-08-17T09:03:09.000100Z  slack.channel:C0ENG  send  for user:dana@example.com
  bound: this covers calls that crossed the audited path. A credential used directly
         against the provider leaves no row here, so the provider's own log is the
         second source you still have to pull.
```

Three things in that output do the work, and one of them is a refusal.

The denial line is the most valuable row in the report, because it is the only evidence that a control was evaluated at all. Something in the injected instruction asked for a destination that was not on the declared list, and the policy said no. Without a record at the decision point that row would not exist, and the answer to "could we have stopped it" would be a shrug.

The `bound` line is the honest limit. Your own records cover calls that crossed your own path. A credential used directly against the provider — by a debugging script, a second integration on the same client id, or whoever holds a copy — leaves nothing in them. So pull the provider's log as an independent source: Google Workspace records OAuth activity under the `token` application, where the `activity` event carries `api_name` and `method_name` for every call the client made. Two properties of that log decide how you read it, both documented and both easy to trip over: events are retained for six months, and the lag is up to a few hours, so the last few hours of your window will fill in after you have written the report.

And the refusal. Run the same tool against a log that cannot support an answer and it declines to produce one:

```text
== the same window, from a log that cannot support an answer ==
  refusing to scope: 1 record(s) in the window are missing 'principal_id'
  refusing to scope: 1 record(s) claim an occurred_at later than the server's recorded_at, so the ordering is not sound
```

Both refusals are load-bearing. A record *missing* the principal field is not the same as one carrying an explicit null — null means the work was unattended and is a real answer, while an absent field means the emitter never captured a principal, and collapsing the two produces a confident count of a blast radius you cannot actually bound. The second refusal compares the emitter's timestamp against the server's: if any record claims to have happened after it was recorded, the ordering you were about to build a timeline on is not sound. Discover that here rather than in cross-examination. The [audit trails guide](/govern/audit/) has the schema, the append-only store, and the skew query these checks are built on.

### 6. Rotate, and get the agent back

Now issue the replacement. Doing it in this order means there is never a window in which both the leaked credential and the new one are live, which is also what keeps the log readable: every action after the revocation timestamp belongs to the new credential.

The exception worth naming, because a runbook that ignores it gets ignored during an incident: if the integration has to keep serving — a payment path, an on-call rota, anything with a person waiting — rotate immediately after revoking and scope afterwards. The invariant is the relative order of revoke and rotate, not the position of the scoping step. Scoping before rotating is a luxury that step 3 bought you by stopping the agent, and when you cannot stop the agent, you do not have it.

Then the copies you do not control: ask the trace vendor, the helpdesk, and the log aggregator to purge, knowing you may not get it and cannot verify it, and assume the model provider retained the prompt for its stated window. This step is the argument for everything above it, because it has no good version.

### 7. The three changes that prevent recurrence

Three, and they close different classes. A fix that closes only one of them leaves the same incident available through a different door.

**Narrow the grant.** `drive.readonly` is Google's "View and download all your Drive files", classified restricted; `drive.file` is "Create new Drive files, or modify existing files, that you open with an app or that the user shares with an app", and its reachable set is bounded by construction. The identical injection against a `drive.file` agent can instruct it to exfiltrate the document the user already handed it. Against the `drive.readonly` agent it can instruct a search of the whole corpus — [the over-scoped guide](/fail/over-scoped-oauth/) does the arithmetic, and lands at roughly 250,000 distinct documents across a 60-user pilot, 510× the corpus the agent was built to read per user. Same model, same prompt, different scope string, different incident. Narrowing is a re-consent rollout with cohorts and a force-revoke date, except on GitHub, which documents that "if you remove permissions or webhooks from your GitHub App, the changes will take effect immediately" — no user in the loop, and therefore no excuse for deferring it.

**Put the second principal in the request.** The agent's effective permissions become the intersection of what the agent may do and what the caller may do — never the union, which is a privilege escalation path in both directions. In this incident the intersection would not have stopped the Drive reads, because the user could read those documents; it would have stopped the version of this incident where the agent's own broad grant answers a question the asker was never entitled to ask, and it is the control that makes the eventual audit record mean something. [Delegated identity](/govern/delegated-identity/) has the token exchange, the intersection rule, and the revocation handling; [least privilege](/govern/least-privilege/) has the resource-level half, and its blunt note applies directly here: a constraint enforced inside the agent's own process is a boundary against a *mistaken* agent, not a compromised one. The channel allowlist that produced the denial above only counts because it is evaluated outside the process the prompt reached.

**Make the record exist.** Emit at the decision point, with both principals and the taint flag, into a store the agent cannot write over. This changes nothing about whether the incident happens and everything about what it costs: the difference between the scoping in step 5 taking an hour and taking a week, and the difference between telling a customer what was reached and telling them you cannot say.

Note what is not on the list. An instruction in the system prompt telling the model to ignore instructions in documents is not a fourth change; [OWASP's sensitive-information entry](https://genai.owasp.org/llmrisk/llm022025-sensitive-information-disclosure/) is explicit that such restrictions "may not always be honored and could be bypassed via prompt injection or other methods". Add it if you like. Do not count it.

## Decision table

Every control below buys exactly one of three things for a given class, and the columns are not interchangeable. Detection tells you it happened. Containment limits it while it is happening. Prevention makes it structurally unavailable. Most teams have bought detection for one class and believe they have prevention for all six.

| Failure class | Detect with | Contain with | Prevent with | Where to spend first |
| --- | --- | --- | --- | --- |
| Permissions | Granted-minus-exercised diff on a schedule; an alert on a new grant carrying a scope no code path uses | Policy evaluated per request outside the agent's process, refusing calls the token would permit | Narrow the grant, so the capability does not exist to be misused | Detection. You cannot argue for the narrowing until you can name the unexercised set, and the diff takes an afternoon |
| Credentials | Exact-value scanning fed by the issuer; a canary; a boot check that every log handler is guarded | Revoke on suspicion; short lifetimes, so expiry bounds a leak nobody detects | The agent holds no provider credential — it names a connection and something else holds the secret | Containment. Cut the lifetime today; the refactor is worth more but takes longer |
| Control flow | Per-run aggregates against a measured p99; cost per run; a metric naming which limit stopped each run | A run budget over calls, tokens, money, and wall clock; a circuit breaker; a kill switch with no deploy | A checkable success predicate in the planner, and a stable idempotency key on every write | Containment. The budget stops the bleeding regardless of which of the five loop shapes you have |
| Trust boundary | A taint flag, then the standing query for successful writes taken with untrusted content in context | Narrow scope and permission intersection, so a successful injection reaches less | Nothing fully. There is no known fool-proof prevention for prompt injection | Containment, and be honest that it is containment. Spend nothing on detecting the injection itself |
| Attribution | Try the two-principal query cold and see whether it answers; monitor the denial count for zero | Nothing. Attribution is not a runtime control; it is what makes the other five investigable | One record per attempted action at the decision point, both principals, append-only | Prevention. This is the only row where the cheap option and the correct option are the same |
| Lifecycle | Reconcile stored delegations against the directory; diff the tool registry against a declared list | Terminal handling of revoked-grant errors, so a missed deprovisioning stops the agent | An owner and an expiry per agent, enforced as a row a review can fail | Detection. Almost every estate has an orphan already, and finding it is a query |

Three things to read out of that table. The same control appears in different columns on different rows, and that is the useful part rather than an untidiness: narrowing a scope is *prevention* for a permission failure and only *containment* for a trust-boundary one, because it shrinks what a successful injection reaches without making the injection any less likely. The trust-boundary row has no prevention cell that is honest, and every other row's prevention is what shrinks its blast radius — which is the argument for spending on permissions and credentials even if injection is the thing you are actually afraid of. And the attribution row is the only one where the control is not defensive at all: it buys you nothing on the day except the ability to answer, which is why it is always the one deferred and always the one missing during the incident.

## Checklist

Run this before an agent reaches production, against the pull request and the deployment together. It is ordered by the six classes.

- [ ] **Permissions** — every scope, permission, and tool the agent holds traces to a specific call it makes, in a manifest that is diffed in review.
- [ ] **Permissions** — for each grant, the reviewer can state what it permits *beyond* the intended call, from the provider's own reference.
- [ ] **Permissions** — the blast radius is recorded as a count of reachable resources, not as a scope string.
- [ ] **Permissions** — the tool registry was reviewed as a permission surface, and every added tool has a workflow that calls it.
- [ ] **Permissions** — any resource constraint enforced inside the agent's own process is labelled as such and is not counted as a security boundary.
- [ ] **Credentials** — no credential, connection string, or signed URL appears in any system prompt, tool description, or tool result that becomes context.
- [ ] **Credentials** — every tool wrapper returns an error message you built, never the stringified exception or a raw response body.
- [ ] **Credentials** — the credential's lifetime is a number you chose and can state, and it is shorter than your realistic detection-to-response time.
- [ ] **Credentials** — subprocesses and third-party tool servers are launched with an explicit environment containing only what they need.
- [ ] **Credentials** — you know whether trace content capture is on in each environment, and where those spans are stored and for how long.
- [ ] **Control flow** — each run has a hard limit on provider calls, tokens, spend, and wall clock, and every retry counts against all four.
- [ ] **Control flow** — exactly one layer in the call path retries, and you can name it.
- [ ] **Control flow** — every write carries an idempotency key derived from the operation and its parameters, never generated inside the retry loop.
- [ ] **Control flow** — a kill switch stops one agent for one user without a deploy, and it has been tested in production this quarter.
- [ ] **Trust boundary** — the agent's exposure to untrusted content is written down explicitly, and its scope was reviewed against that fact specifically.
- [ ] **Trust boundary** — destinations for anything that leaves are declared outside the agent's process, and URLs the model constructed are not auto-fetched.
- [ ] **Trust boundary** — the standing report of successful writes taken with untrusted content in context exists and has a named reader.
- [ ] **Attribution** — one record per *attempted* action, written at the policy decision point, so denials appear; verified by triggering a denial and finding the row.
- [ ] **Attribution** — every record names the agent and the human, keyed on an immutable subject id, with a correlation id shared across the task.
- [ ] **Attribution** — the audit store's writer role cannot update, delete, or truncate it, and this was tested by trying.
- [ ] **Lifecycle** — the agent has a named human owner and an expiry date, both recorded somewhere a review can fail.
- [ ] **Lifecycle** — offboarding deletes stored delegations, and a scheduled job reconciles them against the directory so an orphan cannot survive a missed webhook.
- [ ] **Lifecycle** — revoked-grant errors are terminal in code rather than retried.
- [ ] **Response** — the runbook says stop, revoke, scope, rotate, in those words, and names who can revoke each credential type out of hours.

## Failure modes

These are not the six classes again. They are the ways the *response* to them fails — the recurring shapes of a team that has read all of the above and is still going to have the incident.

### The taxonomy became a wiki page

**Symptom:** six months after the review, a page exists listing six classes of agent failure, and the incident that happens is one of the six, handled exactly as it would have been beforehand.

**Cause:** the classes were adopted as vocabulary rather than as work. Each one needs a different control, a different owner, and a different detection cadence, and collapsing them into a single ticket labelled `security` produces a ticket with no home. This is the same ownership gap that leaves scopes un-narrowed for years: the person who can see the problem is not the person who would carry the fix.

**Fix:** one named owner and one metric per class. Unexercised scopes, credentials older than their stated lifetime, runs stopped by a budget, writes taken with untrusted input, records missing a principal, agents with no live owner. Six numbers on one dashboard that somebody reports on. A number competes with a roadmap; an adjective does not.

### The control lived in the same process as the prompt

**Symptom:** a review passes. The agent has an endpoint allowlist, a destination filter, and a check that refuses to read outside a working set. An injection reaches the model and the agent does something outside all three.

**Cause:** the constraint is enforced by code the compromised component is running. A prompt that persuades the model to call a different tool walks past an allowlist living in the same process, because nothing about that allowlist is privileged relative to the model's choices.

**Fix:** anything that has to hold under attack lives where the agent cannot edit it — a gateway, a proxy, or the provider's own policy engine. Keep the in-process check anyway, and label it in the design document as a guard against a mistaken agent rather than a compromised one, so the next reviewer does not count it twice.

### The response ran in the wrong order

**Symptom:** the incident is declared closed and the audit log shows calls authenticated by the old credential. Or the reverse: a careful scoping exercise whose window kept extending, because nobody stopped the agent first.

**Cause:** pressure during an incident runs toward restoring service, and rotation is what restores service. So rotation happens first, and rotation does not invalidate anything. The mirror-image version is a team that starts investigating immediately, because investigating feels like progress, while the thing being investigated continues.

**Fix:** write the ordering into the runbook in those words — stop, revoke, scope, rotate — and name, per credential type, who can execute each step out of hours. Then verify a revocation the only way that counts: make a call with the old value and confirm it fails.

### The dashboard was green throughout

**Symptom:** the runaway, the injection, and the duplicate writes all happened while every graph looked normal, and the first signal was an invoice, a partner email, or a customer.

**Cause:** every alert is on a rate, and none of these change a rate. Ten times the tool calls spread over ten times the duration is identical on a per-second graph, and a write committed six times is six successes.

**Fix:** alert on per-run aggregates with thresholds traced to a recorded baseline, and add the two queries that are not thresholds at all: successful writes taken with untrusted content in context, and the denial count over time. Neither is a threshold because neither has a safe number — they are lists a person reads.

### The trail was complete and could not answer the question

**Symptom:** every action the agent took is in the log, with timestamps, arguments, and status codes, and the postmortem still takes a week.

**Cause:** the log answers "what happened" and the question is "on whose authority, and could we have stopped it". Those need fields that were never captured: the delegated principal, the grant that made the action permissible, the policy decision and its reason. And because the write happens after a successful call, the refusals — the only evidence the controls did anything — were never recorded at all.

**Fix:** grade the log you have against the four clauses before you add a field to it, and cut anything that does not serve one. Then move the write to wrap the policy evaluation, and check the fix by triggering a real denial in staging and querying for the row.

### The estate had no owners

**Symptom:** an inventory exercise finds agents nobody claims, tools nobody calls, and grants belonging to integrations that were decommissioned, and no single person can say which of them are safe to remove.

**Cause:** agents are created by whoever needed one, on the day they needed it, and nothing in the organisation is responsible for the set of them. There is no joiner-mover-leaver process for software that acts on a person's behalf, so the population only grows.

**Fix:** an owner row per agent, with an expiry, reconciled against the directory on a schedule. Treat an agent with no live owner as an incident rather than as cleanup, because it is a live credential holding delegated authority with nobody accountable for what it does with it.

## Doing this at scale

Everything above is one agent. The honest version of the multiplication is that some of these classes collapse into a single control point and some do not, and a plan that assumes all six collapse will be wrong about the two that matter most on the day.

**Four collapse well.** Credential failures collapse hardest: instead of every agent, environment, and replica holding a provider credential, one place holds them and everything else holds a token addressed to that place — so revocation is one operation rather than a search, and rotation stops touching agent deployments. Attribution collapses because a record emitted by the layer the calls route through is a by-product of the call rather than something each emitter remembered to write, and because the emitter is then not the audited component. The fleet arithmetic of control-flow failures collapses: one quota counter instead of forty, one stop flag instead of a deploy to every service that might be involved. And the inventory half of lifecycle failures collapses, because "what is registered and what can it reach" becomes a query instead of a wiki page.

**Two do not, and saying so is the point.** The first is the grant itself, which is untouched. A policy layer that refuses calls the token would permit reduces the reachable set in practice, which is real risk reduction and much faster to ship than a re-consent rollout — but the grant still exists, and anything that reaches the provider without passing the layer recovers the whole scope: a leaked refresh token, a debugging script written against the raw API, a second integration on the same client id, a bypass someone added during an outage and forgot to remove. Enforcement narrows what happens; only revocation narrows what is possible.

The second is the trust boundary. Nothing at a routing point makes untrusted content stop being read as instructions; per-request policy shrinks what a successful injection reaches, which is worth a great deal, and it is not prevention.

There is a residue inside the four that do collapse, and it is the same shape every time: the arithmetic centralises and the bugs do not. A shared quota bounds a runaway; it does not tell you whether your planner has a checkable success predicate, whether your pagination stops on the cursor's value, or whether two of your agents defer to each other. A single audit emitter guarantees the record exists; it decides nothing about retention, redaction, or what your compliance documentation is allowed to claim. Those stay yours in either architecture.

[Agentic Fabriq](https://agenticfabriq.com) is a control layer built around that split: agents route through it instead of holding credentials, policy is evaluated per request, and every action is attributed to an agent and the user it acted for. For this guide's purposes the useful shape is the lifecycle question, which is the class most estates cannot answer at all — what is registered, what can it reach, and who owns it:

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient

OWNERS = {"meeting-brief": "dana@example.com", "support-triage": "ravi@example.com"}
DEPARTED = frozenset({"ravi@example.com"})


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        agents = await af.list_agents()
        tools = await af.list_tools()
        names = sorted(a["name"] for a in agents if isinstance(a, dict) and a.get("name"))

        print(f"{len(names)} agents registered, {len(tools)} tools reachable")
        for name in names:
            owner = OWNERS.get(name)
            if owner is None:
                print(f"UNOWNED   {name}  -- callable, named by nobody")
            elif owner in DEPARTED:
                print(f"ORPHANED  {name}  -- owner {owner} has left; who stops it?")


asyncio.run(main())
```

The runnable version is `examples/fail-index/fabriq_inventory.py`, which also handles the response shapes this snippet assumes away. Two caveats before you run it. The published `agentic-fabriq-sdk` package has lagged its own README on the `af_sdk.fabriq_client` and `af_sdk.dx` module paths, so check the import against the version you actually installed. And agent names, tool names, and response shapes are per deployment — run `afctl agents list` and `afctl tools list` against your own gateway rather than trusting anything above.

What a layer like [Agentic Fabriq](https://agenticfabriq.com) changes is the cost of doing this repeatedly across a fleet, not what a correct answer looks like. Everything in the step-by-step stays correct if you would rather own the machinery yourself, and at a handful of agents owning it is genuinely viable: a manifest, a scheduled diff, an append-only table, and a stop flag will get you to a defensible place with no new infrastructure. What does not survive multiplication is doing it once per integration, by whoever wired that integration up, on the day they wired it.

## Further reading

Each guide below is the full treatment of one part of this map.

The failure pillar has three. [Over-scoped OAuth](/fail/over-scoped-oauth/) is the permissions class end to end: why the broad scope got granted, why nobody narrows it, the arithmetic that turns "over-scoped" into a count you can put in a ticket, and a scope audit that diffs granted against exercised and refuses when its method mapping is incomplete. [Token leakage through agent context](/fail/token-leakage/) is the credentials class: the six paths a secret takes out of a context window, a broker refactor that closes the whole class rather than blocking instances, and the revoke-then-rotate-then-scope response. [Runaway loops and rate-limit blowups](/fail/runaway-loops/) is the control-flow class: the five loop shapes and how to recognise each on a graph, retry amplification, jittered backoff, idempotency keys, circuit breakers, and a run budget demonstrated stopping a deliberately broken agent.

The [governance pillar](/govern/) has the five controls those failures need. [OAuth flows for AI agents](/govern/oauth-flows/) covers the three grants that survive current security guidance and the token exchange most teams skip, which is what stops a downstream service receiving everything the user consented to. [Delegated user identity](/govern/delegated-identity/) is where the second principal comes from: the on-behalf-of exchange, the intersection rule, revocation that arrives mid-run, and the audit record naming both parties. [Designing least-privilege scopes](/govern/least-privilege/) is the method that produces a defensible grant in the first place — enumerate the actions from the tool definitions, map each to the narrowest scope, remove the rest, and ratchet. [Credential vaulting and rotation](/govern/credentials/) explains why an environment variable cannot express per-user access, how to issue a credential per request, and the six-step rotation whose overlap window has a computable length. [Audit trails and compliance](/govern/audit/) is the record schema field by field, the append-only store, and the query an investigation actually runs.

The [connect pillar](/connect/) is the other end of all of this: per-provider scope tables, consent flows, and the integration decisions that decide which of these failures are even available to you.

Primary sources for the external claims above:

- [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) — the definition of indirect prompt injection, and that no fool-proof method of prevention is known.
- [OWASP LLM02:2025 Sensitive Information Disclosure](https://genai.owasp.org/llmrisk/llm022025-sensitive-information-disclosure/) — that system prompt restrictions may not be honoured and can be bypassed by prompt injection.
- [OWASP LLM06:2025 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/) — excessive functionality, permissions, and autonomy as the root causes.
- [The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — private data, untrusted content, and external communication as the combination that makes exfiltration easy.
- [Google Drive API-specific authorization and authentication](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) — the `drive.readonly` and `drive.file` scope strings, Google's description of each, and their classifications.
- [Using OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server) — the `https://oauth2.googleapis.com/revoke` endpoint, which takes a token rather than a scope list.
- [OAuth Token Audit Activity Events](https://developers.google.com/workspace/admin/reports/v1/appendix/activity/token) — the `activity` event and its `api_name` and `method_name` parameters.
- [Google Workspace audit log data retention and lag times](https://knowledge.workspace.google.com/admin/reports/data-retention-and-lag-times) — six-month retention and the "up to a few hours" lag on OAuth log events.
- [Modifying a GitHub App registration](https://docs.github.com/en/apps/maintaining-github-apps/editing-a-github-apps-permissions) — that removed permissions take effect immediately, with no per-installation approval.
- [W3C Trace Context](https://www.w3.org/TR/trace-context/) — the `traceparent` header and the 32-hex-digit trace-id reused as a correlation id.
- [NIST SP 800-53 Rev. 5](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final) — AU-3 on the required content of an audit record, and AU-9 on protecting audit information from the audited component.
