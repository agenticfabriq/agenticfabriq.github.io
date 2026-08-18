---
title: Giving an AI Agent Access to GitHub
description: How to give an AI agent scoped, short-lived access to GitHub with a GitHub App installation token, what each permission grants, and why a classic PAT fails.
pillar: connect
updated: "2026-08-18"
keywords:
  - ai agent github access
  - github app vs pat
  - fine grained personal access token
  - github app installation token
  - github app permissions
  - github api rate limits
relatedGuides:
  - connect/slack
  - govern/credentials
  - fail/over-scoped-oauth
howTo:
  - name: Register a GitHub App for this agent alone
    text: Create the app under a developer account you control, restrict where it can be installed, and generate a private key you store in a vault rather than in the repository.
  - name: Choose the smallest permission set the agent's calls require
    text: Start at metadata read only and add a permission when a real 403 names it in the X-Accepted-GitHub-Permissions response header.
  - name: Install the app on select repositories, not on every repository
    text: Choose "Only select repositories" at install time so the installation itself cannot reach anything the agent was never meant to see.
  - name: Sign an RS256 JWT with the app's private key
    text: Set iat 60 seconds in the past, exp no more than 10 minutes in the future, and iss to the app's client ID, then sign with RS256.
  - name: Exchange the JWT for an installation access token
    text: POST to /app/installations/INSTALLATION_ID/access_tokens with the JWT as a bearer token, narrowing the result with the repositories and permissions body parameters.
  - name: Open a pull request instead of pushing to the default branch
    text: Create a branch, commit to it, and open a pull request so a ruleset requiring review is the gate on every change the agent proposes.
  - name: Revoke the installation token when the run ends
    text: Call DELETE /installation/token so the credential dies with the run rather than staying valid for the remainder of its hour.
faq:
  - question: Should an AI agent use a personal access token or a GitHub App?
    answer: A GitHub App installation access token. It expires after one hour, is scoped to an installation rather than to a person, and can be narrowed to specific repositories and a subset of permissions when it is minted. A personal access token (classic) has none of those properties.
  - question: How long does a GitHub App installation access token last?
    answer: One hour. GitHub mints it from a JSON Web Token signed with the app's private key, and you can end it early by calling DELETE /installation/token, which returns 204.
  - question: Can a GitHub App token be limited to a single repository?
    answer: Yes, at two levels. Install the app with "Only select repositories" so the installation is limited, and pass the repositories body parameter when minting the token to narrow it further. GitHub accepts up to 500 repository names there.
  - question: What GitHub App permissions does an agent need to open a pull request?
    answer: Contents write to create a branch and commit to it, pull requests write to open the pull request, and metadata read. It needs no access to Actions, secrets, or workflow files to do that job.
  - question: Why does my GitHub App get 403 "Resource not accessible by integration"?
    answer: The token lacks a permission the endpoint requires. GitHub returns an X-Accepted-GitHub-Permissions header on that response listing the permissions that would have worked, as a comma-separated set, with semicolons separating alternative sets.
  - question: Can an agent with GitHub API access read GitHub Actions secrets?
    answer: Not directly. The REST endpoint that gets a repository secret returns its name and timestamps without revealing its encrypted value. But an agent with write access to files under .github/workflows can author a workflow, and that workflow runs with whatever secrets it references.
---

## TL;DR

- Default to a GitHub App installation access token. It carries the `ghs_` prefix, expires after one hour, belongs to an installation rather than to a person, and can be narrowed to named repositories and a subset of permissions at the moment it is minted.
- A personal access token (classic) is the wrong shape for an agent. Its `repo` scope grants full access to public and private repositories, there is no read-only scope for private repositories, and organization owners cannot see the token in the first place.
- The mint flow is three claims and one POST: an RS256 JWT with `iat` backdated 60 seconds, `exp` no more than 10 minutes ahead, and `iss` set to the app's client ID, exchanged at `POST /app/installations/INSTALLATION_ID/access_tokens`.
- `workflows` and `secrets` write access are the two permissions that convert a prompt injection into a CI compromise. An agent that opens pull requests and cannot push to the default branch is in a different risk class from one that can.
- The control that holds when the agent is wrong is a ruleset on the default branch requiring a pull request and an approving review — with the agent's app absent from the bypass list.

