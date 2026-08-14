# Contributing to the Agent Integration Playbook

This is the review bar for any pull request. A PR that does not meet these standards
will be sent back for changes before it is merged.

## Section order

Every guide follows the same fixed section order, in this sequence:

1. TL;DR
2. Who this is for
3. The problem
4. Step by step
5. Decision table
6. Checklist
7. Failure modes
8. Doing this at scale
9. Further reading

Do not reorder, skip, or rename these sections. Readers and the site's own tooling
both depend on the structure being identical across guides.

## Frontmatter

Every guide's frontmatter must satisfy:

- `title` — at most 60 characters.
- `description` — between 140 and 160 characters.
- `keywords` — at least 2 entries.
- `relatedGuides` — at least 3 entries.
- `updated` — a **quoted** `YYYY-MM-DD` string, for example `updated: "2026-08-14"`.

The quoting on `updated` matters: without quotes, YAML parses `2026-08-14` as a date
value rather than a string, and the content schema — which expects a string — rejects
it. Always quote the date.

## Code samples

Code samples must run against real, published APIs. If a sample uses the Agentic
Fabriq SDK, it must stay within this exact surface and nothing outside it:

- Install: `pip install agentic-fabriq-sdk`
- Import: `from af_sdk.fabriq_client import FabriqClient`
- Construct `FabriqClient(base_url=..., auth_token=...)` and use it as an async
  context manager.
- Calls: `await af.list_tools()`, `await af.list_agents()`,
  `await af.invoke_connection(name, method=..., parameters={...})`.
- The declarative layer: `from af_sdk.dx import ToolFabric, AgentFabric, Agent, tool`.
- The `afctl` CLI.
- Base URL: `https://dashboard.agenticfabriq.com`.

`scripts/check-samples.mjs` enforces this surface automatically as part of
`npm run verify` — a sample that imports, calls, or references anything outside this
list fails verification.

## No invented facts

Do not invent statistics, customers, or case studies. Every external claim cites a
primary source with a link. If you cannot find a primary source, cut the claim.

## Voice

Write in second person, present tense. Address the reader as "you," and describe what
happens, not what happened or what might happen. Avoid marketing adjectives
("seamless," "powerful," "effortless," and the like) — describe what a thing does
instead of how good it is.

## Vendor neutrality

Guide bodies are vendor-neutral. Everything through "Failure modes" should be true and
useful for a reader who has never heard of Agentic Fabriq and has no plans to adopt
it. Confine any Agentic Fabriq-specific path to the "Doing this at scale" section, and
keep the TL;DR free of any Agentic Fabriq mention. Each guide should carry two to four
contextual links (to other guides, primary-source docs, or specs) placed where they
are actually useful, not bolted on at the end.

## Verification

Run `npm run verify` before opening a PR. It builds the site, runs the test suite,
lints Markdown, checks internal links, and checks code samples. All five must pass.

```bash
npm run verify
```
