---
title: Connecting an AI Agent to Gmail
description: How to give an AI agent scoped, revocable access to a Gmail mailbox using OAuth, what each scope actually permits, and the failure modes to plan for.
pillar: connect
updated: "2026-08-18"
keywords:
  - ai agent gmail access
  - gmail api oauth scopes
  - agent email integration
  - gmail restricted scopes
  - domain-wide delegation
  - gmail api quota units
relatedGuides:
  - govern/oauth-flows
  - govern/least-privilege
  - fail/over-scoped-oauth
howTo:
  - name: Create a dedicated OAuth client for the agent
    text: Register an OAuth client in Google Cloud console that belongs to the agent alone, so its grants can be revoked without touching anything else.
  - name: Choose the narrowest Gmail scope that completes the task
    text: Start with gmail.readonly, and add gmail.send only once you know exactly which messages the agent will send.
  - name: Send the user through the consent flow with PKCE
    text: Build an authorization URL with code_challenge_method=S256, access_type=offline, and prompt=consent so Google returns a refresh token.
  - name: Exchange the authorization code and verify granted scopes
    text: Post the code and code_verifier to the token endpoint, then read the scope field of the response instead of assuming the user granted everything.
  - name: Refresh the access token explicitly on every run
    text: Trade the refresh token for an access token before the first API call and treat invalid_grant as a permanent revocation, not a retryable error.
  - name: Read and reply through the Gmail REST API
    text: List unread ids, fetch each message, and send a reply carrying In-Reply-To and References so Gmail threads it correctly.
faq:
  - question: Which Gmail OAuth scope should an AI agent use?
    answer: Start with https://www.googleapis.com/auth/gmail.readonly and add https://www.googleapis.com/auth/gmail.send only when the agent genuinely sends mail. Full access via https://mail.google.com/ is almost never justified.
  - question: Does Gmail access require a Workspace admin?
    answer: Domain-wide delegation needs a super admin to grant it and a super admin to withdraw it, with users involved in neither. Individual OAuth consent usually needs no admin, but a Workspace admin can still block third-party apps or disable Gmail API access for the domain, and then every call fails with a domainPolicy 403.
  - question: Can the agent send email without being able to read the mailbox?
    answer: Yes. The gmail.send scope allows sending mail on the user's behalf without granting any read access to existing messages.
  - question: Why did my Gmail refresh token stop working after seven days?
    answer: An OAuth client with an external user type and a publishing status of Testing issues refresh tokens that expire seven days after consent. Publishing the app to production fixes it.
  - question: What is the difference between gmail.modify and full mailbox access?
    answer: gmail.modify allows reading, composing, sending, and moving messages to trash, but not immediate permanent deletion. The https://mail.google.com/ scope adds permanent deletion of anything.
---

## TL;DR

- Ship the agent with `https://www.googleapis.com/auth/gmail.readonly`, prove the workflow against a real mailbox, then widen. The reverse order does not survive a security review.
- `https://mail.google.com/` grants read, compose, send, and permanent deletion of every message in the mailbox. An agent essentially never needs it.
- The Gmail scopes that read a mailbox — `gmail.readonly`, `gmail.modify`, `gmail.compose`, `gmail.metadata` — are Google *restricted* scopes: a public launch needs app verification, plus an annual CASA security assessment if the data can reach a third-party server. Put weeks, not days, on the calendar.
- Per-user OAuth with a refresh token is the default. Service accounts with domain-wide delegation exist to skip user consent, so only an administrator can grant or withdraw one — which is exactly why reaching for them first is the wrong instinct.
- Budget quota in units, not requests: listing costs 5, reading one message costs 20, sending costs 100, against 6,000 per user per minute and 1,200,000 per project per minute.

## Who this is for

You are wiring an agent into a real mailbox — triage, drafting, extraction, escalation — and someone will eventually ask you which messages it read and on whose authority. This guide covers per-user OAuth against the Gmail API, from the consent screen to the reply, plus the operational shape of running it for more than one user. Skip it if you only need to send transactional mail from an application address: use an SMTP relay or a transactional email provider, because none of the consent, verification, or restricted-scope machinery below buys you anything.