## Who this is for

You are wiring an agent into repositories other people depend on — triaging issues, proposing fixes, updating documentation, reviewing diffs — and eventually someone will ask which commit the agent made and on whose authority. This guide covers GitHub App installation tokens end to end: registering the app, choosing permissions, signing the JWT, minting and narrowing the token, opening a pull request, and handing the token back. Skip it if the agent runs inside GitHub Actions on the repository it changes, because the automatic `GITHUB_TOKEN` is already scoped to that repository and that job, and your whole problem collapses into writing a `permissions:` block in the workflow file.

## The problem

The first thing that works is a classic personal access token. An engineer generates one from their own account, ticks `repo` because the tutorial said to, pastes it into the agent's environment, and every call succeeds on the first try. Nothing about this looks like a problem for weeks.

What that token actually granted is not "this repository". GitHub's own scope reference describes `repo` as granting full access to public and private repositories, and the token documentation is blunter still: a classic token grants access to all repositories within the organizations you have access to, as well as all personal repositories in your personal account. So the agent can read the private repository belonging to the client engagement the engineer joined last quarter, the personal repository holding their side project, and the infrastructure repository they have access to but have never opened. There is no read-only scope for private repositories — `public_repo` narrows to public ones, and `repo` is all or nothing.

The organization cannot see it. GitHub's credential reference records that for personal access tokens (classic), organization owners lack direct visibility, and can only revoke a token whose value they already know or restrict token access entirely. Compare that to a fine-grained token, where owners can view and revoke individual tokens, or an app installation, which an owner can uninstall.

Nor does it expire on its own. GitHub's organization policy documentation states plainly that personal access tokens (classic) do not have an expiration requirement. GitHub removes classic tokens that have not been used in a year, which is a cleanup mechanism, not a rotation policy — a token the agent uses hourly is never unused.

Then there is attribution, which is the part that turns an incident into an argument. Every call the agent makes is the engineer's call. The commit history says the engineer wrote the change. The audit log says the engineer deleted the branch. Six months later, when a reviewer asks whether a person or a model authored a particular commit, there is no field to look at, because the credential never carried the distinction.

The last piece is capability, and it is the one most teams discover too late. Two permissions do far more than their names suggest. Write access to repository contents on the default branch means the agent's push starts a workflow run without anyone reviewing what it pushed. Write access to files under `.github/workflows` means the agent can author the workflow itself — and while the REST endpoint that reads an Actions secret deliberately returns only the secret's name and timestamps without revealing its encrypted value, a workflow the agent writes runs with every secret that workflow references. The API refuses to hand over the secret; the CI system hands it to a job the agent designed. That gap is where an injected instruction inside an issue comment stops being a curiosity and becomes an exfiltration path, and [the failure pillar](/fail/) collects the shapes it takes.

## Step by step

One path, start to finish: register an app, install it on one repository, mint a token from the private key, read a file, and open a pull request. Every endpoint, header, claim, and permission name below is taken from GitHub's current documentation, linked at the end. Runnable sources: `examples/connect-github/mint_installation_token.py` and `examples/connect-github/open_pull_request.py`.

### 1. Register a GitHub App, not a token on a person

Register the app from developer settings under an account you control. Two choices at registration matter more than the rest.

"Where can this GitHub App be installed?" takes either **Only on this account** or **Any account**. An internal agent takes the first. There is no reason to make an app installable by strangers when its only installation is yours.

Register one app per agent, not one app for the company. Permissions are declared per app, installations are per app, and suspension is per installation. An app shared by four agents holds the union of what four agents need, and suspending it because one agent misbehaved stops all four.

Then generate a private key. The PEM file GitHub gives you is in `PKCS#1 RSAPrivateKey` format, which some JWT libraries will not load without conversion. An app can hold up to 25 private keys, and GitHub's guidance is explicit about why you would want more than one: use multiple keys in order to rotate keys without downtime in the event of a key compromise. GitHub also says not to hard-code the key in your app even if the code is in a private repository, recommends a key vault, and describes an environment variable as the weaker alternative. Treat it accordingly — this key does not expire and it mints tokens for every installation of the app.

