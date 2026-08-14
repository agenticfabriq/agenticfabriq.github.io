# Agent Integration Playbook

An open reference for connecting AI agents to real systems — Gmail, Slack, GitHub,
storage, databases, MCP servers — without leaking credentials or granting more access
than the job needs.

**Read it at [agenticfabriq.github.io](https://agenticfabriq.github.io).**

Prose is CC BY 4.0. Code is Apache-2.0. Maintained by
[Agentic Fabriq](https://agenticfabriq.com).

## Connect

Giving an agent real access to the systems your team already uses.

- [How to Give an AI Agent Access to Your Tools](https://agenticfabriq.github.io/connect/)
  (cornerstone) — A complete guide to granting AI agents access to real systems,
  covering identity, OAuth, scope design, credential storage, and the checks worth
  running first.
- [Connecting an AI Agent to Gmail](https://agenticfabriq.github.io/connect/gmail/) —
  How to give an AI agent scoped, revocable access to a Gmail mailbox using OAuth,
  what each scope actually permits, and the failure modes to plan for.

## Govern

Identity, permissions, credentials, and audit.

- [Agent Identity and Permissions](https://agenticfabriq.github.io/govern/)
  (cornerstone) — How to give AI agents their identity, decide what they may do on a
  user's behalf, and keep every action attributable, revocable, and auditable in
  production.

## Fail

What breaks in production and how to see it coming.

- [How Agent Integrations Fail in Production](https://agenticfabriq.github.io/fail/)
  (cornerstone) — The failure modes show up once agents touch real systems:
  over-scoped tokens, leaked credentials, runaway loops, rate-limit blowups, and
  missing audit trails.

More guides ship over time; this list is updated with each one.

## Contributing

Corrections and new guides are welcome. Read `CONTRIBUTING.md` for the content
standards every guide is held to.

## Local development

```bash
npm install
npm run dev      # local server
npm run verify   # build, tests, lint, link check, sample check
```
