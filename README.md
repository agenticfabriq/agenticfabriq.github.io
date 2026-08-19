# Agent Integration Playbook

A free, vendor-neutral reference on giving AI agents access to real systems — Gmail,
Slack, GitHub, Google Drive, Notion, PostgreSQL, MCP servers — without leaking
credentials or granting more than the job needs. Eighteen guides, no gate, no signup.

**Read it at [agenticfabriq.github.io](https://agenticfabriq.github.io).**

It is written for engineers shipping agents into production at companies that have to
answer who did what, on whose behalf, and with what permission. Each guide is one path
start to finish: what breaks without the control, runnable code, a decision table of the
options genuinely on the table, a checklist you can paste into a pull request, and the
failure modes with the symptom first. External claims link the vendor's own page, an RFC,
or a standards document rather than a secondary source that restates them.

The library splits three ways, by when you need the answer rather than by topic.
**Connect** is before the grant exists. **Govern** is the policy layer that stops you
re-deciding the same thing per provider. **Fail** is after it is live and something is
wrong. Each pillar has a cornerstone guide carrying the whole argument; the topic guides
underneath it are reference.

Prose is CC BY 4.0. Code is Apache-2.0. Maintained by
[Agentic Fabriq](https://agenticfabriq.com).

## Connect

Giving an agent real access to the systems your team already uses: which permission it
needs against this provider, how it gets one, and what the provider hands over that you
did not ask for.

- [AI Agent Access: Service Account vs Per-User OAuth](https://agenticfabriq.github.io/connect/) (cornerstone)
  — Four ways an agent gets access to a system — shared key, per-user OAuth, service
  identity, brokered — compared on attribution, blast radius, and cost.
- [Connect an AI Agent to Gmail: OAuth Scopes That Work](https://agenticfabriq.github.io/connect/gmail/)
  — Pick the narrowest Gmail scope, run the PKCE consent flow, refresh tokens correctly, and
  avoid the seven-day expiry that breaks Testing-status apps.
- [Slack Bot Scopes and Security for an AI Agent](https://agenticfabriq.github.io/connect/slack/)
  — Which Slack bot scopes an agent needs, how to verify the request signature, Socket Mode
  versus a request URL, and the rate limits that throttle reads.
- [Give an AI Agent GitHub Access with a GitHub App](https://agenticfabriq.github.io/connect/github/)
  — Use a GitHub App installation token instead of a PAT: sign the JWT, scope it to named
  repositories, open a pull request, and revoke it when the run ends.
- [Give an AI Agent Safe Access to Your Postgres Database](https://agenticfabriq.github.io/connect/databases/)
  — Give an agent a dedicated Postgres role, row-level security keyed to the acting user,
  statement timeouts, and parameterized tools instead of raw SQL.
- [AI Agent Access to Google Drive and Notion: Scopes First](https://agenticfabriq.github.io/connect/drive-and-notion/)
  — Pick drive.file over the restricted Drive scopes, put the Picker in the grant loop, and
  understand why one Notion share hands over a whole page subtree.
- [Securing an MCP Server on the 2026-07-28 Spec](https://agenticfabriq.github.io/connect/mcp-servers/)
  — What the 2026-07-28 revision changed, where authentication really sits, how to authorize
  per tool and per argument, and the confused deputy problem.
- [Per-User Credentials in LangChain, CrewAI, Agents SDK](https://agenticfabriq.github.io/connect/frameworks/)
  — One tool built three times in LangChain, CrewAI, and the OpenAI Agents SDK, with the
  per-user credential resolved at call time rather than at import.

## Govern

Identity, permissions, credentials, and audit — the decisions you make once and apply
everywhere, rather than re-arguing them per provider.

- [Agent Identity and Permissions](https://agenticfabriq.github.io/govern/)
  (cornerstone) — An agent is neither a person nor a service, and identity systems were
  built for those two. How to give one a provable identity, decide per call, and revoke it.
- [OAuth Flows for AI Agents](https://agenticfabriq.github.io/govern/oauth-flows/) —
  The three OAuth grants an AI agent actually needs, why RFC 8693 token exchange is the
  one most teams skip, and how to scope a downstream token down.
- [Delegated User Identity for Agents](https://agenticfabriq.github.io/govern/delegated-identity/)
  — Acting for a user is not acting as one. How to intersect agent and user permissions,
  exchange a token on behalf of a caller, and audit both principals.
- [Designing Least-Privilege Scopes for Agents](https://agenticfabriq.github.io/govern/least-privilege/)
  — A repeatable method for scoping an AI agent: enumerate the actions from its tool
  definitions, map each to the narrowest scope, remove the rest, and ratchet.
- [Credential Vaulting and Rotation for Agents](https://agenticfabriq.github.io/govern/credentials/)
  — Why an environment variable cannot express per-user access, how to issue short-lived
  credentials per request, and how to rotate and revoke without downtime.
- [Audit Trails and Compliance for Agent Actions](https://agenticfabriq.github.io/govern/audit/)
  — What belongs in an AI agent audit log, field by field: both principals, the policy
  decision, immutability, redaction, retention, and the query you will run.

## Fail

What breaks in production, read backwards from the symptom: how to see it coming, how to
tell which failure you are looking at, and what contains it.

- [How Agent Integrations Fail in Production](https://agenticfabriq.github.io/fail/)
  (cornerstone) — The six classes of agent failure: permissions, credentials, control
  flow, trust boundaries, attribution, lifecycle. The symptom, the detection, the fix.
- [Over-Scoped OAuth](https://agenticfabriq.github.io/fail/over-scoped-oauth/) —
  An over-scoped agent throws no errors. How the broad scope got granted, why nobody ever
  narrows it, what it reaches, and a scope audit you can run this week.
- [Token Leakage Through Agent Context](https://agenticfabriq.github.io/fail/token-leakage/)
  — How a credential in an agent context window escapes into traces, logs, model output,
  and injected requests, and how a broker closes the whole class of leak.
- [Runaway Loops and Rate-Limit Blowups](https://agenticfabriq.github.io/fail/runaway-loops/)
  — How agent loops run away, why retries multiply across layers, and the run budgets,
  jittered backoff, circuit breakers, and idempotency keys that stop them.

## Runnable examples

Every non-trivial code path has a runnable file under `examples/`, referenced from its
guide by path. They are checked on every commit: they compile, their imports resolve, and
anything touching the Agentic Fabriq SDK is validated against the published surface, so a
sample cannot drift into calling something that does not exist.

## Contributing

Corrections are the most useful contribution. If a scope name changed, a quota moved, or a
guide states more than its cited page states, open an issue or a pull request. Read
`CONTRIBUTING.md` for the content standards every guide is held to.

## Local development

```bash
npm install
npm run dev      # local server
npm run verify   # build, tests, lint, link check, sample check
```