## The problem

The default path is a trap that closes slowly. A tutorial hands you `https://mail.google.com/` because it makes every call work on the first try, and the agent ships. Six weeks later the same grant is still in place, and it now covers a mailbox that has accumulated a legal thread, a compensation discussion, and a customer's bank details in an attachment. The agent does not need any of that, but the mailbox does not know that — a single `messages.get` pulls a full thread body, every participant's address, and every attachment straight into a model's context window. Read [what actually breaks in production](/fail/) for the shape this takes once that context is logged, cached, or forwarded to a third-party inference endpoint.

The capability side is worse than the exposure side. That same convenient scope carries permanent deletion, with no trash to recover from, so the agent is one prompt injection away from destroying mail that has no undo. Nobody chose that risk. It arrived attached to a scope string.

Then there is the failure that wastes a week. The agent runs perfectly for seven days and dies on the eighth with `invalid_grant`, for every user at once. Nothing in the refresh logic is wrong; the expiry is a property of the OAuth client's publishing status, which is not where anyone looks first. Failure modes below has the mechanism and the fix.

## Step by step

The path below is one working per-user read flow with an explicit reply at the end. Every endpoint and parameter is real, and the token refresh is written out rather than hidden behind a client library, because the refresh is where production failures live. Full runnable sources: `examples/connect-gmail/oauth_pkce_consent.py` and `examples/connect-gmail/gmail_agent.py`.

### 1. Create an OAuth client that belongs to the agent

In Google Cloud console, enable the Gmail API and create an OAuth client for this agent and nothing else. Do not reuse the client your web app already has. A dedicated client means the agent's grants show up as a distinct entry in every user's account permissions page, and revoking the agent does not sign anyone out of your product. It also means the scope list on the consent screen describes the agent's job rather than the union of everything your company does.

Client type matters. A server-side agent uses a **Web application** client with an HTTPS redirect URI you control, and its client secret is a real secret. A locally run agent uses a **Desktop app** client with a loopback redirect (`http://127.0.0.1:PORT/...`), which is what the example script uses so you can run it now.

### 2. Choose the narrowest scope that completes the task

These are the Gmail scopes worth knowing, taken from Google's scope reference:

| Scope | What it permits | Google's classification |
| --- | --- | --- |
| `https://www.googleapis.com/auth/gmail.labels` | See and edit labels. No message access at all. | Non-sensitive |
| `https://www.googleapis.com/auth/gmail.send` | Send mail on the user's behalf. No read access, no drafts, no mailbox listing. | Sensitive |
| `https://www.googleapis.com/auth/gmail.metadata` | Labels and headers only. Bodies are never returned. | Restricted |
| `https://www.googleapis.com/auth/gmail.readonly` | View messages and settings. Read everything, change nothing. | Restricted |
| `https://www.googleapis.com/auth/gmail.compose` | Manage drafts and send. Does not grant reading the inbox. | Restricted |
| `https://www.googleapis.com/auth/gmail.modify` | Read, compose, send, and move to trash. Not immediate permanent deletion. | Restricted |
| `https://mail.google.com/` | Read, compose, send, and permanently delete all mail. | Restricted |

Two distinctions carry most of the weight. First, `gmail.send` is a write-only capability: it lets an agent send a message but gives it no way to list, read, or search the mailbox it sends from. That asymmetry is useful — a notification agent needs `gmail.send` and nothing else. Second, `gmail.modify` stops short of immediate permanent deletion; deleted mail goes to trash and can be recovered. The only thing `https://mail.google.com/` adds over `gmail.modify` is irreversible destruction plus full IMAP-equivalent access.

That is why a reviewer seeing `https://mail.google.com/` in a pull request should ask one question: *which call requires permanent deletion?* If the answer is not `messages.delete` on messages the agent itself created, the correct scope is `gmail.modify` or narrower, and the diff goes back. This is the single highest-value review comment on the whole integration. The [governance pillar](/govern/) covers how to make that check systematic rather than a matter of who happened to read the diff.

### 3. Budget for restricted-scope verification before you promise a date