### 2. Choose permissions, and know what read buys versus write

GitHub App permissions are per-resource with an access level, not a flat list of scopes. These are the repository permissions an agent is most likely to touch, using the exact keys the API accepts in the `permissions` object:

| Permission | Read gives the agent | Write additionally gives |
| --- | --- | --- |
| `metadata` | General repository information: contributor lists, languages, statistics. | Nothing. Every endpoint GitHub lists under this permission is at read level. |
| `contents` | File contents, commits, branches, releases. Required for HTTP-based Git access with the token. | Creating, updating, and deleting files; creating and deleting branches; cutting releases. |
| `pull_requests` | Pull requests, their reviews, and their comments. | Opening pull requests, commenting, requesting reviewers, submitting reviews. |
| `issues` | Issues and their comments. | Opening, editing, commenting on, and closing issues. |
| `actions` | Workflow runs, artifacts, job logs, execution history. | Cancelling and re-running workflows. |
| `secrets` | Repository Actions secret names and timestamps — never values. | Creating and overwriting secret values, LibSodium-encrypted under the repository public key. |
| `workflows` | — | Creating and updating files in the `.github/workflows` directory. |

Two of these deserve a second look before you tick them.

`secrets` is asymmetric in a way that misleads people in both directions. Read access genuinely cannot exfiltrate a secret: the get-a-secret endpoint returns `name`, `created_at`, and `updated_at`, described in GitHub's reference as retrieving a secret without revealing its encrypted value. Write access is the dangerous half, and not because it lets the agent read anything — it lets the agent replace the value that a deployment job will use. An agent that can overwrite `NPM_TOKEN` controls what gets published under it.

`workflows` has only a write meaning — `write` is the sole value the API accepts for that key, where `contents`, `issues`, `pull_requests`, `actions`, and `secrets` all accept `read` or `write`. It exists because `.github/workflows` is the one directory where a content change is also a change to what runs in CI. An agent with `contents: write` but without `workflows` can edit every file in the repository except those, which is exactly the boundary you want.

You do not have to guess the minimum set. Start the agent with `metadata: read` and nothing else, run its real workload, and read the `X-Accepted-GitHub-Permissions` header off every 403 it produces. GitHub documents that header as a comma-separated list of the permissions required by the endpoint, with semicolons separating alternative sets that would each be sufficient — `pull_requests=read,contents=read; issues=read,contents=read` means either pair works. That gives you a permission list derived from the agent's actual calls rather than from someone's guess about them. Making that derivation a repeatable practice rather than a one-off is a governance problem, and [the govern pillar](/govern/) treats it as one.

### 3. Install on select repositories only

Installing the app is a separate act from registering it, and it is where repository access is decided. Choose **Only select repositories** and name one. An installation scoped to every repository in the organization can never be narrowed below that by anything you do at mint time, because the mint-time filter can only subtract.

The token response tells you which you got. `repository_selection` comes back as `all` or `selected`, and it is worth asserting on in a start-up check: an agent that expects `selected` and receives `all` is an agent whose blast radius silently grew when somebody re-installed the app.

Installation is also the organization's lever. An owner can uninstall the app to deactivate its tokens, and an app owner can suspend an installation with `PUT /app/installations/INSTALLATION_ID/suspended`, authenticated with a JWT, which blocks the app from accessing the GitHub API or webhook events without tearing down the installation's configuration. Suspension is the right button during an incident; uninstalling is the right button after one.

### 4. Sign an RS256 JWT with the app's private key

The JWT authenticates as the app itself. It carries exactly three claims and GitHub constrains all three:

- `alg` is `RS256`.
- `iat` is when the JWT was created. GitHub recommends setting it 60 seconds in the past to protect against clock drift.
- `exp` must be no more than 10 minutes into the future.
- `iss` is your app's identifier. GitHub accepts the app ID and recommends the client ID.

Building it by hand is about fifteen lines and makes every constraint visible:

```python
issued_at = int(time.time())
header = {"alg": "RS256", "typ": "JWT"}
payload = {
    "iat": issued_at - 60,   # backdated for clock drift
    "exp": issued_at + 540,  # 9 minutes, inside GitHub's 10-minute ceiling
    "iss": client_id,        # app client ID; the app ID is also accepted
}

segments = [
    _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
    _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
]
signature = private_key.sign(
    ".".join(segments).encode("ascii"),
    padding.PKCS1v15(),  # RS256 is RSASSA-PKCS1-v1_5 with SHA-256
    hashes.SHA256(),
)
segments.append(_b64url(signature))
app_jwt = ".".join(segments)
```

Note the interaction between the two time claims: backdating `iat` by 60 seconds does not buy you an extra minute of `exp`. The ceiling is measured from now, so `issued_at + 540` leaves a minute of headroom and `issued_at + 600` leaves none.

Send it as `Authorization: Bearer`. GitHub's REST authentication page accepts `Authorization: token` for most credentials but states that if you are passing a JSON web token you must use `Authorization: Bearer`.

Two things this JWT cannot do. It cannot read repository content — it is an app-level credential, and repository access comes from the installation token you are about to mint. And it cannot be used against the GraphQL API at all: GitHub states that the GraphQL API does not support any queries or mutations that require you to authenticate with a JWT.

### 5. Exchange the JWT for an installation access token

You need the installation id. `GET /app/installations` lists every installation of the app, and `GET /repos/OWNER/REPO/installation` returns the one on a specific repository. Both require a JWT. In a webhook-driven agent the id also arrives in the payload, which saves a call.

Then one POST:

```python
response = requests.post(
    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
    headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {app_jwt}",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    json={
        "repositories": ["billing-service"],
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    },
    timeout=30,
)
```

A 201 returns `token`, `expires_at`, `permissions`, and `repository_selection`. The token carries the `ghs_` prefix and expires after one hour.

Both body parameters are narrowing filters, and using them is the difference between a token that matches the installation and a token that matches this run. `repositories` takes repository names — GitHub accepts up to 500 — and `repository_ids` takes numeric ids. `permissions` takes a subset of what the app was granted; it cannot add a permission the app does not hold. If you omit `permissions`, the token gets everything the installation has, which is almost never what a single task needs.

Pin `X-GitHub-Api-Version`. GitHub currently supports `2026-03-10` and `2022-11-28`, and requests without the header default to `2022-11-28`. The examples pin `2022-11-28` deliberately: an explicit version means a future default change is a decision you make rather than a morning you spend debugging.

### 6. Read a file with the installation token

`GET /repos/OWNER/REPO/contents/PATH` returns file content base64-encoded by default. Size decides whether that works: files of 1 MB or smaller support every feature of the endpoint; between 1 MB and 100 MB only the raw and object media types work, and the object media type returns an empty `content` field; over 100 MB the endpoint is not supported at all.

That empty `content` field is the trap, because it decodes to an empty string rather than raising. `read_file` in `examples/connect-github/open_pull_request.py` checks the `encoding` field and refuses instead:

```python
encoding = body.get("encoding")
if encoding != "base64":
    raise ValueError(
        f"{path} came back with encoding {encoding!r}; files over 1 MB must be "
        "fetched with the application/vnd.github.raw+json media type instead"
    )
return base64.b64decode(body["content"]).decode("utf-8"), body["sha"]
```

Keep the `sha` it returns. Updating a file requires the blob sha of the version you are replacing, which is also GitHub's concurrency check: if someone else changed the file between your read and your write, the write fails rather than clobbering them.

### 7. Open a pull request instead of pushing to the default branch

This is the single highest-value design decision on the whole integration, and it costs three calls instead of one.

Create a branch at the current head with `POST /repos/OWNER/REPO/git/refs`, passing a fully qualified `ref` — GitHub rejects a ref that does not start with `refs` and contain at least two slashes — and the `sha` to point it at. Commit with `PUT /repos/OWNER/REPO/contents/PATH`, passing `message`, base64 `content`, the `sha` of the file you read, and `branch`. Then open the pull request:

```python
created = call(
    token, "POST", f"/repos/{repo}/pulls",
    json_body={
        "title": title,
        "head": branch,
        "base": base,
        "body": "Opened by an automation. Nothing merges without a human approval.",
        "draft": True,
        "maintainer_can_modify": True,
    },
)
```

