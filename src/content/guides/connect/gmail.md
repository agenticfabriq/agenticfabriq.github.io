---
title: Connecting an AI Agent to Gmail
description: How to give an AI agent scoped, revocable access to a Gmail mailbox using OAuth, what each scope actually permits, and the failure modes to plan for.
pillar: connect
updated: "2026-08-14"
keywords:
  - ai agent gmail access
  - gmail api oauth scopes
  - agent email integration
relatedGuides:
  - govern/oauth-flows
  - govern/least-privilege
  - fail/over-scoped-oauth
howTo:
  - name: Register the agent in Google Cloud console
    text: Create an OAuth client for the agent so it has its own credentials, separate from any human user's.
  - name: Scope the consent screen to the minimum Gmail permissions
    text: Request only the specific Gmail scopes the agent's task requires, such as gmail.send instead of full mailbox access.
faq:
  - question: Does Gmail access require a Workspace admin?
    answer: Only domain-wide delegation across a Workspace organization requires admin approval; individual OAuth consent granted by a single user does not.
  - question: Can the agent send email without being able to read the mailbox?
    answer: Yes. The gmail.send scope allows sending mail on the user's behalf without granting any read access to existing messages.
---

## TL;DR

Placeholder replaced in Task 9.