Google classifies these Gmail scopes as restricted: `gmail.readonly`, `gmail.compose`, `gmail.modify`, `gmail.metadata`, `gmail.insert`, `gmail.settings.basic`, `gmail.settings.sharing`, and `https://mail.google.com/`. `gmail.send` is sensitive but not restricted, and `gmail.labels` is neither. So the moment an agent reads anything from a mailbox, you are in restricted territory, and that carries consequences for your schedule:

- Public production access requires OAuth verification. Brand verification alone typically takes a few business days, and Google states restricted-scope verification can take several weeks.
- Any app that can move restricted data to or through a third-party server must pass a CASA security assessment, and must be reverified at least every 12 months. An agent that sends mailbox content to a hosted model is squarely in scope.
- Google's Limited Use requirements constrain what you may do with the data afterwards, including restrictions on human review and on transferring it.

There are real exemptions, and they are narrower than they look. An internal-only app is genuinely exempt: the project is owned by your Google Workspace or Cloud Identity organization, the consent screen is set to an Internal user type, and only people in that organization use it. Most internal agents live here. Domain-wide installation is a different and weaker exemption — an app that targets a single organization and always installs domain-wide skips *brand* verification, but Google states plainly that app verification is still required if the app uses restricted or sensitive scopes. Every Gmail scope that touches mail is one or the other, so a Workspace-targeted agent still needs to budget the time. And an app parked in Testing status to dodge the whole thing is capped at 100 test users and hands you seven-day refresh tokens, which is not a production posture.

### 4. Send the user through consent with PKCE

Build the authorization URL against `https://accounts.google.com/o/oauth2/v2/auth`. The parameters that matter are `code_challenge` and `code_challenge_method=S256`, which bind the authorization code to this client instance; `access_type=offline`, which is what makes Google return a refresh token at all; and `prompt=consent`, which forces a fresh refresh token even for a user who has authorized before.

```python
def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for code_challenge_method=S256."""
    verifier = _b64url(os.urandom(64))  # 86 chars, inside Google's 43-128 range
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


params = {
    "client_id": client_id,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "state": state,
    "access_type": "offline",
    "prompt": "consent",
}
url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
```

The `code_verifier` must be 43 to 128 characters from the unreserved URL character set; 64 random bytes base64url-encoded gives 86 characters and satisfies that. Generate `state` from a CSRF-safe source and compare it with `secrets.compare_digest` when the redirect comes back.

### 5. Exchange the code, then check what you actually got

Post the code to `https://oauth2.googleapis.com/token` with the `code_verifier`. Then read the `scope` field of the response. Google presents restricted scopes as individual permissions the user can decline one at a time, and Google's own guidance is that your app must verify which scopes were actually granted rather than assume.

```python
token = exchange_code(client_id, client_secret, code, verifier)

granted = set(token.get("scope", "").split())
missing = sorted(set(SCOPES) - granted)
if missing:
    raise SystemExit(f"user withheld: {', '.join(missing)}")

refresh_token = token.get("refresh_token")
if refresh_token is None:
    # Google omits it when the user already holds a live grant for this client.
    raise SystemExit("no refresh_token: re-request with prompt=consent, or reuse the stored one")
```

Store `refresh_token` encrypted, keyed by user, never in the same store as your application data and never in an environment variable that gets baked into an image. It is a standing grant on a human being's mailbox that survives process restarts, deploys, and your departure from the company.

### 6. Refresh the access token explicitly

Access tokens are short-lived; the refresh token is the durable object. Refresh before the first call of a run rather than reacting to the first 401, and treat `invalid_grant` as terminal:

```python
def refresh_access_token(refresh_token: str) -> tuple[str, int]:
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code == 400:
        body = response.json()
        if body.get("error") == "invalid_grant":
            raise GrantRevoked(body.get("error_description", "invalid_grant"))
    response.raise_for_status()
    body = response.json()
    return body["access_token"], int(body["expires_in"])
```

### 7. List unread and read a thread

`users.messages.list` returns ids only — `{"id": ..., "threadId": ...}` — so every message you actually read is a second call. That is the whole quota story in one sentence.