`title`, `head`, and `base` are the parameters that matter; `draft` and `maintainer_can_modify` are optional and both are worth setting. A 422 here means validation failed — most often a head branch with no difference from base, or an open pull request that already exists for that pair.

The reason this matters is not politeness. A pull request produces a diff, a reviewer, a conversation, and a revert button. A push to the default branch produces a deploy. Both are `contents: write`; only one of them is reviewable.

### 8. Make the branch protection real

An agent that opens pull requests is only safer than one that pushes if pushing is actually blocked. Configure a ruleset on the default branch. The rules that carry the weight:

- **Restrict updates** — only actors with bypass permissions can push to matching branches.
- **Require a pull request before merging**, with **Required approvals** set to at least one.
- **Require approval of the most recent reviewable push**, which forces the approval to come from someone other than the person who pushed last.
- **Dismiss stale pull request approvals** when new commits arrive, so an approved diff cannot grow after approval.
- **Block force pushes** and **Restrict deletions**, both of which GitHub enables by default.

Now the part people get wrong. A ruleset's bypass list can contain roles, teams, **and GitHub Apps**. Adding the agent's app to it, which someone will propose the first time a workflow gets stuck, deletes the entire control. If the agent's job genuinely requires bypassing review, that is a decision to argue about explicitly, not a checkbox to tick during an outage.

### 9. Revoke the token when the run ends

`DELETE /installation/token` revokes the installation token you are authenticating with and returns 204. GitHub's own best-practice guidance is to revoke tokens as soon as you no longer need them, and the reason is arithmetic: a run that lasts 90 seconds otherwise leaves a working credential in memory, logs, and any crash dump for the remaining 58 minutes.

Handle the 401 case as success. A token that already expired is a token that is already dead, which is the outcome the call exists to produce.

## Decision table

| Option | When it wins | Attribution | Scope granularity | Expiry and org control |
| --- | --- | --- | --- | --- |
| Personal access token (classic) | Legacy tooling that cannot do anything else. Not a default. | The human who created it. Agent and person are indistinguishable. | Coarse scopes over every repository in every organization the human can reach; no read-only scope for private repositories. | No expiration requirement; owners lack direct visibility into individual tokens. |
| Fine-grained personal access token | A one-off script owned by a named person, in one organization, where GitHub recommends it over the classic token. | Still the human. Better logged, same conflation. | Per-repository selection plus per-resource read/write permissions, limited to a single user or organization. | 1 to 366 days or no expiration, subject to org policy; owners can view and revoke individual tokens and can require approval. |
| GitHub App installation token | Unattended agents. The default for anything running on a schedule or a webhook. | The app, not a person. Distinguishable from human activity in history and logs. | Installation-level repository selection, narrowed further per token by `repositories` and `permissions`. | One hour, automatic; owners can suspend or uninstall the installation. |
| GitHub App user access token | The agent is acting on one human's explicit request and the action should be recorded as theirs. | The user and the app. | Bounded by both the installation's permissions and the user's own access. | 8 hours, with a refresh token valid for 6 months. |

That last row is the honest counterweight to this guide's argument. An installation token attributes activity to the app, which is what you want for a nightly dependency bump and not what you want for "the agent merged this because Priya asked it to". GitHub's best-practice page is unambiguous: if your GitHub App takes an action on behalf of a user, it should always use a user access token instead of an installation access token. If you stay on installation tokens for operational reasons, the mapping from action to requesting human is now yours to record, and nobody will build it for you after the incident.

Note one detail that survives every organization policy choice. GitHub states that regardless of the personal-access-token policy an organization sets, personal access tokens will have access to public resources within the organization. Restricting token access protects private repositories; it does not make the organization opaque.

## Checklist

