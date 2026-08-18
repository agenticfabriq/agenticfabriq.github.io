---
title: Connecting an AI Agent to Slack
description: "How to give an AI agent scoped, revocable access to a Slack workspace: bot versus user tokens, real scope names, request signature checks, and rate limits."
pillar: connect
updated: "2026-08-18"
keywords:
  - ai agent slack integration
  - slack bot token scopes
  - slack app agent
  - slack signing secret verification
  - socket mode vs events api
  - slack api rate limits
relatedGuides:
  - connect/gmail
  - govern/delegated-identity
  - fail/over-scoped-oauth
howTo:
  - name: Create a Slack app and decide where it installs
    text: Register an app that belongs to the agent alone, and choose a single-workspace install over an org-wide install unless the agent genuinely spans an Enterprise Grid organization.
  - name: Pick the smallest bot scope set that does the job
    text: Start with channels:read, channels:history, and chat:write, and add nothing whose absence you have not seen break a specific API call.
  - name: Install the app and read back the granted scopes
    text: Exchange the OAuth code at oauth.v2.access, check the state parameter, and store the scope string from the response rather than assuming you got what you asked for.
  - name: Verify the signature on every request Slack sends
    text: Recompute an HMAC-SHA256 over the literal string v0 colon timestamp colon raw body, compare it in constant time, and reject any timestamp more than five minutes from now.
  - name: Choose Socket Mode or a public request URL
    text: Use Socket Mode with an xapp- app-level token when the agent runs inside a VPC, and a signed HTTPS request URL when the app must be listable in the Slack Marketplace.
  - name: Read a channel's recent messages
    text: Call conversations.history with an explicit limit, page with the cursor from response_metadata, and treat not_in_channel as a membership problem rather than a scope problem.
  - name: Post the reply as the bot
    text: Call chat.postMessage with thread_ts set to the parent message's ts so the answer stays attached to the question it answers.
faq:
  - question: Should an AI agent use a Slack bot token or a user token?
    answer: A bot token, which starts with xoxb-. Slack states that write actions taken with a user token are performed as if by the user themselves, so a user token makes the audit trail name a human for something the agent did.
  - question: Which Slack scopes does an AI agent need to read and reply in a channel?
    answer: channels:read to resolve a channel name to an id, channels:history to read public channel messages, and chat:write to post. With a bot token the app must also be a member of the channel; the scope alone is not enough.
  - question: How do you verify that a request really came from Slack?
    answer: Compute an HMAC-SHA256 of the string v0:{timestamp}:{raw request body} keyed with the app's signing secret, prefix the hex digest with v0=, and compare it against the X-Slack-Signature header in constant time. Reject any X-Slack-Request-Timestamp more than five minutes from local time.
  - question: What is the difference between Socket Mode and the Events API request URL?
    answer: Socket Mode delivers events over an outbound WebSocket opened with an xapp- app-level token, so no inbound port is exposed. A request URL is a public HTTPS endpoint you host and must sign-verify. Slack does not currently allow Socket Mode apps in the public Slack Marketplace.
  - question: Why does conversations.history only return 15 messages?
    answer: Since 29 May 2025 Slack limits conversations.history and conversations.replies to one request per minute and 15 objects per request for new apps distributed outside the Slack Marketplace and for new installations of existing ones. Internal customer-built apps and Marketplace-listed apps keep the older limits.
  - question: How does an agent find out that a Slack workspace uninstalled it?
    answer: Subscribe to the app_uninstalled and tokens_revoked events, and treat the ok:false errors token_revoked, account_inactive, and invalid_auth as terminal rather than retryable.
---

## TL;DR

- Give the agent a bot token (`xoxb-`). A user token (`xoxp-`) makes Slack record a human as the author of everything the agent writes, and Slack documents it as reaching all public conversations rather than only the ones the agent was invited to.
- Scopes gate methods; channel membership gates data. `channels:history` grants "View messages and other content in public channels that your Slack app has been added to" — two separate gates, and forgetting the second one produces `not_in_channel` on a channel you can already list.
- Verify every inbound request: HMAC-SHA256 over `v0:{timestamp}:{raw body}` keyed with the signing secret, compared in constant time against `X-Slack-Signature`, with anything more than five minutes off `X-Slack-Request-Timestamp` rejected outright.
- Socket Mode removes the public request URL entirely — app-level `xapp-` token, `connections:write`, `apps.connections.open` — and in exchange Slack does not currently allow Socket Mode apps in the public Slack Marketplace.
- Read throughput is the sharp edge, not write throughput: an app commercially distributed outside the Slack Marketplace, newly created or newly installed since 29 May 2025, gets `conversations.history` at one request per minute returning at most 15 objects. Internal customer-built apps are exempt.

