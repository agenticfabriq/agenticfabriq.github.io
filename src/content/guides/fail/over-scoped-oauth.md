---
title: Over-Scoped OAuth
description: An over-scoped agent throws no errors. How the broad scope got granted, why nobody ever narrows it, what it reaches, and a scope audit you can run this week.
pillar: fail
updated: "2026-08-18"
keywords:
  - over-scoped oauth agent
  - oauth scope blast radius
  - agent permission audit
  - unused oauth scopes
  - oauth re-consent rollout
  - scope drift detection
relatedGuides:
  - govern/least-privilege
  - govern/oauth-flows
  - connect/gmail
howTo:
  - name: Freeze one integration and write down what it is supposed to do
    text: Pick a single agent and a single provider, and write the one-sentence job description that the scope list is supposed to implement. Everything downstream is a diff against that sentence.
  - name: Enumerate the live grants
    text: List the grants that exist right now rather than the scopes in your source code. On Google Workspace that is GET /admin/directory/v1/users/{userKey}/tokens, whose Token resource carries clientId and a scopes array.
  - name: Pull the scopes the agent actually exercised
    text: Query the OAuth token audit log with applicationName=token and eventName=activity to get the api_name and method_name of every call the client made, then map those methods to the scopes that gate them.
  - name: Produce the unexercised set
    text: Subtract the scopes implied by the exercised methods from the granted scopes. What remains is standing authority that no code path has used, and it is the working definition of over-scoped.
  - name: Narrow the grant and record the reason
    text: Revoke the old grant and re-consent to the narrow set inside one user-initiated interstitial, because Google revocation clears every scope the user granted the whole Cloud project. Verify the scope field of the token response, store it, and write the decision down with the evidence window that justified it.
  - name: Alert on the next over-granted scope at grant time
    text: Watch the authorize event in the token audit log and page when a new grant carries a scope that is not on the declared allowlist for that client, rather than waiting for an access review.
faq:
  - question: What does an over-scoped OAuth agent look like before anything goes wrong?
    answer: It looks like nothing. The tell is administrative, not operational - an access review that answers "full access" instead of naming a resource set, a consent screen with more lines than the agent has features, and an incident response that cannot state an upper bound on what the agent could reach. Over-scoped integrations do not throw errors; they succeed at more than they were asked to do.
  - question: Why are OAuth scopes almost never narrowed after they are granted?
    answer: Because narrowing is a rollout, not a code change. On most providers a granted scope cannot be removed from an existing token - Slack states plainly that "there is no way to remove scopes from an existing token without revoking it entirely" - so narrowing means revoking and re-consenting every affected user. That needs a prompt, a support plan, and an owner. The engineer who noticed the excess scope owns the code, not the users, so the ticket has no home and does not move.
  - question: What order should I use when narrowing a Google OAuth grant?
    answer: Revoke first, then re-consent, both inside one user-initiated interaction. Google states that revocation "removes all OAuth 2.0 scopes previously granted to a project, invalidating any issued access or refresh tokens for all clients registered under that project". The grant is held per project per user, not per token, so the intuitive order - acquire the narrow grant, then revoke the broad one - destroys the narrow token you just obtained. It also means the revoke clears this user's grant for every other OAuth client under the same Cloud project, so check what else lives there first.
  - question: How do I find out which OAuth scopes my agent actually uses?
    answer: Diff the granted set against the exercised set. Get the granted set from the provider's grant listing, such as the Google Workspace Directory API tokens.list endpoint, whose Token resource has a scopes array. Get the exercised set from an audit log of calls - the Google Workspace OAuth token log records api_name and method_name for every call under eventName=activity - and map each method to the scope that gates it. The scopes left over are unexercised.
  - question: Does an over-scoped agent make prompt injection worse?
    answer: The scope is what converts a successful injection into an incident. OWASP lists "excessive permissions" as a root cause of Excessive Agency, and defines indirect prompt injection as an LLM accepting input from external sources such as websites or files. An agent that both reads untrusted content and holds a broad grant has the attacker's capability and the attacker's reach in one process, so the two risks multiply rather than add.
  - question: Can I narrow a GitHub App's permissions without asking anyone to re-approve?
    answer: Yes. GitHub documents that adding repository or organization permissions requires each installing account to approve, but that "if you remove permissions or webhooks from your GitHub App, the changes will take effect immediately". Removal is unilateral and instant on GitHub, which makes it the cheapest narrowing you will ever do and the one with no excuse for being deferred.
  - question: Is call-time policy enforcement a substitute for narrowing the scope?
    answer: No. A policy layer that refuses calls the token would permit reduces the reachable set in practice, which is real risk reduction and much faster to ship than a re-consent rollout. But the grant still exists, so anything that bypasses the policy layer - a leaked refresh token, a direct API call, a misconfiguration - recovers the full scope. Enforcement buys time to narrow; it does not replace narrowing.
---

## TL;DR

- Over-scoping has no error message. The symptom is administrative: an access review that returns "full access", a consent screen listing capabilities the product does not have, and an incident whose blast radius nobody can bound.
- The broad scope was almost never a decision. The narrow one returned a 403 late in a sprint, the broad one worked, and the pull request that widened it was three characters long.
- Scopes are not narrowed later because narrowing is a rollout, not a diff. Slack states outright that "there is no way to remove scopes from an existing token without revoking it entirely", so narrowing means re-consenting every user, and nobody owns re-consenting users.
- The blast radius is computable today. `https://www.googleapis.com/auth/drive.readonly` is Google's "View and download all your Drive files"; `files.list` defaults to `corpora=user` — "Files owned by or shared to the user" — at up to 1,000 files per page. That is an enumeration, not a search.
- Untrusted input plus a broad grant is not two risks. It is one risk that supplies the capability and another that supplies the reach.
- Narrowing on Google means revoking first: revocation "removes all OAuth 2.0 scopes previously granted to a project", for every client under it, so acquire-then-revoke destroys the grant you just acquired.
- Run the audit: granted scopes from the provider's grant listing, exercised methods from the audit log, subtract, and act on what is left over.

## Who this is for

You maintain an agent that already holds an OAuth grant on somebody's real account, and one of two things has happened. Either someone asked what the agent can reach and you could not answer with a number, or you already know the answer and it is larger than the agent's job. This guide is the diagnostic path: how to tell from outside that a grant is over-scoped, how to compute what it actually reaches, how to narrow it without stranding users mid-flight, and how to catch the next one at grant time rather than at audit time.