- [ ] The agent authenticates with a `ghs_` installation access token, and no `ghp_` or `github_pat_` value appears anywhere in its configuration.
- [ ] The app is registered as **Only on this account** unless it is genuinely distributed.
- [ ] The private key is loaded from a vault or an injected secret, never from a file committed to a repository, and the app holds a second key so rotation does not require downtime.
- [ ] The installation is on select repositories, and a start-up check asserts `repository_selection == "selected"`.
- [ ] Every token is minted with an explicit `permissions` object rather than inheriting the installation's full grant.
- [ ] Neither `workflows` nor `secrets` appears in the app's permissions unless a named, reviewed task requires it.
- [ ] The permission list was derived from `X-Accepted-GitHub-Permissions` headers on real 403s, and the derivation is repeatable in CI.
- [ ] A ruleset on the default branch requires a pull request with at least one approval, and the agent's app is not in its bypass list.
- [ ] The agent calls `DELETE /installation/token` in a `finally` block.
- [ ] Retry logic distinguishes a 403 carrying `X-Accepted-GitHub-Permissions` from a 403 carrying rate-limit headers, and honours `retry-after` when present.
- [ ] You can answer, from your own records, which human's request produced any given commit the app authored.

## Failure modes

### 401 "Bad credentials" seconds after minting a perfectly good JWT

**Symptom:** the token exchange returns 401 immediately, with a key you just downloaded and an app you just registered. The same code worked on a colleague's laptop.

**Cause:** almost always one of three things. The machine's clock is fast, so the `iat` GitHub receives is in the future — this is exactly what the recommended 60-second backdate exists to absorb, and skipping it is the most common version of this bug. Or `exp` is more than 10 minutes ahead, which GitHub rejects outright. Or `iss` is not the app's identifier: the client ID and the app ID both work, and the installation id, which is a different number sitting right next to them in the URL bar, does not.

**Fix:** log the three claims before signing, then check them against a trusted clock. If they are right, verify the key belongs to this app by comparing fingerprints — GitHub documents `openssl rsa -in PATH_TO_PEM_FILE -pubout -outform DER | openssl sha256 -binary | openssl base64` for exactly this, and the result should match the fingerprint shown next to the key in the app's settings.

### 403 "Resource not accessible by integration"

**Symptom:** a call that works with your own personal access token fails under the app, with `Resource not accessible by integration` in the body.

**Cause:** the token lacks a permission the endpoint requires. The corresponding message for a fine-grained token is `Resource not accessible by personal access token`. Neither is a rate limit and neither will clear on retry.

**Fix:** read `X-Accepted-GitHub-Permissions` off that response, which lists the permission sets that would have worked. Add the smallest one, and note that adding a permission to an installed app does not apply retroactively — the installation has to accept the new permission before tokens carry it, so the fix involves the organization, not just your code. Branch on the presence of that header rather than on the status code, since 403 also means throttling.

### 404 on a repository you can see in your browser

**Symptom:** the app returns 404 for a repository that plainly exists and that you have open in another tab.

**Cause:** three different conditions collapse into this one status. The installation may not include that repository. The `repositories` array you passed at mint time may not include it. Or the request is not authenticated in a way that reaches the resource — GitHub's rule is that a request to a private resource that is not properly authenticated returns 404 rather than 403, to avoid confirming that a private repository exists. GitHub words that rule around authentication rather than around permissions, so read a 404 as "this credential cannot see it" rather than as a precise diagnosis of why.

**Fix:** call `GET /installation/repositories` with the installation token and compare the list against what you expected. That distinguishes "not in the installation" from "in the installation but the token was narrowed" without guessing. Treat 404 on a repository you believe you have as a permissions bug, not a typo.

### The token expires 60 minutes into a two-hour run

**Symptom:** a long backfill or a large-repository crawl runs cleanly and then every request starts returning 401 at roughly the same moment.

**Cause:** the installation access token expired after one hour, and the process cached it at start-up.

**Fix:** mint on demand rather than at start-up. Keep `expires_at` next to the token, re-mint when it is inside a couple of minutes of expiry, and treat re-minting as ordinary rather than exceptional — the JWT signing is local and the exchange is one request. Do not paper over this by extending the JWT lifetime; the JWT and the installation token are different credentials with different ceilings, and the 10-minute JWT ceiling is not adjustable either.

### A prompt-injected agent rewrites CI

**Symptom:** none at first. Later, a secret appears somewhere it should not, and the commit that made it possible is a small, plausible change to a workflow file, authored by the app.