```python
def list_unread(token: AccessToken, max_results: int = 10) -> list[dict[str, str]]:
    body = call(token, "GET", "/messages",
                params={"q": "is:unread", "maxResults": max_results})
    return body.get("messages", [])


def get_message(token: AccessToken, message_id: str) -> dict:
    return call(token, "GET", f"/messages/{message_id}", params={"format": "full"})


def get_thread(token: AccessToken, thread_id: str) -> dict:
    """Every message in the conversation in one call. 40 quota units."""
    return call(token, "GET", f"/threads/{thread_id}", params={"format": "full"})
```

Base URL is `https://gmail.googleapis.com/gmail/v1/users/me`. `maxResults` defaults to 100 and caps at 500. Message bodies arrive base64url-encoded inside a nested MIME part tree, so decoding means walking `payload.parts` for the first `text/plain` node — `plain_text()` in the example file does exactly that.

Look at what one `format=full` call hands your agent: the message's entire MIME tree — the full body, every address on `From`, `To`, `Cc`, and `Reply-To`, and each attachment part with its filename. `threads.get` returns that for every message in the conversation, which is why it costs 40 units instead of 20. Almost none of it is what the agent asked for, and all of it ends up in whatever your model provider logs. Truncate before the prompt, not after.

### 8. Send the reply, and widen the grant to do it

Gmail threads a reply when the `Subject` matches and the `References` and `In-Reply-To` headers follow RFC 2822. Passing `threadId` alone is not enough.

```python
reply = email.message.EmailMessage()
reply["To"] = header(message, "Reply-To") or header(message, "From")
reply["Subject"] = f"Re: {header(message, 'Subject')}"
reply["In-Reply-To"] = header(message, "Message-ID")
reply["References"] = f"{header(message, 'References')} {header(message, 'Message-ID')}".strip()
reply.set_content("Acknowledged. A human is picking this up.")

raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii")
call(token, "POST", "/messages/send", json={"raw": raw, "threadId": message["threadId"]})
```

This call fails on a readonly-only grant, which is the point. Run the agent read-only until you can name every message it would have sent, then re-run consent with `gmail.readonly` plus `gmail.send`. Widening is one more consent screen. Narrowing after an incident is an incident review.

## Decision table

| Option | When it wins | What it costs | What breaks first |
| --- | --- | --- | --- |
| Per-user OAuth (code + PKCE) | The agent acts for a named human who can see and revoke the grant. Default choice. | One refresh token per user per agent to store, rotate, and revoke; a consent screen in your onboarding. | Storage. At 50 users you have a secrets problem you did not plan for. |
| Service account + domain-wide delegation | Workspace-internal, no interactive user, and the mailbox set is fixed — shared mailboxes, archival, compliance export. | Super-admin approval; one credential that can impersonate any user in scope, with no user-visible consent. | Blast radius. The key is a master key over the scopes it was authorized for, and the user has no revoke affordance, because the delegation lives on an admin-console page that requires super-administrator sign-in. |
| Brokered credential layer | Many users, many tools, and an auditor who will ask who read whose mail. | A dependency and a policy model to maintain. | Nothing early; the cost is up front, not in the incident. |
| IMAP or SMTP with an app password | Legacy tooling that cannot speak OAuth. | A password-equivalent secret with no scoping and no per-action audit. | Scope. There is no read-only app password, so this is full access by another name. |

Domain-wide delegation deserves the sharpest warning. It exists so an application can act on Workspace user data without those users consenting, and Google constrains it only by the impersonated user's permissions and the scopes an admin authorized. Reaching for it because per-user consent is inconvenient trades a user-revocable, attributable grant for one only an administrator can withdraw.

Be precise about which half of that is the problem, because the revocation itself is strong. A super administrator deletes the delegation in the admin console, and Google's wording is that applications depending on that client authorization will immediately stop working — sharper than revoking a user grant, where Google warns it can take some time before the revocation has full effect. What the user loses is not the strength of the revocation but any access to it. They never consented, so there is nothing on their account permissions page to withdraw, and the delegation lives on a page that requires super-administrator sign-in. There is no indirect route either: the service account authenticates with a signed JWT assertion rather than a user refresh token, so the user-side levers — a password change, a *Remove access* click — never reach it.