Skip it if you are still choosing scopes for an integration that has not shipped. That is a cheaper problem and it is solved in the [connect pillar](/connect/), where each guide picks the scope before the consent screen exists. Come back when the grant is live, because from that point the economics reverse: widening stays a one-line change and narrowing becomes a project.

## The problem

### What it looks like from outside

There is no exception, no log line, and no dashboard that turns red. The failure surfaces in three places, all of them administrative.

The first is the consent screen. It scrolls. A user clicking through an install sees a list of capabilities that has nothing to do with the thing they were told the agent does. Google's `https://www.googleapis.com/auth/drive` renders as "View and manage all your Drive files" for a tool that summarises one document. Its `https://www.googleapis.com/auth/calendar` renders as "See, edit, share, and permanently delete all the calendars you can access using Google Calendar" for a tool whose entire job is booking one meeting — and Google publishes `calendar.app.created`, "Make secondary Google calendars, and see, create, change, and delete events on them", which is the scope that tool wanted. Nobody reads any of it, and this is not a user-education failure. The screen is asking for a decision that the person clicking cannot make, because it lists capabilities rather than the resources those capabilities reach, and the resource set is the only thing they could reason about. A consent screen is a bad control surface, which is exactly why teams stop treating it as one.

The second is the access review. Someone in security asks what a given agent can reach, and the honest answer comes back as a scope string rather than a resource set. "It has `repo`." "It has full mailbox access." Those are answers about capability. The reviewer asked about reach. GitHub's own description of the `repo` scope is "Grants full access to public and private repositories including read and write access to code, commit statuses, repository invitations, collaborators, deployment statuses, and repository webhooks", with a note that it "also grants access to manage organization-owned resources including projects, invitations, team memberships and webhooks". Translating that into "which repositories, in which organisations, belonging to whom" is work that nobody does during a review, so the review records the scope string and moves on. A review that records capability instead of reach has not reviewed anything.

The third is the incident, and it is the one that costs. Something goes wrong — a token in a log, a compromised dependency, a model that followed an instruction it read in a document — and the first question is always the same: what could it have touched? For a narrowly scoped agent that question has an answer with a number in it. For an over-scoped one it does not, and "we cannot bound it" is operationally identical to "assume everything". The cost of the over-scoping is not the breach. It is that the breach cannot be sized, so the response has to be sized for the maximum, which means notifying everyone, rotating everything, and explaining to a customer that you are unable to say whether their data was in scope.

If you want the tell in one sentence: an over-scoped integration is one where the answer to "what can it reach" is a capability rather than a count.

### How the broad scope got granted

Almost nobody chooses a broad scope. It arrives, and the mechanics are worth naming precisely, because a fix aimed at the wrong mechanism does not hold.

The technical half is familiar. A developer wires up the narrow scope, and one call returns a 403. It is late, the error message is generic — GitHub's is "Resource not accessible by integration", Google's is a 403 whose body has to be parsed to distinguish "you lack the scope" from "you are being throttled" — and the fastest way to find out whether the problem is scope at all is to widen the scope and see if it goes away. It goes away. That experiment is now the configuration. This is not carelessness; it is the correct debugging move, and the only mistake is not reverting it.

The organisational half is what makes it permanent, and it has four parts.

**The widening is invisible in review.** The diff is one line in a constants file. It changes a string. Nothing in the pull request states that the change moves the reachable set from one document to a corpus, because the scope string does not say what it reaches — you have to go and read the provider's scope table to find out, and the reviewer has thirty other files to look at. A three-character diff that multiplies blast radius by four figures is the highest-leverage line of code in the change set and the least likely to be discussed.

**The pilot ratifies it.** The pilot runs with five internal users who all trust the tool. Nobody objects to the consent screen because nobody reads consent screens for internal tools, and the absence of objection reads as approval. By the time the first external user sees that screen, the scope list is a fact about the product rather than a proposal.

**Nothing downstream disagrees.** Broad scopes do not degrade anything. There is no latency cost, no quota cost, no error rate, no alert. Every signal an engineering organisation uses to notice a mistake is silent, so the only thing that can surface it is a person deliberately looking, on a schedule, at something that is not broken.

**The deadline is real and the risk is abstract.** The launch date exists on a calendar. The blast radius exists in a hypothetical. When those two compete inside a sprint, the calendar wins every time, and it is not irrational that it wins — the person making the call has been asked to ship, and the risk has not been quantified for them. This is the part that a checklist does not fix. What fixes it is doing the arithmetic in the section below and putting a number in the ticket, because a number competes with a date and an adjective does not.

Note what is absent from all four: ignorance. The engineer who widened the scope usually knew it was wider. They intended to come back.

### Why the scope is never narrowed

This is the centre of the guide, so it is worth being exact rather than rhetorical. Nobody comes back because of a chain of dependencies, and every link in it is real.

**Narrowing requires a new grant.** On most providers you cannot subtract from an existing one. Slack is the clearest on this because it says it twice: "each scope you request is additive to the scopes you've already been awarded. It is not possible to downgrade an access token's scopes", and "there is no way to remove scopes from an existing token without revoking it entirely". Google's model points the same way — `include_granted_scopes=true` means "the new access token will also cover any scopes to which the user previously granted the application access", the merge runs upward, and the documented instrument for removing access is the revocation endpoint at `https://oauth2.googleapis.com/revoke`, which takes a token and not a scope list. There is no partial revoke.

**A new grant requires a user prompt.** Revoking and re-consenting means every affected user sees a consent screen again. Not a notification — an interactive screen, at a moment you do not control, that they have to complete before the agent works for them again.

**A user prompt requires someone to own the rollout.** Somebody has to decide which cohort goes first, write the in-product explanation of why the thing they already approved is asking again, staff the support queue for the people who click Deny, define what the agent does for a user who has not re-consented yet, and set the date after which the old grant is force-revoked. That is a product launch with a comms plan attached, not an engineering ticket.

**Nobody owns it.** The engineer who noticed the excess scope owns the code. They do not own the users, the consent copy, the support queue, or the release calendar. The person who owns those things has a roadmap, and "re-prompt our entire user base to get a permission we already have" ranks below every feature on it, because it produces no new capability, generates support load, and its benefit is a risk reduction that has not been sized. So the ticket is filed, labelled `security`, and enters the backlog at a priority that is honestly assigned and never reached.

That is the whole mechanism. Not ignorance, not negligence — an ownership gap between the person who can see the problem and the people who would have to carry the fix. Every intervention that works attacks one of the four links:

- **Make the grant narrowable without a prompt.** GitHub already did this and it is the single most useful asymmetry in the space: adding permissions means "each account where the app is installed will need to approve the new permissions" and "updated permissions won't take effect on an installation or user authorization until the new permissions are approved", but "if you remove permissions or webhooks from your GitHub App, the changes will take effect immediately". Narrowing a GitHub App is a settings change with no user in the loop. If your over-scoped integration is a GitHub App, the entire chain above does not apply to you and there is no reason it is still over-scoped after today.
- **Attach the narrowing to a re-auth that is already happening.** If the provider or your own rotation schedule already forces users through consent — a migration, a token rotation, an app rename — the prompt is free because it is being paid for anyway.
- **Give the rollout an owner by making it someone's number.** Not a ticket. A metric on a dashboard that a named person reports on.
- **Reduce what the scope reaches without changing the scope.** This is the enforcement path, and it is covered under [doing this at scale](#doing-this-at-scale). It is genuinely useful and it is genuinely not the same thing as narrowing.

### The blast radius, with the arithmetic

"Over-scoped" is an adjective until you turn it into a count. Here is the full working for one scope, using literals from Google's documentation.

An agent prepares meeting briefs. A user points it at the agenda doc for their next meeting; it reads that doc and writes a summary. That is the entire job description.

The scope that implements the job description is `https://www.googleapis.com/auth/drive.file`, which Google describes in full as "Create new Drive files, or modify existing files, that you open with an app or that the user shares with an app while using the Google Picker API or the app's file picker", and classifies as recommended rather than sensitive or restricted. Do not drop that trailing clause: the picker *is* the access mechanism. `drive.file` does not grant a set of files, it grants a way for the user to hand files over one at a time, and an app without a picker has no route to any of them. The reachable set is exactly what the user handed over — for this agent, one document per meeting, call it twenty documents per user over a quarter.

The scope that shipped is `https://www.googleapis.com/auth/drive.readonly`, which Google describes as "View and download all your Drive files" and classifies as restricted. It shipped because the file picker integration was a week of work and `drive.readonly` plus a filename search was an afternoon.

Now compute the reachable set. `files.list` takes a `corpora` parameter that "Specifies a collection of items (files or documents) to which the query applies", supporting `user`, `domain`, `drive`, and `allDrives`. Google states "By default, corpora is set to `user`. However, this can change depending on the filter set through the `q` parameter." The reference page defines `user` as "Files owned by or shared to the user" under the `Corpus` enum — the deprecated `corpus` parameter's type rather than `corpora`'s own prose, though the semantics carry across. So the reachable set is not the user's own files. It is their files *union* every file anyone in the company has ever shared with them.

Take a plausible knowledge worker at a 600-person company: 1,200 files they own, 9,000 files shared to them across four years of project folders, decks, and spreadsheets. Reachable set per user: 10,200 files.

- Ratio against the job description: 10,200 ÷ 20 = **510×**. The agent can reach five hundred times the corpus it was built to read, per user, from the moment they click Allow.
- Sixty users consented during the pilot. The union is not 612,000, because "shared to the user" sets overlap heavily inside one company — that overlap is the point of a shared drive. Dedupe by file id and you land somewhere near a quarter of a million distinct files. That is the number that belongs in the incident report, and the only honest way to get it is to run the enumeration.
- Enumeration cost: `files.list` has a `pageSize` maximum of 1,000, so one user's 10,200 files is eleven paginated requests. Sixty users is 660 requests. Nothing about the blast radius is rate-limited into safety; the whole corpus is minutes away, not months.
- If anyone set `includeItemsFromAllDrives=true` with `corpora=allDrives` — and someone will, because a shared-drive file that does not appear in results looks exactly like a bug — add every shared drive any of those sixty users belongs to, in full.

Two things make this arithmetic worth doing rather than asserting.

First, the number is the argument. "The agent is over-scoped" loses to a launch date. "The agent can enumerate roughly 250,000 documents belonging to 600 people, in 660 API calls, and we cannot currently prove it hasn't" does not lose to a launch date. Same fact, different weight, and the difference is entirely that somebody spent an afternoon counting.

Second, the count is what an incident actually needs. If a token leaks, the question is not whether the agent read those files. It is whether you can prove it did not, and the grant proves nothing in either direction — only a per-object access log covering the whole window does. A narrow scope is worth having precisely because it makes that proof unnecessary: with `drive.file` the reachable set is bounded by construction and there is nothing to reconstruct.

The same shape appears everywhere, with different nouns. On Gmail, `https://mail.google.com/` reaches every message in a mailbox including permanent deletion, where `gmail.send` reaches nothing that already exists — [the Gmail guide](/connect/gmail/) works that table in full. On GitHub, `repo` on a classic personal access token reaches every private repository its human owner can see, in every organisation, for as long as the token exists — GitHub notes that scopes "limit access for OAuth tokens" but "do not grant any additional permission beyond that which the user already has", which means the ceiling is the human's entitlements and those grow over time. A GitHub App installed on selected repositories reaches a list you can print. On Slack, the reach is scope multiplied by channel membership, and only the first half is visible to the admin who approved the install, which [the Slack guide](/connect/slack/) covers.

### The prompt-injection multiplier

Two properties are commonly tracked as separate risks on separate rows of a register: the agent reads content from sources you do not control, and the agent holds a broad grant. They are not separate. One is the exploit and the other is the payoff, and a risk register that scores them independently is understating the product of the two.

OWASP's definition of the first half is precise: indirect prompt injections "occur when an LLM accepts input from external sources, such as websites or files", where content inside that external source alters the model's behaviour when the model interprets it. Any agent that summarises a document somebody else wrote, triages an inbound email, reads an issue comment, or fetches a URL is inside that definition. Not as an edge case — as its normal operating mode.

OWASP names the second half too. Excessive Agency's root causes are given as "excessive functionality; excessive permissions; excessive autonomy", and the worked example of excessive permissions is "an extension intended to read data connects to a database server using an identity that not only has SELECT permissions, but also UPDATE, INSERT and DELETE permissions". Swap the database identity for an OAuth grant and it is the same sentence.

Simon Willison's framing of the combination is the most useful operational version, because it is a checklist of three: access to private data, exposure to untrusted content, and the ability to communicate externally. His claim about the combination is blunt — if an agent has all three, "an attacker can easily trick it into accessing your private data and sending it to that attacker".

The scope is the first leg. That is why this belongs in a guide about over-scoping rather than in a guide about prompt injection. An injection against a `drive.file` agent can instruct it to exfiltrate exactly the document the user already handed it, which the user was going to see anyway. The identical injection against a `drive.readonly` agent can instruct it to search 10,200 files for the string "password" and put the results in its summary. Same model, same attack, same prompt — the difference in outcome is entirely the scope string, which is another way of saying the scope is the variable that turns a model behaviour into a security incident.

The practical consequence is an ordering. Prompt-injection defences are probabilistic and always will be, because the model cannot reliably tell instructions from data. Scope narrowing is deterministic: a call the token cannot make does not happen regardless of what the model was persuaded to attempt. Spend on the deterministic control first. It is the one that still holds after the probabilistic one fails.

## Step by step

What follows is one audit, on one integration, that fits in a week. It is written against Google Workspace because Google exposes both halves of the diff through documented APIs, but the shape transfers: every provider has a way to list live grants and a way to see what was called, and the interesting work is the join between them. Runnable sources are `examples/fail-over-scoped-oauth/scope_audit.py` (the diff, with a `--demo` mode that runs with no credentials), `examples/fail-over-scoped-oauth/narrow_google_scope.py` (the re-consent and revoke, in the order that does not strand users), and `examples/fail-over-scoped-oauth/enforce_at_call_time.py` (the mitigation).

### 1. Freeze one integration and write down what it is supposed to do

One agent, one provider. Write the job description in one sentence, in terms of resources rather than capabilities: "reads the single document a user selects for the meeting they are about to attend". Not "reads Drive".

This sentence is the specification the rest of the audit diffs against, and writing it first matters because it is the only step that is hard to do honestly afterwards. Once you have seen the granted scope list, the job description drifts to accommodate it — you will find yourself writing "reads documents relevant to the user's meetings", which is vague enough to justify anything. Write it before you look.

### 2. Enumerate the live grants

Read the grants that exist, not the scopes in your source tree. They diverge, and the divergence is the interesting part: source shows what you ask for now, the grant records everything anyone has ever granted, including scopes from a version you shipped in March and reverted in April. On Slack that divergence is guaranteed rather than possible, since every reinstall adds to the set.

On Google Workspace, the Directory API lists a user's third-party grants:

```text
GET https://admin.googleapis.com/admin/directory/v1/users/{userKey}/tokens
Authorization: Bearer <token with https://www.googleapis.com/auth/admin.directory.user.security>
```

The Token resource carries `clientId`, `displayText`, `nativeApp`, `anonymous`, `userKey`, and `scopes`, documented as "a list of authorization scopes the application is granted". `scopes` is your granted set, per user, per client id. Run it across the users who consented and take the union — you are auditing a client, not a person, and the client's authority is the union of what everyone gave it.

Two other providers, for orientation. Slack returns an `x-oauth-scopes` header on every Web API response listing the scopes the calling token currently holds, so a single `auth.test` tells you the live set without an admin API. GitHub returns `X-OAuth-Scopes`, which "lists the scopes your token has authorized", alongside `X-Accepted-OAuth-Scopes`, which "lists the scopes that the action checks for" — the second header is the one that makes step 3 cheap, because the provider is telling you the requirement instead of making you derive it.

### 3. Pull the scopes the agent actually exercised

This is the half that teams skip, and it is the half that turns an opinion into a finding.

Google Workspace records OAuth activity in an audit log you query through the Reports API:

```text
GET https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/token
    ?eventName=activity&maxResults=1000
Authorization: Bearer <token with https://www.googleapis.com/auth/admin.reports.audit.readonly>
```

Now the caveat that determines how the rest of this works, because getting it wrong produces an audit that quietly compares nothing to nothing. **The `activity` event does not carry a `scope` parameter.** Its documented parameters are `api_name`, `app_name`, `client_id`, `client_type`, `method_name`, `num_response_bytes`, and `product_bucket` — Google renders it in the admin console as "{app_name} called {method_name} on behalf of {actor}". The events that do carry `scope` are `authorize`, `deny`, `request`, and `revoke`, and those describe grants, not calls: `authorize` renders as "{actor} authorized access to {app_name} for {scope} scopes".

So the log tells you which methods were called and which scopes were granted, and the join between them is yours to write. That mapping is a table you maintain from the provider's own per-method documentation, where each method reference lists the scopes it accepts:

| Observed call | Scope that gates it | Source |
| --- | --- | --- |
| Drive `files.list`, `files.get` (metadata only) | `drive.metadata.readonly` suffices | Drive method reference lists accepted scopes per method |
| Drive `files.get` with `alt=media` | `drive.readonly` or narrower per-file `drive.file` | Downloading content is not a metadata operation |
| Gmail `users.messages.list`, `users.messages.get` | `gmail.readonly` | Gmail scope reference |
| Gmail `users.messages.send` | `gmail.send` | Send is write-only and does not imply read |

Two honest notes on this step. First, Google documents that `method_name` exists and is a string but does not document its emitted format — whether it appears as `gmail.users.messages.list`, `messages.list`, or something else — so build the left column from your own log output rather than from this table, which is illustrative. Second, copy each method's accepted-scope list whole rather than curating it down to the one you expect. A scope you leave out is a scope the audit reports as unexercised, which means recommending the deletion of something the observed calls actually require — a confident wrong answer, which is worse than no answer. `scope_audit.py` carries one derived row rather than a copied one, for `files.get` with `alt=media`, because Google publishes a single list for `files.get` and does not split it by `alt`; that row is flagged as derived in the file.

Set the window carefully. Google retains OAuth log events for six months, so the widest defensible window is 180 days; the documented lag on token log events is "a couple of hours", so exclude the most recent day or you will score a scope as unexercised because its call has not landed yet. Pick a window that contains at least one of every periodic thing the agent does — one month-end close, one quarterly report — because a scope used once a quarter looks identical to a scope used never inside a 30-day window, and deleting it is how you find out.

### 4. Produce the unexercised set

Subtract. Granted scopes, minus the scopes implied by exercised methods, equals standing authority that no code path has used in your evidence window. That is the working definition of over-scoped, and it is now a list rather than an adjective.

```python
def required_scopes(exercised_methods: Iterable[str]) -> set[str]:
    """Narrowest scope set that would have satisfied every observed call."""
    methods = set(exercised_methods)
    unmapped = sorted(method for method in methods if method not in METHOD_GATES)
    if unmapped:
        # An unmapped method could require any scope, including the one we are
        # about to recommend deleting. Refuse rather than under-report.
        raise AuditInconclusive(f"no METHOD_GATES entry for {method}" for method in unmapped)
    return {METHOD_GATES[method] for method in methods}
```

The `raise` is the load-bearing line. A method your table does not know about might be the only user of the scope you are about to delete, so a partial mapping must fail the audit rather than produce a confident and wrong delta. `scope_audit.py` does exactly this, and its `--demo` mode ships two fixtures so you can watch one audit report findings and the next one refuse.

The tool splits the leftovers into two findings, because they have different fixes. **Unexercised** means granted and never accepted by any observed call — delete it. **Narrowable** means granted and genuinely load-bearing, but a scope of smaller reach would have covered every call it carried. A tool that reported only the first finding would call an over-scoped grant clean, which is how most homegrown scope audits pass while the integration is still over-scoped.

Rank the candidates by **reach, not by the provider's capability tier**, because the two disagree in the case that matters. `drive.metadata.readonly` sits one rung below `drive.readonly` on Google's ladder, and swapping to it feels like progress — but it is still restricted, and it still enumerates every file in `corpora=user`, so the 10,200-file arithmetic above runs unchanged underneath it. `files.list` and `files.get` both also accept `drive.file`, which is non-sensitive and bounded to what the user hands over. That is the swap worth making, and only a reach ranking finds it. This is the guide's own thesis applied to its own tooling: the question is what the scope can reach, not which tier it sits in. `scope_audit.py` prints the classification and the reach beside every recommendation, and flags a recommendation that is still restricted as a partial win rather than a fix.

Two things about that ranking are worth stating plainly, because both are places where a tool can be confidently wrong.

First, the reach ranks are a judgement about the data model, not a vendor artefact. Providers publish a capability classification — Google's non-sensitive, sensitive, and restricted — and that column in `scope_audit.py` is theirs. The ordering *within* a corpus is this guide's, and so are the corpus labels themselves — which is the heavier judgement of the two, because the corpus match gates every substitution the tool will recommend and a mislabelled scope either blocks a valid narrowing or lets a broken one through. Edit both for your own model rather than treating either as derived.

Second, smaller is only a valid recommendation when the candidate is a subset of what the current scope reaches, and reach alone does not establish that. `drive.appdata` is on `files.list`'s accepted-scope list and has the smallest reach of anything on it, so a plain minimum recommends it — and it would break the agent outright, because it addresses the app's own hidden folder and none of the user's files. This guide shipped that bug before catching it. The fix is structural rather than a denylist: every scope carries a **corpus** label naming which resources it addresses, and a candidate must share the corpus of the scope it replaces. A special-purpose scope added later cannot slip through by being given a low rank, and an unprofiled scope can never win at all.

One limit no table closes: an accepted-scope list says a call is *permitted*, not that the response still carries the fields your code reads. `messages.get` accepts `gmail.metadata`, but that scope never returns bodies. Treat every recommendation as a candidate to test against a real request, which is what the tool now prints beneath each one.

Then triage what comes out into three buckets, because they have different owners:

- **Never used, no code path.** Delete it. The scope is a fossil of a feature that was cut or a debugging session that was never reverted. This is the majority.
- **Never used, code path exists.** A feature nobody exercises. Delete the scope *and* the code, or you will re-add the scope in six months.
- **Used, but by one call you could remove.** The most valuable finding. One `files.get` with `alt=media` in a summariser is what forces a content scope where a per-file one would otherwise do. Change the call, then re-run the audit — removing a single call site often collapses the recommendation from a restricted scope to a non-sensitive one, which is a change in reach rather than in tier.

### 5. Narrow the grant and record the reason

Order of operations is the whole game here, and on Google the intuitive order is actively destructive. Before anything else, read the Key Point Google prints beside every revocation code sample on the web-server OAuth page:

> "Revocation removes all OAuth 2.0 scopes previously granted to a project, invalidating any issued access or refresh tokens for **all clients registered under that project**."

Three consequences, none of them obvious from the endpoint's signature.

**The grant is per project, per user — not per token.** Google says the combined authorization "includes all scopes that the user granted to the API project even if the grants were requested from different clients", and that revoking a token representing a combined authorization revokes "access to all of that authorization's scopes on behalf of the associated user". There is one grant record per user per Cloud project, and every client under that project reads and writes the same record.

**So acquire-then-revoke is self-defeating.** The tempting order — get the narrow grant, store it, then revoke the broad one — cannot work here. Both refresh tokens belong to the same user and the same project, so the revoke you fire last invalidates the narrow token you obtained thirty seconds earlier. You end with no working credential and a user who has just been prompted for nothing. This is worth stating plainly because it is a procedure that passes code review, passes a staging test with two clients in two projects, and fails in production for every user at once.

**And the blast radius of `revoke` is wider than the agent.** Every other OAuth client registered under that Cloud project loses this user's grant too. If the agent shares a project with your main product — which is exactly what happens when someone skips [the dedicated-client advice in the Gmail guide](/connect/gmail/) — narrowing the agent signs the user out of the product. Check which clients live in the project before you send the first revoke.

So on Google the order is revoke, then re-consent, and the work is making the gap between them short and expected rather than long and surprising:

1. **Ship the narrow code path under the broad token.** The new code runs in production against the existing grant with no user action at all, so if it is wrong you find out while the safety net is still up. One limit: this de-risks a *subset* narrowing only. `drive.file` is not a subset of `drive.readonly` — it adds create and modify, and its per-file authorizations only start accruing once the app actually holds it. Reads will succeed under the broad token and the soak will look clean while proving less than it appears to. A change of access model, rather than a trim within one, needs its own plan for re-picking files after cutover.
2. **Watch it for a full business cycle.** Log the scope each call would require and confirm nothing needs the scope you are about to drop. A month-end is not optional if the agent touches anything monthly.
3. **Change the requested scope list for new consents.** Everyone who installs from now on gets the narrow set, so the over-scoped population stops growing while you deal with the one you have.
4. **Cohort and stage it, and set the deadline first.** Internal users, then a friendly cohort, then everyone. Set the date after which un-migrated grants are force-revoked *before* the first user is prompted, publish it in the first prompt, and hold it — without a deadline this stalls at 70% and leaves you running both grant shapes forever. Everything in step 5 happens inside a cohort, never across the whole user table at once.
5. **Per user, inside the interstitial: revoke, then immediately re-consent.** Do not drive this from a background job that emails people at 3am. Drive it from a first-run interstitial when the user is already in the product with their hand on the mouse: they click through, you POST the old refresh token to `https://oauth2.googleapis.com/revoke`, and you send them straight into the narrow consent screen in the same interaction. The no-access window is the few seconds between those two calls, and it is user-initiated, so it reads as a step in a flow rather than an outage. Then read the `scope` field of the token response and check it equals the narrow set exactly — Google notes that when requesting multiple permissions, "users may not grant your app access to all of them", so a partial grant is a normal outcome and not an error you can skip. If the user abandons the consent screen after the revoke, that user now has no grant at all: treat it as a first-class state, mark the connection as needing consent, and put them back into the same interstitial on their next session rather than retrying in the background. Mark that state *before* firing the revoke, not after, so a crash in the gap still leaves a record. And remember what the Key Point above means for an abandonment: the revoke cleared this user's grant for every client under the Cloud project, so any sibling integration they were using needs its own re-consent too. The state is recoverable, but not by the agent alone, and whoever owns those siblings has to know.

`narrow_google_scope.py` implements the per-user mechanic of step 5 in that order: revoke first, then consent, then verify the returned `scope`, then store — and it exits non-zero with the connection marked as needing consent if the consent does not complete. It is a single-user script with a `--user` flag; it has no cohorting, no interstitial, and no force-revoke date, because steps 1 through 4 are rollout work that lives in your product rather than in a script.

That same Key Point settles a question this guide used to hedge on. Because the grant is held at project level and the combined authorization retains every scope the user ever granted the project, **re-consenting with a strict subset does not narrow the grant record.** The new token carries only what you asked for, but the record behind it still lists the old scopes, so a later authorization request can be auto-approved back up to the broad set without showing the user anything. Only an explicit revoke clears it. Verify on your own account at the Google account permissions page that the entry lists what you expect after the cutover.

Finally, write the decision down. Not a commit message — a short record that survives the people involved:

```text
integration: meeting-brief-agent / Google Drive (client 4172...apps.googleusercontent.com)
removed:     https://www.googleapis.com/auth/drive.readonly
kept:        https://www.googleapis.com/auth/drive.file
evidence:    token audit log, 2026-05-01 to 2026-08-01 (92 days, 1 month-end close)
             exercised methods: files.get (metadata), files.export
             files.get with alt=media: 0 calls
reach before: ~250k files across 60 users (files.list, corpora=user)
reach after:  files explicitly shared with the app
re-consent:   4 cohorts, 2026-08-04 to 2026-08-29, 58/60 migrated, 2 force-revoked
owner:        <name>
```

The `reach before` line is the one that makes the next audit easier to fund.

### 6. Detect the next one before it becomes an incident

An audit is a snapshot. The control that holds is an alert that fires the moment a grant exists that no code path can justify, and there are three places to put it, cheapest first.

**In CI, before anything is granted.** Keep the requested scopes in exactly one module. Keep an allowlist file next to it that names every permitted scope with the call site that requires it. Fail the build when the requested set is not a subset of the allowlist. Adding a scope now costs a deliberate edit to a file whose only purpose is to be reviewed, which drags the invisible three-character diff described above into daylight. This catches the Friday 403 on the following Monday, which is the only moment the person who widened the scope still remembers why.

**At grant time, in the provider's audit log.** The `authorize` event carries `scope` and `client_id`, so a job polling that event can compare the granted scope string against the declared allowlist for that client and page on a mismatch. This is the detection the section title promises: it fires when a grant is created carrying a scope no code path exercises, which is minutes to hours after the fact rather than quarters. Two warnings — the log's documented lag of up to a few hours makes this near-real-time at best, not real-time, and you must alert on `client_id` you own rather than on scope strings globally, or the alert will drown in every SaaS tool your company uses.

**Continuously, as a scheduled diff.** Run steps 2 through 4 on a schedule — monthly is enough — and alert when the unexercised set is non-empty and older than a grace period. The grace period matters: a scope granted last week for a feature shipping next week is not drift. A scope unexercised for two consecutive 90-day windows is, and it should open a ticket automatically with the `reach before` number already computed, because a ticket that arrives with its own justification is a ticket that gets scheduled.

One alert to skip: do not page on the *existence* of a broad scope. That produces a permanent red light that everyone learns to ignore, and it tells you something you already know. Page on the *delta*, because the delta is new information and it is actionable on the day it appears.

## Decision table

You have found an over-granted scope. Three options, and the honest comparison is that they are not ranked — they fit different situations.

| Option | Risk reduction | User friction | Time to implement | When it wins |
| --- | --- | --- | --- | --- |
| Narrow now with re-consent | Highest. The grant no longer exists, so nothing downstream — leaked token, direct API call, bypassed proxy — can recover it. | Highest. Every user sees a consent screen at a time you choose but they do not, and some fraction clicks Deny or never returns. | Weeks. Days of engineering, then a staged rollout with a support plan and a deadline. | The scope is restricted or destructive, the data is regulated, or an incident already happened. Also whenever removal is unilateral — a GitHub App permission comes off immediately with nobody in the loop, so this row costs an afternoon and there is no reason to pick another. |
| Narrow at the next natural re-auth | Same as above, eventually, and zero until then. The exposure continues for the whole waiting period, and if no re-auth is actually scheduled this is indistinguishable from doing nothing. | None. The prompt is one the user was going to see anyway. | Hours of engineering, then wait — bounded by the re-auth, not by you. | A re-auth is genuinely on the calendar: a migration, an app rename, an enforced rotation, a provider-mandated re-verification. Requires an actual date, not the hope of one. |
| Keep the scope, enforce at call time | Partial and real. The reachable set shrinks to what policy permits for every call that goes through the policy layer. Anything that does not go through it recovers the full grant. | None. Users see nothing. | Days. A proxy or control layer, a policy, and a deployment. No consent screen, no cohorts, no deadline. | You need the exposure down this week and the re-consent cannot happen this quarter — or the agent is a fleet and the same policy has to cover twelve providers at once. Best used as the bridge that funds the narrowing, not as the destination. |

Two notes on reading the table. The rows compose: enforcement while you stage the re-consent is the strongest option available and the one most teams should actually pick. And the middle row is the one that quietly fails: "we'll narrow it at the next re-auth" is the sentence behind most scopes that are still over-granted three years later, because the re-auth was never actually scheduled. Ask for the date before you pick that row. If nobody can name one, it is the third row without the enforcement.

## Checklist

- [ ] Every scope the agent requests lives in one module, and each entry names the call site that requires it.
- [ ] A CI check fails the build when the requested scope set is not a subset of a reviewed allowlist file.
- [ ] You can produce, on demand, the granted scope set per client id from the provider — not from your source tree.
- [ ] You can produce the set of methods the client actually called over the last 90 days.
- [ ] The method-to-scope mapping is written down, and the audit fails loudly on a method it cannot map rather than silently under-reporting.
- [ ] The unexercised set for every live integration is empty, or each remaining item has a named owner and a date.
- [ ] The blast radius of each granted scope is recorded as a count of reachable resources, not as a scope string.
- [ ] Narrowing on Google revokes first and re-consents in the same user-initiated interaction, because revocation clears the grant for every client under the Cloud project, and you know which other clients share that project.
- [ ] The force-revoke date is set and published before the first user is prompted, not after the rollout stalls.
- [ ] A user who abandons the consent screen after the revoke is a handled state, not a retry loop.
- [ ] Every agent that reads content from outside your trust boundary has had its scope reviewed against that fact specifically.
- [ ] An alert fires when a new grant appears carrying a scope not on that client's allowlist.
- [ ] Any GitHub App in the inventory has had unused permissions removed, since removal takes effect immediately and needs nobody's approval.
- [ ] The decision record for every narrowing states the evidence window, the reach before, and the reach after.

## Failure modes

### The access review passes and the agent is still over-scoped

**Symptom:** the integration was reviewed six weeks ago and signed off. The scope list has not changed. The agent can still reach an unbounded set.

**Cause:** the review checked that the scopes were *documented and approved*, which is a different property from *exercised*. A reviewer reading `drive.readonly` against a summariser has no way to know whether the summariser calls `files.get` with `alt=media` on files the user did not select — that fact lives in the code and the audit log, not in the scope string. Reviews that read configuration cannot detect over-scoping, because over-scoping is defined by a gap between configuration and behaviour.

**Fix:** make the exercised set an input to the review. A review packet containing the granted set, the exercised set, and the delta takes an hour to assemble with `scope_audit.py` and converts the review from an opinion about a string into a check on a diff. If the delta is empty, the review is genuinely over in five minutes.

### The narrowing rollout stalls at seventy percent

**Symptom:** cohorts one through three migrated. The last cohort has not moved in a month. Both grant shapes are in production, the code has branches for each, and nobody wants to be the person who revokes a working integration.

**Cause:** no force-revoke date, or a date that was set and quietly slipped. Without a deadline, re-consent is opt-in, and the residual population is exactly the users with the lowest engagement — the ones least likely to respond to a prompt and most likely to file a ticket when the agent stops working.

**Fix:** set the date before the first cohort starts, publish it in the first prompt, and hold it. Accept that some grants get force-revoked and those users re-onboard later. The alternative is maintaining two authorization models forever, which costs more than the tickets and leaves the original exposure in place for the population you were most worried about.

### The audit comes back clean and the scope is still too wide

**Symptom:** the audit says every granted scope was exercised. Nothing to remove. Six months later an incident shows the agent reaching something nobody expected.

**Cause:** the method-to-scope mapping was too generous. Mapping `files.get` to `drive.readonly` when the call only ever requested metadata marks `drive.readonly` as exercised and hides the finding. A mapping that resolves each method to the broadest scope that would work will always report a clean delta, because the broadest scope always works.

**Fix:** map every method to the *narrowest* scope that satisfies it, and distinguish call variants that differ in scope requirement — `files.get` for metadata and `files.get` with `alt=media` are different rows. When you are unsure which scope a call needs, get it from the provider rather than guessing: GitHub returns `X-Accepted-OAuth-Scopes` naming "the scopes that the action checks for" and `X-Accepted-GitHub-Permissions` naming the permissions an endpoint requires, which is the requirement stated by the party that enforces it.

### Reinstalling with fewer scopes changes nothing

**Symptom:** you removed a scope from the install URL, sent users through the flow again, and the token still has it.

**Cause:** the provider merges, exactly as quoted under "Why the scope is never narrowed" above — Slack appends rather than replaces and will not downgrade a token, and Google's combined authorization retains every scope the user granted the project. A reinstall is a widening instrument; there is no narrowing instrument that looks like one.

**Fix:** revoke, then re-consent — `auth.revoke` on Slack, the revocation endpoint on Google — and accept that this is a real interruption, which is why it needs the staged, interstitial-driven rollout in step 5 rather than a script run over the whole user table. Verify the result by reading back the live grant (Slack's `x-oauth-scopes` header, GitHub's `X-OAuth-Scopes`, the Directory API `scopes` array) rather than by trusting that the install URL you edited is what the provider recorded.

### The scope is fine and the agent still reaches too much

**Symptom:** the scope list survives an honest audit — every scope exercised, nothing removable — and the reachable set is still far larger than the task.

**Cause:** the provider's scope granularity does not go where you need it to. There is no Gmail scope for "the messages relevant to this ticket", no Drive scope for "documents from this project", no Slack scope for "the last thirty days". Scopes cut by capability; tasks cut by relevance; the two axes do not meet. On Slack the second axis is channel membership, granted by anyone with an `/invite` and reviewed by nobody.

**Fix:** stop expecting the grant to be the control and put the boundary in your code. Filter server-side before results reach the model, cap what a single run may read, refuse fetches outside a declared working set, and enumerate the second axis on a schedule where one exists. The [govern pillar](/govern/) treats this as a policy problem rather than a per-integration one, which is the right altitude once you are doing it for more than two providers.

### The audit cannot be run because nobody has the admin credential

**Symptom:** you know exactly which two API calls the audit needs and you cannot make either, because both need Workspace admin scopes that no engineer holds.

**Cause:** the grant enumeration and audit log APIs are themselves privileged — `admin.directory.user.security` and `admin.reports.audit.readonly` — and correctly so. This stops more scope audits than any technical obstacle.

**Fix:** do not request standing admin access; it turns your audit tooling into the next over-scoped integration, which is a bleak way to end this guide. Ask the Workspace admin to run the two queries and hand you the JSON, or get a time-boxed grant for the audit window. Both API calls are read-only and the second is an audit log read, which is an easy ask when the request is specific. `scope_audit.py` has a `--tokens` / `--activities` mode that reads two saved JSON responses and never touches the network, for exactly this reason: the person with the credential runs two `curl` commands and sends you the files, and you run the diff.

## Doing this at scale

Everything above is one integration. A fleet is a different problem, and the arithmetic gets worse in the direction you would expect: the pairs to audit are agents times providers, the grants to enumerate are that times users, and the re-consent rollouts are one per narrowing per provider, each needing its own cohorts, copy, and deadline. At three agents and five providers, a quarterly audit is a standing team cost, and the honest read is that most organisations will not pay it — which is why the third row of the decision table exists.

The lever that changes the shape is moving enforcement from grant time to call time. If the agent does not hold the provider credential — if it holds a token for a control layer, and the control layer holds the OAuth grant and evaluates a policy per request — then an over-granted scope stops being a standing capability and becomes a capability the policy layer declines to use. The grant says `drive.readonly`; the policy says this agent may read files under one folder id for the user who triggered the run; the call that walks outside that returns a refusal, and the refusal is logged with the agent, the user, and the rule.

That is what [Agentic Fabriq](https://agenticfabriq.com) is built for: credentials live in the control layer, the agent calls a named connection, policy is evaluated per request, and every call is attributed to an agent and the user it acted for.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        # The inventory the audit needs: which connections this agent can reach
        # at all. Names and shapes are per-deployment - check yours with
        # `afctl tools list` rather than copying anything from this page.
        for tool in await af.list_tools():
            print(tool)

        result = await af.invoke_connection(
            "drive_meeting_docs",
            method="get_file",
            parameters={"file_id": os.environ["MEETING_DOC_ID"]},
        )
        print(result)


asyncio.run(main())
```

The runnable version is `examples/fail-over-scoped-oauth/enforce_at_call_time.py`. Two properties matter and only one of them is about the code being shorter. First, the Drive refresh token is not in the agent process, so a compromised agent leaks a revocable gateway token rather than a standing grant on a corpus. Second, `await af.list_tools()` gives the agent's own reachable-connection inventory without an admin credential. That is not the same artefact as step 2's grant listing — it tells you what the agent may call, not what the provider grant permits — but it is the half of the inventory an engineer can produce unaided, which is worth having when the other half is blocked on a Workspace admin.

Now the part that is easy to leave out. **This is mitigation, not narrowing, and the difference is not academic.** The `drive.readonly` grant still exists. Anything that reaches the provider without passing the policy layer recovers the whole scope — a leaked refresh token, a debugging script somebody wrote against the raw API, a second integration using the same client id, a policy misconfiguration, an outage where somebody adds a bypass to restore service and forgets to remove it. Enforcement narrows what happens; only revocation narrows what is *possible*. If the grant is one you would be unable to explain in a breach notification, enforcement is the thing you do this week so that you can afford to do the re-consent next quarter, and the ticket for the re-consent still needs an owner and a date. A layer like [Agentic Fabriq](https://agenticfabriq.com) buys you the time and the audit trail; it does not buy you the absence of the grant, and a team that believes otherwise has swapped one unbounded blast radius for a slightly better-instrumented one.

## Further reading

The [govern pillar](/govern/) is where scope selection stops being a per-integration decision and becomes a policy you can enforce — least privilege as a standing property rather than a review comment. The [connect pillar](/connect/) has the per-provider scope tables that make the arithmetic in this guide possible for Gmail, Slack, GitHub, storage, and databases; [the Gmail guide](/connect/gmail/) is the closest worked example, since Google's Gmail scopes are the cleanest published ladder from write-only to permanent deletion. [The Slack guide](/connect/slack/) covers the case where the grant is only half the reach, and [the GitHub guide](/connect/github/) covers the one provider where narrowing is free. The rest of the [fail pillar](/fail/) collects what happens after the grant, including how the data an over-scoped agent reaches escapes through prompt logs and caches.

Primary sources for everything asserted above:

- [Google Drive API-specific authorization and authentication](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) — the exact Drive scope strings including `drive.file`'s full "…while using the Google Picker API or the app's file picker" clause, and the recommended/sensitive/restricted classification of each.
- [Choose Google Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth) — the `calendar` and `calendar.app.created` scope strings and Google's wording for each.
- [Drive `files.list`](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list) and [`files.get`](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/get) — the `corpora` parameter and its `q`-dependent default, the `Corpus` enum definition "Files owned by or shared to the user", `includeItemsFromAllDrives`, and the 1,000 `pageSize` maximum. Each also publishes the accepted-scope list `METHOD_GATES` copies verbatim: eight scopes for both of these, four for [`files.export`](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export), and four each for Gmail [`messages.list`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list), [`messages.get`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get), and [`messages.send`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send).
- [Directory API: `tokens.list`](https://developers.google.com/workspace/admin/directory/reference/rest/v1/tokens/list) and [the Token resource](https://developers.google.com/workspace/admin/directory/reference/rest/v1/tokens) — the grant enumeration endpoint, its required `admin.directory.user.security` scope, and the `clientId` / `scopes` fields.
- [OAuth Token Audit Activity Events](https://developers.google.com/workspace/admin/reports/v1/appendix/activity/token) — the `activity`, `authorize`, `deny`, `request`, and `revoke` event names, and the fact that `activity` carries `api_name` and `method_name` but no `scope`.
- [Reports API: `activities.list`](https://developers.google.com/workspace/admin/reports/reference/rest/v1/activities/list) — the request path, the `token` value for `applicationName`, and the `admin.reports.audit.readonly` scope.
- [Google Workspace audit log data retention and lag times](https://knowledge.workspace.google.com/admin/reports/data-retention-and-lag-times) — six-month retention, and the "a couple of hours" lag on the *Token log events* row, which is the log this guide reads. The "up to a few hours" figure on the same page belongs to the separate *OAuth* row.
- [Using OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server) — the `scope` field of the token response, the warning that users may not grant every requested scope, `include_granted_scopes` merging upward, the combined authorization holding "all scopes that the user granted to the API project even if the grants were requested from different clients", and the Key Point that revocation "removes all OAuth 2.0 scopes previously granted to a project, invalidating any issued access or refresh tokens for all clients registered under that project".
- [Slack: installing with OAuth](https://docs.slack.dev/authentication/installing-with-oauth/) — additive scopes, "it is not possible to downgrade an access token's scopes", "there is no way to remove scopes from an existing token without revoking it entirely", the `x-oauth-scopes` response header, and `auth.revoke`.
- [Modifying a GitHub App registration](https://docs.github.com/en/apps/maintaining-github-apps/editing-a-github-apps-permissions) — added permissions requiring per-installation approval, and removed permissions taking effect immediately.
- [Scopes for OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps) — the `repo` scope description and the `X-OAuth-Scopes` / `X-Accepted-OAuth-Scopes` response headers.
- [Troubleshooting the REST API](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api) — `X-Accepted-GitHub-Permissions`, "Resource not accessible by integration", and the 404-instead-of-403 behaviour.
- [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/) — the definition of indirect prompt injection quoted above.
- [OWASP LLM06:2025 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/) — excessive functionality, permissions, and autonomy as root causes, and the excessive-permissions example.
- [The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) — private data, untrusted content, and external communication as the combination that makes exfiltration easy.