**Cause:** the app holds `workflows: write`, or holds `contents: write` on a branch that CI runs from, and the agent processed untrusted text — an issue body, a pull request comment, a dependency's README — as if it were instruction. The secret was never read through the API. It was injected into a job the agent authored.

**Fix:** remove `workflows` from the app's permissions and keep it removed. Where CI genuinely must be agent-editable, split it: a separate app with a separate installation, `workflows: write`, no `contents: write` elsewhere, and a ruleset requiring code owner review on `.github/**`. Then keep the two credentials in different processes, because a single process holding both is a single injection away from holding their union.

### Throttled, and the retry logic makes it worse

**Symptom:** the agent's throughput collapses. Some requests return 403, some return 429, and a naive retry loop turns a two-minute pause into a twenty-minute one.

**Cause:** GitHub's primary rate limit for a GitHub App installation is 5,000 requests per hour, or 15,000 if the installation is on a GitHub Enterprise Cloud organization. Installations with more than 20 repositories earn another 50 requests per hour per repository, and installations with more than 20 users another 50 per user, up to a documented ceiling of 12,500. Separately, secondary rate limits cap concurrency at 100 requests and content-generating requests at 80 per minute — and an agent opening pull requests in a loop is generating content.

**Fix:** honour the signals in GitHub's documented order. If `retry-after` is present, wait that many seconds. Otherwise, if `x-ratelimit-remaining` is `0`, wait until `x-ratelimit-reset`, which is UTC epoch seconds. Otherwise wait at least a minute, and back off exponentially for secondary limits. `_sleep_for` in `examples/connect-github/open_pull_request.py` implements exactly that order. The structural fix is to stop polling: GitHub's guidance is to subscribe to webhook events instead of polling the API for data.

### Every commit says the app's name and nobody knows who asked

**Symptom:** an auditor asks who requested a change. Git blame names the app. The GitHub audit log names the app. Nobody can get further.

**Cause:** an installation access token attributes activity to the app by design — that is the property that makes it better than a token on a person, and it is also the property that erases the requester. GitHub is explicit that installation tokens are useful for automations that act independently of users, and that user access tokens are the mechanism for attributing activity to a user.

**Fix:** either move to user access tokens for user-initiated actions, or carry the requester yourself: put the requesting identity in the pull request body and commit trailer, and log the request id alongside the installation id in your own audit store. Do it at the moment of the request. Reconstructing it later from chat history is the work nobody has time for during an incident.

## Doing this at scale

The single-repository version above takes an afternoon. What it turns into is not more of the same, and the difference is worth naming before you commit to owning it.

The private key is the first thing that changes character. One app with one key is a secret in a vault. A dozen agents, each with its own app so that suspension is per-agent, is a dozen non-expiring keys that each mint hourly tokens for every installation of their app — and the two rotation events, planned and post-compromise, both require the second key to already exist. GitHub gives you 25 slots per app precisely so rotation is not an outage, but scheduling it, verifying the new key mints successfully before deleting the old one, and doing that across a fleet is a job somebody has to hold.

The permission set is the second. Every new repository that joins an installation widens what the installation reaches, so a token minted without an explicit `permissions` object drifts toward maximum privilege without anyone changing a line of code. Per-run narrowing has to be the default, and something has to check that it stayed the default.

The audit trail is the third, and it is the one that fails quietly. Installation tokens tell you the app acted. Your logs have to supply the rest — which agent, on whose request, against which repository, under which permission set — and they have to supply it in a form an auditor can query without reading application logs. Every one of these problems has an obvious solution at one repository and no obvious solution at eighty.

That lifecycle is what [Agentic Fabriq](https://agenticfabriq.com) exists to hold. Credentials live in the control layer rather than in the agent: the agent holds a token for the layer and calls a named connection, policy is evaluated per request, and each call is attributed to an agent and to the user it acted for.

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
            "github_platform",
            method="list_pull_requests",
            parameters={"repository": "acme/billing-service", "state": "open"},
        )
        for pull_request in result.get("pull_requests", []):
            print(pull_request)