## Who this is for

You are putting an agent into a Slack workspace where it will read channels, answer mentions, and post on its own behalf, and someone will later ask which conversations it read and whose authority it acted under. This guide covers the whole path: app creation, scope selection, OAuth install, request verification, event transport, and the read-and-reply loop, plus what changes when the same agent serves many workspaces. Skip it if all you need is a one-way alert feed into a single channel — an incoming webhook, installed with the `incoming-webhook` scope and bound to one channel, gives you that with no event subscriptions, no request verification, and no read access at all.

## The problem

The failure starts as a shortcut that looks like empathy for the user. An agent should behave the way a person behaves, so someone adds `user_scope=channels:history,chat:write` to the install URL and hands the agent the resulting `xoxp-` token. It works immediately, which is the trap. From that moment Slack records the human as the author of everything the agent writes — the token reference says write actions with a user token are "performed as if by the user themselves", and there is no field on a Slack message that says "an app did this on someone's behalf". Six months later, when someone asks who sent the message that pasted a customer's name into a shared channel, the honest answer is "an agent" and the recorded answer is a person's name. That gap is the whole governance problem in one line, and it is why [delegated identity](/govern/) is a design decision rather than a detail.

The read side is larger than it looks, and Slack documents it. The `conversations.history` reference tabulates what each token type reaches: a bot token gets "Any conversation the relevant bot is a member of", and a user token gets "Any private conversation the user is a member of, and all public conversations". Its error table says the same thing from the other direction — "Only user tokens can access public channels they are not in." So an agent holding an `xoxp-` token with `channels:history` reads every public channel in the workspace, including the ones nobody invited it to and the ones created next month. Membership stops being a gate at all. The grant is also attached to a person rather than to a task, so it tracks that person's entitlements as they change, and nobody re-reviews an agent's access because an engineer joined the incident channel last Tuesday.

The second failure is quieter and has no error message at all. A bot token with `channels:history`, `im:history`, `files:read`, and `users:read` accumulates. Not because anything went wrong — because that is what the scopes do. `users:read` grants "View people in a workspace", which is the directory. `files:read` grants "View files shared in channels and conversations that your Slack app has been added to", which is every PDF, screenshot, and spreadsheet anyone dropped in a channel the bot was invited to. `im:history` grants direct messages the app was added to. None of these announces itself. The agent simply gets a slightly larger view every time someone invites it somewhere, and the union of those invitations is never reviewed by anyone, because there is no page in Slack that says "here is everything this app can now read". Least privilege here is therefore not a scope decision you make once at install time — it is a standing review of scopes times memberships, which is the framing the [govern pillar](/govern/) takes.

The third failure is the one that turns into an incident report. An Events API request URL that does not verify signatures is an unauthenticated endpoint that takes instructions. Anyone who learns the URL can post a synthetic `app_mention` payload and make the agent act. If you verify the signature but skip the timestamp check, a request captured once can be replayed indefinitely, which converts a single recorded webhook into a permanent remote trigger. That is not a hardening nicety; it is the difference between an endpoint with a trust boundary and one without.

## Step by step

One working path: create an app, install it with three bot scopes, verify an inbound event, read a channel, and reply as the bot. Every scope string, header, method, and error code below is Slack's, and the runnable sources are `examples/connect-slack/verify_signature.py` (request verification and the events endpoint) and `examples/connect-slack/slack_agent.py` (the read-and-reply loop).

### 1. Create the app and decide where it installs

Create the app at Slack's app management console and give it to the agent alone. Sharing one app between an agent and a human-facing integration means one uninstall kills both, one scope list covers both, and the workspace admin reviewing permissions sees the union of two unrelated jobs.

The bigger decision is the install target. A **workspace install** grants the app's scopes inside one workspace, and a workspace admin can approve it. An **org-wide install** on Enterprise Grid is a different act: Slack's enterprise documentation says an Org Admin receives a direct message from Slack to review the request, and the `oauth.v2.access` response comes back with `is_enterprise_install` set to `true` when "the installation happened on an organization, as opposed to an individual workspace".

Two things change and both surprise people. First, an org-wide install does not fan out automatically — Slack states that when an Org Admin installs an app across the organization, "it will not yet be installed to any workspaces in the organization". Second, the API gets a new required argument: Slack documents that `team_id` is required for 25 or more methods when an org-ready app calls them, including `conversations.list`, `users.list`, and `search.messages`. Code written against a single-workspace install will fail the moment the same code runs under an org token, and it will fail on argument validation rather than on permissions, which sends people looking in the wrong place.