The narrow case where it is right: an unattended Workspace-internal job over mailboxes with no interactive owner — a shared `support@` alias, a compliance archive, an offboarded account — where there is no human to consent in the first place.

## Checklist

- [ ] The scope list in your consent request contains no scope you cannot tie to a specific API call.
- [ ] `https://mail.google.com/` appears nowhere in the codebase, and a grep for it runs in CI.
- [ ] The agent has its own OAuth client, not one shared with a user-facing product.
- [ ] The code exchange verifies the `scope` field of the token response instead of assuming full consent.
- [ ] Refresh tokens are encrypted at rest, keyed by user, and excluded from logs and crash reports.
- [ ] `invalid_grant` triggers a re-consent path, not a retry loop.
- [ ] The OAuth client's publishing status is In production, so refresh tokens do not expire after seven days.
- [ ] Offboarding a user revokes their refresh token via `https://oauth2.googleapis.com/revoke` and deletes it from storage — or, on domain-wide delegation, offboarding is an admin-console change, because there is no user token to revoke.
- [ ] Message bodies are truncated or redacted before they reach a model prompt, and attachment bytes are never fetched unless a step needs them.
- [ ] Retry logic distinguishes a 403 that means throttling from a 403 that means insufficient scope.
- [ ] You can answer, from logs, which agent read which message on whose behalf, and when.

## Failure modes

### The integration works for exactly seven days

**Symptom:** every call succeeds for a week, then every refresh returns HTTP 400 with `{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}` — for every user at once, roughly a week after each consented.

**Cause:** the OAuth client has an external user type and a publishing status of Testing. Google issues seven-day refresh tokens to those clients, and the authorization expires seven days after consent.

**Fix:** move the client to In production, which for restricted scopes means completing verification. There is no configuration flag that extends the seven days. If you are mid-verification, be explicit with pilot users that they will re-consent weekly rather than letting them discover it.

### `invalid_grant` for one user, at 3am, with no deploy

**Symptom:** one user's agent starts failing on refresh while everyone else is fine. Your retry loop hammers the token endpoint and the error never changes.

**Cause:** the refresh token became permanently invalid. Google lists the reasons: the user revoked access, the token went unused for six months, **the user changed their password and the token carries Gmail scopes**, the account exceeded its live-refresh-token limit, or an admin restricted the service. The password-change rule is Gmail-specific and catches people whose other Google integrations keep working.

**Fix:** treat `invalid_grant` as terminal. Delete the stored token, mark the connection as needing re-consent, notify the user, and stop calling. A retry loop against a revoked grant is not just useless, it is the signature of an integration that ignores revocation, which is precisely what a reviewer will look for.

### The user revokes consent halfway through a run

**Symptom:** a run that started cleanly gets a 401 on its fourth or fortieth call, the refresh that follows returns `invalid_grant`, and the agent has already read six messages and sent one reply.

**Cause:** revocation invalidates the tokens themselves, not just your ability to mint new ones, and it cascades — revoking one token revokes its counterpart, so the access token sitting in memory becomes worthless rather than running out its natural lifetime. Google also notes that after a successful revocation it can take some time before the revocation has full effect, which is exactly why this presents as a run that works and then does not: early calls succeed, a later one returns 401, and the refresh behind it returns `invalid_grant`. A revoked grant does not announce itself at the start of a run, and a 401 on its own is indistinguishable from an ordinary expiry until the refresh fails.

**Fix:** make the transition explicit. Refresh once on the first 401, and if that refresh raises `invalid_grant`, stop the run there — do not continue with the messages you already have, do not retry the remaining work, and do not write partial results as though the task completed. Delete the stored token, mark the connection dead, and surface a re-consent prompt to the user. Wrap the whole run in the handler, not just the first call: in `examples/connect-gmail/gmail_agent.py` `main()` wraps the entire `run()` in the `GrantRevoked` handler, because a revocation between the list call and the third `get_message` is the normal case, not the edge case. Anything the agent already did before the revocation still happened and still belongs in your audit trail.