asyncio.run(main())
```

The runnable version is `examples/connect-github/list_open_pull_requests.py`. The property that matters is not the shorter code — it is that the GitHub App private key never enters the agent process, so a compromised agent leaks a revocable gateway token rather than the key that mints tokens for every repository the app is installed on. Connection and method names are per-deployment, so run `afctl tools list` against your own gateway instead of trusting `github_platform` and `list_pull_requests` here. What a layer like [Agentic Fabriq](https://agenticfabriq.com) sells is that lifecycle, not the API call; everything above this section remains correct if you would rather own the lifecycle yourself.

## Further reading

Start with [the connect pillar](/connect/) for how this pattern generalizes. The vocabulary changes per provider — GitHub calls them permissions, Google calls them scopes — but the ordering does not: a credential that belongs to a system rather than a person, narrowed at mint time, short-lived, and revocable by the organization. [Connecting an AI agent to Gmail](/connect/gmail/) is the same argument in OAuth's terms, and the contrast is instructive: Gmail's refresh token is the long-lived object you must protect, while GitHub's equivalent is the app private key, and the access tokens on both sides are cheap. [The govern pillar](/govern/) covers making permission derivation a policy rather than a per-integration habit, and [the fail pillar](/fail/) collects what happens when it is neither.

Primary sources for everything asserted above:

- [Generating a JSON Web Token (JWT) for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app) — RS256, the `iat` backdate recommendation, the 10-minute `exp` ceiling, and `iss`.
- [Generating an installation access token for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app) — the exchange request and the one-hour expiry.
- [Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation) — the `repositories` and `permissions` narrowing parameters and the 500-repository limit.
- [REST API endpoints for GitHub Apps](https://docs.github.com/en/rest/apps/apps) — the token endpoint's body parameters, response fields, status codes, and the suspend endpoint.
- [REST API endpoints for installations](https://docs.github.com/en/rest/apps/installations) — `DELETE /installation/token` and `GET /installation/repositories`.
- [Managing private keys for GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps) — the PKCS#1 format, the 25-key limit, the rotation guidance, and the fingerprint command.
- [Permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps) — which endpoints each permission gates, at which access level.
- [Editing a GitHub App's permissions](https://docs.github.com/en/apps/maintaining-github-apps/editing-a-github-apps-permissions) — updated permissions do not take effect on an installation until that account approves them.
- [Best practices for creating a GitHub App](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app) — minimum permissions, revoking tokens early, webhooks over polling, and installation versus user access tokens.
- [GitHub credential types](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/github-credential-types) — the `ghp_`, `github_pat_`, `gho_`, `ghu_`, `ghs_`, and `ghr_` prefixes with each credential's lifetime and organization control.
- [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) — what a classic token reaches, and GitHub's recommendation of fine-grained tokens over it.
- [Setting a personal access token policy for your organization](https://docs.github.com/en/organizations/managing-programmatic-access-to-your-organization/setting-a-personal-access-token-policy-for-your-organization) — approval requirements, maximum lifetime policy, and the public-resources caveat.
- [Scopes for OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) — the `repo`, `public_repo`, and `workflow` scope definitions.
- [Troubleshooting the REST API](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api) — `Resource not accessible by integration`, the `X-Accepted-GitHub-Permissions` format, the 404-for-403 behaviour, and the retry ordering.
- [Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) — installation limits, the per-repository and per-user scaling, and the secondary limits.
- [API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions) — the supported `X-GitHub-Api-Version` values and the default.
- [REST API endpoints for repository contents](https://docs.github.com/en/rest/repos/contents) — base64 encoding, the 1 MB and 100 MB thresholds, and the media types.
- [REST API endpoints for Git references](https://docs.github.com/en/rest/git/refs) — the fully qualified `ref` requirement.
- [REST API endpoints for pull requests](https://docs.github.com/en/rest/pulls/pulls) — the create-a-pull-request body parameters and the 422 response.
- [REST API endpoints for GitHub Actions secrets](https://docs.github.com/en/rest/actions/secrets) — what the get-a-secret response does and does not contain, and the LibSodium requirement.
- [Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) — the rule names quoted above and the fact that bypass lists can include GitHub Apps.