Choose the workspace install unless the agent genuinely has to act across workspaces. An org-wide grant is a much larger blast radius approved by one person, and it is exactly the shape of grant that [over-scoped installs](/fail/) are made of.

### 2. Pick the smallest bot scope set that does the job

These are the bot scopes that matter for an agent, quoted from Slack's scope reference:

| Scope | What Slack says it grants |
| --- | --- |
| `channels:read` | View basic information about public channels in a workspace |
| `channels:history` | View messages and other content in public channels that your Slack app has been added to |
| `groups:history` | View messages and other content in private channels that your Slack app has been added to |
| `im:history` | View messages and other content in direct messages that your Slack app has been added to |
| `mpim:history` | View messages and other content in group direct messages that your Slack app has been added to |
| `chat:write` | Send messages as your Slack app |
| `chat:write.public` | Send messages to channels your Slack app isn't a member of |
| `channels:join` | Join public channels in a workspace |
| `files:read` | View files shared in channels and conversations that your Slack app has been added to |
| `users:read` | View people in a workspace |
| `users:read.email` | View email addresses of people in a workspace |
| `app_mentions:read` | View messages that directly mention your Slack app in conversations that the app is in |

The read-and-reply agent in this guide needs exactly three: `channels:read`, `channels:history`, `chat:write`. Add `app_mentions:read` if the agent should be triggered by being mentioned rather than by reading everything. `channels:join` is the scope [`conversations.join`](https://docs.slack.dev/reference/methods/conversations.join/) requires, so an app can add itself to a public channel instead of waiting for an invite.

Note what the `*:history` descriptions have in common: they are bounded by "that your Slack app has been added to". That is a real limit and it is the best property Slack's model has. A bot token with `channels:history` cannot read a public channel nobody invited it to, which is the asymmetry against user tokens that The problem set out. But the same sentence contains the trap. The bound is **membership, not time**. `conversations.history` documents `oldest` with a default of `0` and instructs you to "Call the method with no `oldest` or `latest` arguments to read the entire history for a conversation". Slack's unit of access is the conversation, not a time slice. So the moment someone types `/invite @agent` in a channel that has existed for three years, the agent is one paginated loop away from the whole thing — the incident retro, the vendor negotiation, the thread where somebody pasted a production connection string. Nobody experiences this as granting access; they experience it as adding a bot to a channel.

One boundary Slack's documentation does not settle: whether the app's join time floors that range, so that "the entire history" means everything or only everything since the invite. The reference states the access unit and the default; it does not state that edge. Post a message in a throwaway channel, invite the app afterwards, and read the result before you decide your retention posture.

Two scopes deserve a specific argument before you add them. `users:read.email` turns a Slack user id into a company email address, which is the join key between your Slack logs and every other system you run; add it only if the agent actually needs to correlate identities. And `chat:write.public` lets the app post into channels it is not a member of, which quietly removes the invite as a consent signal — the thing a channel owner did to opt in. If you take `chat:write.public`, you should be able to name the workflow that needs it.

Scopes are additive on reinstall and cannot be trimmed by reinstalling with fewer. Slack's install documentation is explicit that subsequent authorizations add scopes and that removing them requires revocation. So the cost of asking for one scope too many is not "a slightly larger consent screen", it is a grant that persists until somebody deliberately revokes the token.

### 3. Install the app, then read back what was actually granted

For a single internal workspace, the console's install button is enough. For anything distributed, run OAuth v2: redirect to `https://slack.com/oauth/v2/authorize` with `client_id`, a comma-separated `scope` list, an optional `user_scope`, your `redirect_uri`, and a `state` value. Slack's guidance on the return leg is direct: "Check the `state` parameter if you sent one along with your initial user redirect. If it doesn't match what you sent, consider the authorization a forgery." Compare it with a constant-time comparison against a value you generated from a CSRF-safe source.

Exchange the code with a POST to `https://slack.com/api/oauth.v2.access` carrying `code`, `client_id`, and `client_secret`. The response separates the two identities cleanly, and that separation is the point:

```json
{
  "ok": true,
  "access_token": "xoxb-...",
  "token_type": "bot",
  "scope": "channels:history,channels:read,chat:write",
  "bot_user_id": "U0...",
  "authed_user": { "id": "U1...", "access_token": "xoxp-...", "token_type": "user" },
  "team": { "id": "T0...", "name": "Example" }
}
```

`access_token` is the bot token. `authed_user.access_token` only exists if you asked for `user_scope`, and if you did not, its absence is the desired outcome, not a bug. Store the top-level `scope` string rather than the list you requested — Slack recommends storing the granted scopes from the response — because what you asked for and what the workspace approved are different facts, and only one of them determines whether your next call returns `missing_scope`.

Slack states that OAuth tokens do not expire until revoked via `auth.revoke`. That is convenient and it is also the risk: an `xoxb-` token in a config file is a standing grant with no clock on it. If you want an expiry, opt into token rotation, which issues access tokens with a documented lifetime of 43,200 seconds (12 hours) plus a refresh token, exchanged with `oauth.v2.access` using `grant_type=refresh_token`. Slack's rotation documentation states plainly that "token rotation may not be turned off once it's turned on", so treat enabling it as a one-way door and test the refresh path before you flip it in production. The rotation docs show the rotated bot credentials as an access token prefixed `xoxe.xoxb-` paired with a refresh token prefixed `xoxe-1-`, and the user-token equivalent prefixed `xoxe.xoxp-`. Refresh tokens are single-use, so persist the new one from every exchange before you use the access token it arrived with.

### 4. Verify the signature on every request Slack sends you

Slack signs each request to your request URL. The construction, from Slack's verification page: take the literal `v0`, the value of `X-Slack-Request-Timestamp`, and the raw request body, join them with colons, and HMAC that string with your signing secret using SHA-256. The expected `X-Slack-Signature` is `v0=` followed by the hex digest.

```python
def is_signature_valid(headers, raw_body: bytes, now: float | None = None) -> bool:
    timestamp = headers.get("X-Slack-Request-Timestamp")
    signature = headers.get("X-Slack-Signature")
    # compare_digest raises TypeError on a str holding a non-ASCII code point.
    if not timestamp or not signature or not signature.isascii():
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - sent_at) > 60 * 5:
        return False
    basestring = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    digest = hmac.new(SIGNING_SECRET, basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)
```

Three details do the work. The **raw bytes** must be the bytes that arrived: parsing JSON and re-encoding it changes whitespace and key order, and the digest with it, so capture the body before any middleware touches it. The **five-minute window** is Slack's own — its guidance is to "verify that the timestamp does not differ from local time by more than five minutes" — and it is the only thing standing between you and unlimited replay of a captured request. The **constant-time compare** is likewise Slack's recommendation to use an HMAC compare function rather than comparing signatures for equality, which keeps the digest from leaking a byte at a time through response timing.

The first request you receive is the handshake. Slack sends `{"type": "url_verification", "challenge": "..."}` and expects the challenge echoed back with a 200. Handle it before your event dispatch, and still verify its signature — it is a request from Slack like any other.

### 5. Choose Socket Mode or a public request URL

The Events API delivers to an HTTPS URL you host. Socket Mode delivers over a WebSocket your app opens outbound: you mint an app-level token (`xapp-` prefix) with the `connections:write` scope, call `apps.connections.open` with it, and connect to the URL you get back. Slack's own framing is that Socket Mode "helps developers working behind a corporate firewall, or who have other security concerns that don't allow exposing a static HTTP endpoint".

For an agent inside a VPC, that is decisive. There is no inbound path to open, no certificate to rotate on a public hostname, no load balancer whose access log now contains message text, and no signature verification to get wrong, because there is no request URL to forge against. The costs are real too: you own a long-lived connection and its reconnect logic, the app must be running to receive anything at all (a request URL at least queues behind Slack's retries), and Slack states that "apps using Socket Mode are *not* currently allowed in the public Slack Marketplace". If you intend to list the app, that sentence makes the decision for you.

If you take the request URL, the timing contract is strict. Slack requires an HTTP 2xx "within three seconds", retries up to three times — near-immediately, then after one minute, then after five — and marks each retry with `x-slack-retry-num` and `x-slack-retry-reason` headers. An app that fails more than 95% of delivery attempts within 60 minutes has its subscriptions temporarily disabled. Those numbers are the whole contract, and the failure mode *Slack turns your event subscriptions off* below turns them into a handler design.

### 6. Read a channel's recent messages

The Web API lives at `https://slack.com/api/METHOD_FAMILY.method`. Slack prefers the token in the `Authorization` header as `Bearer`, and when you send a JSON body that is the only place it can go. Every response is a JSON object with a top-level `ok`; on failure `error` carries a short machine-readable code — and the HTTP status is still 200. Branch on `error`, never on the status code, with 429 and 5xx as the two exceptions.

Slack meters by method tier rather than one global budget, and each method's reference page names its tier: Tier 1 is "1+ per minute", Tier 2 "20+ per minute", Tier 3 "50+ per minute" for the paginated collection methods, and Tier 4 "100+ per minute". `chat.postMessage` sits in a special tier that Slack describes as generally one message per second per channel "while also maintaining a workspace-wide limit" — Slack does not publish that second number, so do not size a fan-out against a value you found in a blog post. Exceeding a limit returns HTTP 429 with a `Retry-After` header in seconds and the `ratelimited` error code; honour the header as given.

Resolve the channel once with `conversations.list` (Tier 2, 20+ per minute; `limit` defaults to 100 and Slack documents it as "must be an integer under 1000", so 999 is the ceiling; `types` defaults to `public_channel`), cache the id, then read:

```python
def recent_messages(channel_id: str, limit: int = 15) -> list[dict]:
    body = call("conversations.history", {"channel": channel_id, "limit": limit})
    return body.get("messages", [])
```

`conversations.history` documents a default `limit` of 100 and a maximum of 999 — but read the failure mode *`conversations.history` returns fifteen messages, once a minute* below before you rely on either number, because for a large class of apps both collapse to 15. Page with `response_metadata.next_cursor` and stop when it is empty. Pass `oldest` with the timestamp of the last message you processed so a restart does not re-read the backlog; that one argument is also the cheapest way to keep an agent from ingesting years of history it has no business summarising.

Two errors here mean different things and get confused constantly. `missing_scope` means the token lacks the permission, and Slack tells you exactly what is missing — the response carries `needed` and `provided` fields, whose shape Slack shows in the [`search.all` error example](https://docs.slack.dev/reference/methods/search.all/), so print them instead of guessing. `not_in_channel` means the token has the scope and the app is not a member. The fix for the second is an invite (`/invite @yourapp`) or a `conversations.join` call, which itself needs `channels:join`.

### 7. Post the reply as the bot

`chat.postMessage` needs `chat:write` and posts as the app, which is the attribution you want:

```python
body = call(
    "chat.postMessage",
    {"channel": channel_id, "thread_ts": parent.get("thread_ts", parent["ts"]), "text": text},
    json_body=True,
)
```

`thread_ts` is the `ts` of the parent message, and it is what keeps the answer attached to the question. Note the fallback in that expression: a message that is itself inside a thread carries the parent's `ts` in its own `thread_ts`, so replying to `parent["ts"]` blindly starts a nested thread under a reply. Set `reply_broadcast` only when the channel genuinely needs to see it.

Slack's `chat.postMessage` reference gives 4,000 characters as the recommended ceiling for `text` and 40,000 as the point beyond which messages are truncated. For an agent that is a useful forcing function: if the model's answer does not fit, the answer is too long for a channel anyway. `is_archived`, `channel_not_found`, and `restricted_action` — "a workspace preference prevents the authenticated user from posting" — are the errors worth handling explicitly; the rest of the surface is authentication.

## Decision table

Who the action is attributed to:

| Option | When it wins | What it costs | What breaks first |
| --- | --- | --- | --- |
| Bot token (`xoxb-`) | Default for every agent. The app is a distinct actor with its own scopes, its own channel memberships, and its own name in the transcript. | The bot must be invited to each conversation it reads, which feels like friction and is actually the consent signal. | Reach. It cannot see a channel nobody added it to — which is the feature, until someone asks it to summarise one. |
| User token (`xoxp-`) | An action the user explicitly asked to be taken in their own name, or a method Slack exposes only to user tokens. | Attribution is destroyed at the source, for the reason quoted in The problem, and reach follows the person rather than the task. | Audit. The transcript names a human for an action software took, and no logging you add downstream can undo that. |
| Brokered access through a control layer | Many workspaces, many users, one agent, and an auditor who will ask which user caused a given message. | A dependency and a policy model to run. | Nothing early. The cost is up front rather than in the incident. |

How events reach the agent:

| Transport | When it wins | What it costs | What breaks first |
| --- | --- | --- | --- |
| Socket Mode (`xapp-` + `connections:write`) | The agent runs inside a VPC or behind a firewall, and the app is internal. | A persistent outbound WebSocket to supervise and reconnect; events arriving only while the process is up; no listing in the public Slack Marketplace. | Availability. Nothing is delivered to a process that is not connected. |
| Events API request URL | You need a listable app, or event handling that survives the agent being down. | A public HTTPS endpoint, correct signature verification, and a 2xx inside three seconds. | Latency. Inline model calls miss the three-second window and Slack retries into a duplicate. |

## Checklist

- [ ] No `xoxp-` user token is issued to the agent, and `user_scope` is absent from the install URL.
- [ ] The requested bot scopes are exactly the ones you can tie to a named API call, with `users:read.email` and `chat:write.public` each justified in writing or absent.
- [ ] The `scope` string from the `oauth.v2.access` response is stored and compared against what you requested, rather than assumed.
- [ ] The `state` parameter is generated per-install and compared with a constant-time comparison on the callback.
- [ ] Signature verification runs on the raw request bytes, before any JSON parse or re-encode.
- [ ] The `X-Slack-Request-Timestamp` check rejects anything more than five minutes from local time, and there is a test that feeds it an old timestamp with a valid signature.
- [ ] The signing secret and bot token are read from a secret store, never committed, and excluded from logs and crash reports.
- [ ] The events endpoint returns 2xx before the model is called, and dedupes on `event_id`.
- [ ] Error handling branches on the `error` string in the body, not on the HTTP status, with 429 and 5xx handled separately.
- [ ] `Retry-After` is honoured as given rather than replaced by a backoff curve of your own.
- [ ] `token_revoked`, `account_inactive`, and `invalid_auth` stop the agent and mark the install dead instead of retrying.
- [ ] The app subscribes to `app_uninstalled` and `tokens_revoked`, and both delete stored tokens.
- [ ] `conversations.history` is called with an explicit `limit` and an `oldest` watermark, not with the defaults.
- [ ] You can list, for each workspace, every channel the app is a member of, and someone reviews that list on a schedule.
- [ ] Message text and file contents are truncated or redacted before they reach a model prompt.

## Failure modes

### The audit trail says a human posted

**Symptom:** an investigation into a message that leaked something lands on a named employee, who did not send it. Slack's own transcript, the export, and the search results all show their name.

**Cause:** the app was installed with `user_scope` and the agent posts with the `xoxp-` token, which Slack treats as the user acting — the rule quoted in The problem. There is no field in the message that records "an app did this on behalf of a user"; the identity is simply the user's.

**Fix:** move to a bot token and re-install without `user_scope`. Where a user token is genuinely required, keep your own request log that binds each API call to the agent, the user it acted for, and the reason — because Slack's record will not contain that binding. This is the exact problem [governing delegated identity](/govern/) exists to solve, and it is cheaper to solve before the transcript exists.

### `not_in_channel` on a channel the agent can already see

**Symptom:** `conversations.list` returns the channel, the token clearly has `channels:history`, and `conversations.history` on that channel id returns `{"ok": false, "error": "not_in_channel"}`.

**Cause:** two independent gates. `channels:read` lets the app enumerate public channels; `channels:history` lets it read messages in public channels "that your Slack app has been added to". Listing a channel is not membership.

**Fix:** invite the app (`/invite @yourapp`) or call `conversations.join`, which needs `channels:join`. Resist the reflex to add `chat:write.public` and treat the invite as unnecessary — the invite is the only per-channel consent step Slack gives you, and removing it means the agent's reach is bounded by nothing but its scope list.

### A replayed request sails through signature verification

**Symptom:** nothing, until a penetration test or an audit finds it. A request captured from your logs, a proxy, or a shared tunnel replays cleanly weeks later and the agent acts on it every time.

**Cause:** the signature was verified and the timestamp was not. The HMAC covers the timestamp, so a replayed request has a perfectly valid signature forever; the timestamp is only meaningful if you compare it to now.

**Fix:** reject any request whose `X-Slack-Request-Timestamp` differs from local time by more than five minutes, as Slack's verification guidance specifies, and unit-test that path with an old timestamp and a correctly computed signature. Use a constant-time compare for the digest itself. If you terminate TLS somewhere that rewrites bodies, verify before the rewrite or the signature check will fail for a reason that has nothing to do with security.

### Slack turns your event subscriptions off

**Symptom:** events stop arriving. No deploy, no config change, and the endpoint answers fine when you curl it.

**Cause:** your handler misses the three-second acknowledgement more often than not, and Slack's 95% rule eventually disables the subscription. An agent that runs an inference call inside the request handler exceeds that window as a matter of course, so the failure rate is a property of the design, not of a bad day.

**Fix:** acknowledge with a 2xx immediately, enqueue the payload, and process it out of band. Dedupe on `event_id`, because the retries you already triggered carry the same id and a duplicate reply is worse than a slow one. For a payload you will never handle, return a non-2xx carrying `x-slack-no-retry: 1` so Slack stops rather than counting three more failures against you.

### `conversations.history` returns fifteen messages, once a minute

**Symptom:** a backfill that worked in development crawls in production, or the agent starts returning partial answers with no error. The method still succeeds; it simply returns 15 messages regardless of the `limit` you pass.

**Cause:** as of 29 May 2025, Slack limits `conversations.history` and `conversations.replies` to 1 request per minute and a maximum of 15 objects per request for apps commercially distributed outside the Marketplace — immediately for new unlisted apps and for new installations of existing unlisted apps. Slack's changelog states that internal customer-built applications are not impacted and retain 50+ requests per minute with 1,000 objects, and a follow-up post repeated that internal customer-built apps "will maintain their existing rate limits". The method reference still documents a default of 100 and a maximum of 999, which is what makes this so confusing to hit: the docs and your responses disagree, and both are right for different apps.

**Fix:** first, establish which class your app is in — internal customer-built, Marketplace-listed, or distributed outside the Marketplace — because the answer changes your architecture, not just a constant. Then stop backfilling: drive the agent from the Events API or Socket Mode so messages arrive as they happen, and use `conversations.history` only to catch up from an `oldest` watermark. An agent that reads history on a timer is spending its scarcest quota on data it mostly already has.

### The workspace uninstalled the app and the agent kept calling

**Symptom:** a workspace's calls all fail with the same error and your retry loop hammers Slack for days. Nothing in your deployment changed.

**Cause:** the install is gone, or the token was revoked. Slack returns `token_revoked` for "a deleted user or workspace or the app has been removed", `account_inactive` for a deleted user or workspace on a bot token, and `token_expired` for an expired rotating token. These are terminal states dressed as ordinary `ok:false` errors, so generic retry logic treats them as transient.

**Fix:** subscribe to `app_uninstalled`, which Slack sends when an app is "completely uninstalled", and to `tokens_revoked`, whose payload carries `tokens.oauth` and `tokens.bot` arrays naming the affected user ids. Neither requires a scope. On any of those signals, or on `token_revoked` / `account_inactive` / `invalid_auth` from an API call, delete the stored token, mark the workspace as needing re-installation, and stop calling. [`auth.test`](https://docs.slack.dev/reference/methods/auth.test/) needs no scopes and is the cheapest way to check a stored token's liveness before a batch of work.

### The agent reads a channel it was never meant to read

**Symptom:** no error. A prompt-log review turns up a customer name, a salary figure, or a filename from a channel nobody remembers connecting to the agent.

**Cause:** somebody invited the bot to a channel. That is all it takes. Scope was granted once, months ago, by an admin who reviewed a list of scope strings; membership is granted continuously by anyone with an `/invite`. The union of those two grants is the agent's actual reach, and Slack surfaces the first one clearly and the second one not at all.

**Fix:** enumerate membership as a routine, not as an investigation. [`users.conversations`](https://docs.slack.dev/reference/methods/users.conversations/) defaults to the conversations the calling bot belongs to and needs only `channels:read` (or the matching `groups:read`, `im:read`, `mpim:read` for other types), so it is a cheap scheduled job. Review that list and leave channels the agent has no mandate in. Truncate message text and skip file contents before anything reaches a prompt. The [failure pillar](/fail/) covers how this material escapes once it is in the pipeline — prompt logs, caches, and third-party inference endpoints are all downstream of a decision made by whoever typed the invite.

## Doing this at scale

One workspace is a weekend. The fleet version is the actual work, and Slack's shape makes it specific. You hold one `xoxb-` token per workspace, and if you have opted into rotation you hold a refresh token per workspace and a 12-hour clock on every access token, which means a refresh path that must be correct at 3am. You hold one signing secret per app, which every replica needs and none should log. You detect a dead install from three different places — the `app_uninstalled` event, the `tokens_revoked` event, and the `ok:false` codes on ordinary calls — and all three have to converge on the same "this workspace is gone" state, or you will keep calling on behalf of a customer who left. Org-wide installs add a `team_id` argument to a couple of dozen methods, so the same code path behaves differently depending on how a given customer installed you.

Then there is the question the auditor actually asks, which is not about tokens at all: *which user caused this message?* Slack will tell you the bot posted it. Your own records have to supply the rest — the mention or ticket that triggered the run, the user id behind it, the policy that permitted the agent to post in that channel, and the time. If that binding lives only in application logs, answering the question means grepping, and grepping is not an audit trail. The same reasoning applies to [Gmail and every other system the agent touches](/connect/gmail/): the per-integration work is small and the credential-and-attribution lifecycle around it is not, which is why it is worth solving once rather than once per provider.

That lifecycle is what [Agentic Fabriq](https://agenticfabriq.com) is built for. Credentials live in the control layer rather than in the agent process; the agent holds a token for the layer and calls a named connection. Policy is evaluated per request, and every call is attributed to an agent and the user it acted for.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        channels = await af.invoke_connection("slack", method="get_channels", parameters={})
        for channel in channels.get("channels", []):
            print(channel)

        await af.invoke_connection(
            "slack",
            method="post_message",
            parameters={"channel": os.environ["SLACK_CHANNEL_ID"], "text": "Acknowledged."},
        )


asyncio.run(main())
```

The runnable version is `examples/connect-slack/post_message.py`. The property that matters is not the shorter code — it is that no `xoxb-` token and no signing secret live in the agent process, so a compromised agent leaks a revocable gateway token instead of standing read access to a workspace's channels. Connection names and method names are per-deployment, so run `afctl tools list` against your own gateway rather than copying `slack`, `get_channels`, and `post_message` on faith. What a layer like [Agentic Fabriq](https://agenticfabriq.com) buys you is the lifecycle, not the API call; everything above stays correct if you would rather own that lifecycle yourself.

## Further reading

Start with the [connect pillar](/connect/) for how this pattern generalises across providers — the scope taxonomy is different everywhere, but the read-only-first ordering and the revocation path are not. [Connecting an agent to Gmail](/connect/gmail/) is the closest sibling and a useful contrast: Google splits capability by scope, Slack splits it by scope *and* by conversation membership, and only one of those two gates is visible to an administrator reviewing an install. The [govern pillar](/govern/) covers attribution and least privilege as policy rather than per-integration decisions, and the [fail pillar](/fail/) collects what these integrations look like after they go wrong.

Primary sources for everything asserted above:

- [Token types](https://docs.slack.dev/authentication/tokens/) — the `xoxb-`, `xoxp-`, and `xapp-` prefixes, and the user-token attribution rule quoted in The problem.
- [Permission scopes reference](https://docs.slack.dev/reference/scopes/) — the exact scope strings and Slack's own description of each.
- [Installing with OAuth](https://docs.slack.dev/authentication/installing-with-oauth/) — the authorize URL, `scope` versus `user_scope`, the `state` check, the `oauth.v2.access` response shape, additive scopes, and `auth.revoke`.
- [Using token rotation](https://docs.slack.dev/authentication/using-token-rotation/) — the 43,200-second access token lifetime, `oauth.v2.exchange`, `grant_type=refresh_token`, and the one-way switch.
- [Verifying requests from Slack](https://docs.slack.dev/authentication/verifying-requests-from-slack/) — the `v0:{timestamp}:{body}` basestring, HMAC-SHA256, the five-minute window, and the constant-time compare.
- [The Events API](https://docs.slack.dev/apis/events-api/) — the `url_verification` handshake, the three-second requirement, the retry schedule and headers, and the 95%-in-60-minutes disabling rule.
- [Using Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/) — app-level tokens, `connections:write`, `apps.connections.open`, and the Marketplace restriction.
- [Web API rate limits](https://docs.slack.dev/apis/web-api/rate-limits/) — the four tiers, the `chat.postMessage` special tier, HTTP 429, and `Retry-After`.
- [Rate limit changes for non-Marketplace apps](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps) — the 1 request per minute and 15 object limits, and who they apply to.
- [Clarifying rate limit changes](https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/) — the internal customer-built app exemption.
- [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/) — required scopes, the `limit` default and maximum, cursor pagination, and the error table including `not_in_channel` and `missing_scope`.
- [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage/) — `chat:write`, `thread_ts`, the text length guidance, and the error table.
- [`conversations.list`](https://docs.slack.dev/reference/methods/conversations.list/) — the Tier 2 rate limit, `types`, the `limit` ceiling, and the org-wide `team_id` requirement.
- [Developing apps for Enterprise Grid](https://docs.slack.dev/enterprise/developing-for-enterprise-orgs/) — org-wide installs, Org Admin approval, `is_enterprise_install`, and the methods that require `team_id`.
- [`tokens_revoked`](https://docs.slack.dev/reference/events/tokens_revoked/) and [`app_uninstalled`](https://docs.slack.dev/reference/events/app_uninstalled/) — the revocation signals and their payloads.
- [Using the Slack Web API](https://docs.slack.dev/apis/web-api/) — the base URL, bearer authentication, and the `ok`/`error` response contract.