### A 403 you retried forever because it looked like throttling

**Symptom:** the agent loops with exponential backoff against a 403 and never recovers, or it gives up on a genuine rate limit that a short backoff would have cleared.

**Cause:** Gmail returns 403 for several unrelated conditions. `rateLimitExceeded` and `userRateLimitExceeded` are throttling and should be retried with backoff. `domainPolicy` means the Workspace admin disabled Gmail API access and no retry will help. A 403 carrying `"Request had insufficient authentication scopes."` means the token lacks the scope for that call — commonly a `messages.send` on a readonly grant.

One caveat on that last one: Google's error guide documents token problems under 401 `authError` and does not list an insufficient-scope reason under 403, but the Gmail API returns the scope failure as a 403 in practice. Expect the 403, and do not build a scope check that only watches for 401.

**Fix:** branch on the reason inside `error.errors[].reason`, not on the status code. Retry the two rate-limit reasons and 429 and 5xx; surface everything else immediately. The `_is_rate_limited` helper in `examples/connect-gmail/gmail_agent.py` shows the split.

### Search stops working when you narrow the scope

**Symptom:** you downgrade from `gmail.readonly` to `gmail.metadata` to reduce exposure, and `users.messages.list` starts returning an error for a call that worked minutes earlier.

**Cause:** the `q` parameter cannot be used when accessing the API with the `gmail.metadata` scope. Gmail search runs over message content, and that scope has no content access.

**Fix:** filter with `labelIds` instead of `q` — `UNREAD`, `INBOX`, or your own label ids. If your triage genuinely needs search, `gmail.metadata` is not a viable narrowing and `gmail.readonly` is the honest choice.

### Quota disappears into reads, not sends

**Symptom:** the agent gets throttled while doing nothing but polling, long before it sends anything.

**Cause:** Gmail meters quota units, not requests. Against 6,000 units per user per minute and 1,200,000 per project per minute, `messages.list` costs 5, `messages.get` costs 20, `threads.get` costs 40, `messages.send` costs 100, and `history.list` costs 2. A poll loop that lists then fetches 50 messages spends 1,005 units; ten users on a one-minute cadence spend a five-figure unit budget per minute at the project level.

**Fix:** stop polling. Call `users.watch` against a Cloud Pub/Sub topic, then use `history.list` from the returned `historyId` to fetch only what changed. The watch expires after seven days, so renew it daily. Where polling is unavoidable, poll with `history.list` rather than re-listing the inbox.

### Every reply starts a new thread

**Symptom:** the agent's replies appear in the recipient's client as separate conversations, and users report the agent "not answering" when it did.

**Cause:** the message was sent with a `threadId` but without the RFC 2822 threading headers, or with a `Subject` that does not match the original. Gmail groups by the `Subject`, `References`, and `In-Reply-To` headers.

**Fix:** copy `Message-ID` from the original into `In-Reply-To`, append it to the original's `References`, and keep the subject identical apart from a leading `Re:`.

### The hundred-and-first token kills the first

**Symptom:** an account that has been connected for months starts failing with `invalid_grant` even though nobody revoked anything, and the accounts it happens to are the ones that have re-run consent the most.

**Cause:** there is a limit of 100 refresh tokens per Google Account per OAuth client ID, and a new token issued past the limit invalidates the oldest one for that account. This is per account, not per user population, so it bites integrations that re-consent per environment, per replica, or on every deploy — one shared service mailbox can burn the whole allowance on its own.

**Fix:** issue one refresh token per user per client and reuse it. Do not call the consent flow to "make sure" a token exists, and do not let staging and production share a client id.

### The mailbox leaks more than the task needs

**Symptom:** no error at all. A prompt-log review shows third-party addresses, a salary figure, or an attachment filename that nobody intended to send to a model provider.

**Cause:** scope is per-mailbox and relevance is your job. There is no scope that says "only the messages relevant to this task", so a `format=full` read hands over everything step 7 described, and `threads.get` does it for the whole conversation.

**Fix:** decide at the code boundary. Fetch `format=metadata` when headers suffice, extract only the `text/plain` part when you need a body, cap it by length, redact known patterns before the prompt, and never fetch attachment bytes speculatively. The [failure pillar](/fail/) covers how these bodies escape through logs and prompt caches once they are in the pipeline.

## Doing this at scale

The single-user version above is a weekend. The fleet version is the actual work, and it looks like this: one refresh token per user per agent, encrypted at rest under a key you rotate; a rotation schedule that survives Google invalidating tokens on password change; a revocation path wired into offboarding so a departing employee's mailbox stops being readable the same day; and an audit trail that answers "which agent read whose mail, when, and under what authority" without asking anyone to grep application logs. Add the seven-day expiry rule, the 100-token ceiling, the restricted-scope reverification every 12 months, and per-user quota accounting, and the ongoing cost is not the Gmail integration — it is the credential lifecycle around it, multiplied by every other system the agent touches.

That lifecycle is the problem [Agentic Fabriq](https://agenticfabriq.com) is built for. Credentials live in the control layer rather than in the agent; the agent holds a token for the layer and calls a named connection. Policy is evaluated per request, and every call is attributed to an agent and the user it acted for, which is the record an auditor actually wants.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        result = await af.invoke_connection(
            "gmail_work",
            method="get_emails",
            parameters={"max_results": 10, "q": "is:unread"},
        )
        for message in result.get("emails", []):
            print(message)


asyncio.run(main())
```

The runnable version is `examples/connect-gmail/list_unread.py`. The property that matters is not the shorter code — it is that the Gmail refresh token does not live in the agent process, so a compromised agent leaks a revocable gateway token instead of a standing grant on a mailbox. Connection names, method names, and response shapes are all per-deployment, so run `afctl tools list` against your own gateway rather than copying `gmail_work`, `get_emails`, or the `emails` response key on faith. What a layer like [Agentic Fabriq](https://agenticfabriq.com) buys you is the lifecycle, not the API call; everything above stays correct if you would rather own that lifecycle yourself.

## Further reading

Start with the [connect pillar](/connect/) for how this pattern generalizes to Slack, GitHub, and storage — the scope taxonomy differs, but the read-only-first ordering and the refresh-token lifecycle do not. The [govern pillar](/govern/) covers OAuth flows and least-privilege scope design as a policy problem rather than a per-integration decision, and the [fail pillar](/fail/) collects the incident patterns, including how mailbox content escapes through prompt logs.

Primary sources for everything asserted above:

- [Gmail API OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) — the authoritative scope strings and their non-sensitive, sensitive, and restricted classifications.
- [Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification) — verification, the CASA security assessment, annual reverification, and the exemptions.
- [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy) — the Limited Use requirements on restricted scope data.
- [Using OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server) — the authorization and token endpoints, `access_type=offline`, and the requirement to verify granted scopes.
- [OAuth 2.0 for mobile and desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app) — PKCE verifier and challenge rules, loopback redirects, `https://oauth2.googleapis.com/revoke`, the cascade from an access token to its refresh token, and the note that revocation can take some time to take full effect.
- [Using OAuth 2.0 to access Google APIs](https://developers.google.com/identity/protocols/oauth2) — refresh token expiration causes, the seven-day Testing rule, and the 100-token limit.
- [Gmail API usage limits](https://developers.google.com/workspace/gmail/api/reference/quota) — per-minute quota ceilings and the per-method unit costs.
- [Handle Gmail API errors](https://developers.google.com/workspace/gmail/api/guides/handle-errors) — the 403 and 429 reason strings and the backoff guidance.
- [Push notifications](https://developers.google.com/workspace/gmail/api/guides/push) — `users.watch`, Pub/Sub setup, and the seven-day watch expiry.
- [Using OAuth 2.0 for server to server applications](https://developers.google.com/identity/protocols/oauth2/service-account) — domain-wide delegation, its admin authorization steps, and that it reaches user data without requiring the users' consent.
- [Control API access with domain-wide delegation](https://knowledge.workspace.google.com/admin/apps/control-api-access-with-domain-wide-delegation) — that managing a delegation requires super-administrator sign-in, and that deleting one makes dependent applications stop working immediately.
