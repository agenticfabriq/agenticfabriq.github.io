---
title: "AI Agent Access to Google Drive and Notion: Grant Boundaries"
description: "Pick drive.file over the restricted Drive scopes, put the Picker in the grant loop, and understand why one Notion share hands over a whole page subtree."
pillar: connect
updated: "2026-08-18"
keywords:
  - "drive.file scope vs drive.readonly"
  - "notion api 404 object_not_found"
  - "ai agent google drive access"
  - "notion integration capabilities"
relatedGuides:
  - connect/gmail
  - govern/least-privilege
  - fail/over-scoped-oauth
cornerstone: false
howTo:
  - name: Pick the Drive scope from the call list, not from the tutorial
    text: Default to https://www.googleapis.com/auth/drive.file, which is non-sensitive and covers only files the app created or the user handed it.
  - name: Put the Google Picker in the grant loop
    text: Let the user choose files in the Picker so the drive.file grant grows one file at a time instead of covering the whole Drive at consent.
  - name: Export Google Workspace documents instead of downloading them
    text: Call files.export with a concrete MIME type for Docs and Sheets, and use alt=media only for files that have real bytes.
  - name: Create the Notion connection and set its capabilities deliberately
    text: Choose read, update, insert, comment, and user-information capabilities at the connection level, because they apply to every page the connection is ever given.
  - name: Share one Notion page and count what arrived
    text: Add the connection through the page menu, then walk the subtree, because sharing a parent grants access to all of its child pages.
  - name: Treat a Notion 404 as an access answer
    text: object_not_found means either the page does not exist or it was never shared with your token, so never retry it as a transient error.
faq:
  - question: Which Google Drive scope should an AI agent use?
    answer: Default to https://www.googleapis.com/auth/drive.file, which Google classifies as non-sensitive and which covers only files the app created or the user selected in the Google Picker. The broad scopes https://www.googleapis.com/auth/drive and https://www.googleapis.com/auth/drive.readonly are restricted and require verification.
  - question: Does a Notion integration see every page in the workspace?
    answer: No. A Notion connection starts with no page access and sees only what someone explicitly shares with it. But Notion's docs state that sharing a parent page with a connection grants access to all of its child pages, so one share can hand over a large subtree.
  - question: Why does the Notion API return 404 for a page I can see in my browser?
    answer: Notion returns the object_not_found error code when a resource does not exist and also when it has not been shared with the owner of the bearer token. Add the connection to that page through the page menu rather than retrying the request.
  - question: Can a Notion integration lose access without anyone changing its settings?
    answer: Yes. Notion states that if a user loses edit access to the page where they added a connection, that connection also drops to read access, regardless of the capabilities it was created with.
  - question: Do I need Google verification to read a user's Drive files?
    answer: Only if you use a restricted scope, and the verification requirement attaches to public production access. drive.file is non-sensitive, while drive, drive.readonly, drive.metadata, and drive.metadata.readonly are restricted, which brings OAuth verification plus a security assessment if restricted-scope data reaches your servers. Confirm your own case against Google's restricted-scope verification page, because internal-only apps follow a different path.
  - question: Can a Notion workspace owner stop members from sharing pages with a connection?
    answer: On Enterprise plans, yes. A workspace owner can open Settings then Connections, use the Manage tab, select the connection's ... menu and Manage page access, and set Who can manage page access to Connection owners only. Enterprise owners can also restrict which connections members may install. It does not change child-page inheritance.
  - question: What is the Google Picker API for?
    answer: It renders a Drive file-open dialog inside your app and is how a drive.file grant grows one file at a time. Google recommends pairing it with drive.file, and for desktop and mobile apps drive.file is the only scope the Picker permits.
---

## TL;DR

- On Drive, default to `https://www.googleapis.com/auth/drive.file`. Google classifies it as non-sensitive and describes it as covering files "that you open with an app or that the user shares with an app while using the Google Picker API or the app's file picker". Pair it with the Google Picker and the grant grows one file at a time.
- `https://www.googleapis.com/auth/drive` and `https://www.googleapis.com/auth/drive.readonly` are *restricted* scopes. Public production access brings OAuth verification, and a security assessment once restricted-scope data reaches your own servers. Choosing them costs weeks before it costs anything else.
- Notion has no content scopes at all. A connection has *capabilities* — read content, update content, insert content, read comments, insert comments, and one of three user-information settings — and separately it has a list of pages a human added it to.
- Notion access is inherited: "Sharing a parent page with the connection grants access to all of its child pages as well." One click by a non-engineer can hand an agent a subtree nobody has inventoried.
- The two models fail in opposite directions. Drive over-grants in a code review, where an engineer can see the scope string in a diff. Notion over-grants in a page menu, where by default no engineer is present and nothing appears in the diff at all — Enterprise workspaces can restrict who is allowed to open that menu, and should.

## Who this is for

You are connecting an agent to the documents a company actually runs on — a Drive full of contracts and spreadsheets, a Notion workspace holding the handbook, the runbooks, and somebody's performance notes. This guide covers the permission model on each side, the code that respects it, and the specific ways each one surprises people in production. Skip it if your documents live in a bucket you own: object storage has an IAM policy you write, not a consent screen and a share menu operated by people who do not work for you.

## The problem

Two failures, one per provider, and they do not look alike.

The Drive one starts in a tutorial. The tutorial asks for `https://www.googleapis.com/auth/drive.readonly` because it makes every snippet work on the first attempt — `files.list` returns results, search finds things, almost nothing 403s. The agent ships. What shipped is a standing grant to read every file in a person's Drive: the tax return, the offer letter for the role they did not take, the shared folder from the last company. Nobody chose that. It arrived because the narrow scope makes the first ten minutes harder and the broad one makes them easy, and the habit forms in those ten minutes and never gets revisited. Six months later it is in the consent screen of every user you have, and narrowing it means re-consenting all of them.

The Notion one starts in a meeting. Someone from the operations team is told to "give the assistant access to the runbooks", and the runbooks live under a page called *Engineering*. They open *Engineering*, click through the page menu, and add the connection there, because that is the page they were looking at. Every child page comes with it: the incident write-ups, the postmortems naming individuals, the compensation-band draft somebody nested under *Engineering / People* eighteen months ago. No scope was widened. No pull request was opened. The agent's code is unchanged and its capabilities are unchanged, and its reach grew by two orders of magnitude. The [governance pillar](/govern/) is where you make that kind of change visible, because Notion will not: its [webhook event list](https://developers.notion.com/reference/webhooks-events-delivery) covers pages, databases, data sources, and comments — `page.created`, `page.moved`, `database.schema_updated` and their siblings — and contains no event for a connection being added to a page. The share that doubled the agent's reach fires nothing.

Both failures share a property worth naming: the blast radius is set by somebody else's filing habits. You do not control how deeply a Drive folder nests or how a Notion workspace is organised, so a permission model that grants by *container* hands you a variable you cannot see. That is why the honest first step on either provider is to enumerate what you were actually given, before the agent reads a single word of it.

## Step by step

One path per provider, because the interesting content is in the difference. Runnable sources: `examples/connect-drive-notion/drive_file_agent.py` and `examples/connect-drive-notion/notion_subtree_audit.py`.

### 1. Pick the Drive scope from the call list, not from the tutorial

These are the Drive scopes worth knowing, with Google's own classification:

| Scope | What it permits | Classification |
| --- | --- | --- |
| `https://www.googleapis.com/auth/drive.file` | Create new Drive files, or modify existing files, that the user opens with the app or shares with it through the Google Picker. | Non-sensitive |
| `https://www.googleapis.com/auth/drive.appdata` | View and manage the app's own configuration data in the user's Drive. | Non-sensitive |
| `https://www.googleapis.com/auth/drive.install` | Let the app appear in Drive's "Open with" or "New" menu. | Non-sensitive |
| `https://www.googleapis.com/auth/drive.apps.readonly` | View the apps authorized to access the user's Drive. | Sensitive |
| `https://www.googleapis.com/auth/drive.metadata.readonly` | View metadata for every file in the Drive. No content. | Restricted |
| `https://www.googleapis.com/auth/drive.metadata` | View and manage metadata for every file in the Drive. | Restricted |
| `https://www.googleapis.com/auth/drive.readonly` | View and download every file in the Drive. | Restricted |
| `https://www.googleapis.com/auth/drive` | View and manage every file in the Drive, including deletion. | Restricted |
| `https://www.googleapis.com/auth/drive.activity.readonly` | View the activity record of files in the Drive. | Restricted |
| `https://www.googleapis.com/auth/drive.scripts` | Modify the behaviour of the user's Apps Script projects. | Restricted |

The line that matters runs between row one and row five. `drive.file` is per-file: the app reaches files it created and files the user explicitly handed it, and nothing else. Every restricted scope is per-Drive: the unit of the grant is the whole account, and nothing in the scope list above narrows it to a folder, a label, or a date range. Note that even `drive.metadata.readonly` is restricted — a filename list is not a small ask when the filenames are `Q3 layoffs - draft.xlsx` and `NDA - Acme.pdf`.

Restricted scopes also change your schedule. Public production access requires OAuth verification, and Google states that if you store or transmit restricted-scope data on servers you must go through a security assessment. An agent that pipes document text to a hosted model is transmitting it to a third-party server, which is precisely the condition. `drive.file` avoids all of it. That asymmetry — weeks of assessment versus none — is the strongest argument for the narrow scope, and it is the one that persuades people who were unmoved by the privacy argument.

A reviewer who sees `drive.readonly` in a diff should ask one question: *which file does the agent need that the user cannot hand it?* If the answer is "we don't know which ones until runtime", the answer is the Picker, not a wider scope. If the answer is "all of them, for an index", you have a real restricted-scope case and should budget for it deliberately rather than discovering it at launch.

### 2. Put the Google Picker in the grant loop

`drive.file` is not a smaller version of `drive.readonly`; it is a different shape. There is no discovery. The agent cannot search a Drive it has not been given, so something has to hand it ids, and that something is the [Google Picker](https://developers.google.com/workspace/drive/picker/guides/overview) — Drive's own file-open dialog, rendered inside your app.

Google's guidance is explicit that using `drive.file` together with the Picker optimises both user experience and safety, and that web apps using the Picker can use `drive.file`, `drive.readonly` and others, while desktop and mobile apps are strict: only `drive.file` is permitted. You pass the OAuth access token to the Picker with `setOAuthToken(accessToken)`, and the user's selection is what extends the grant.

`DocsView` is where you constrain what the user can even pick. `setMimeTypes(mimeTypes)` limits the view to the file types your agent can handle, `setParent(parentId)` sets the initial folder, `setIncludeFolders(included)` shows folders in the view, and `setSelectFolderEnabled(enabled)` allows selecting one. Leave the last one off unless you have a reason. A folder selection may be the Drive-side equivalent of sharing a Notion parent — see below for why that is a question rather than a statement.

One thing to check for yourself rather than take from this page: I could not find a statement on Google's Drive or Picker documentation that says whether a `drive.file` grant on a folder extends to the files inside it. The mechanism is testable in about a minute — select a folder in the Picker, then call `files.list` with `q='<folderId>' in parents` and see whether the children come back or whether a direct `files.get` on one of them returns `appNotAuthorizedToFile`. Do that test on your own client before you design around either answer. `list_children()` in the example script carries the same caveat in a comment rather than assuming.

### 3. Read a file, and export Workspace documents instead of downloading them

Drive splits sharply between blob files and Google Workspace editor files. A PDF or a CSV has bytes, and you fetch them with `files.get` plus `alt=media`, which Google describes as telling the server that a download of content is being requested as an alternative response format. A Google Doc has no bytes to fetch; it must be converted, which is `files.export` with a target MIME type.

```python
export_mime = EXPORT_AS.get(mime_type)
if export_mime is not None:
    try:
        response = call(token, f"/files/{file_id}/export",
                        params={"mimeType": export_mime})
    except DriveError as error:
        if error.status in (403, 400):
            raise ExportTooLarge(
                f"export of {file_id} as {export_mime} failed ({error}). "
                "Exported content is limited to 10 MB; for larger content "
                "use files.download with a long-running operation."
            ) from error
        raise
    if len(response.content) >= EXPORT_SIZE_LIMIT_BYTES:
        raise ExportTooLarge(...)
    text = response.text[:limit]
    if mime_type == GOOGLE_SHEET_MIME:
        return PARTIAL_SHEET_MARKER + text   # names the limitation in the payload
    return text

if mime_type.startswith("text/") or mime_type == "application/json":
    return call(token, f"/files/{file_id}", params={"alt": "media"}).text[:limit]

return f"<{mime_type}: not fetched>"   # binary; do not pull speculatively
```

Two constraints belong in the code, not in a comment, which is why they are in the code above rather than in the sentence you are reading. Exported content is limited to 10 MB, so an export path with no size handling fails on exactly the large documents an agent is most likely to be pointed at; `read_text()` converts that failure into a named `ExportTooLarge` carrying the route that does work, `files.download` with a long-running operation. And exporting a spreadsheet to `text/csv` gives you the first sheet, so the example returns it behind `PARTIAL_SHEET_MARKER`, a line of text stating that other sheets were not read. That marker travels with the content into the prompt, which is the only place it can do any good — a silent, partial read is the worst kind of failure, because the agent produces a confident answer from two thirds of the data.

One honesty note on that error branch: Google documents the 10 MB limit but I did not find the error reason string Drive returns when a document exceeds it, so the code branches on the export call failing rather than on a literal I could not verify. If you find the reason string on a Google page, tighten the branch.

Truncate before the prompt, not after. `read_text()` in the example takes a `limit` and applies it at the boundary, because a 200-page contract that reaches your process has already been through your logs and your retry buffers even if the model never sees it. The [failure pillar](/fail/) covers where that content escapes next.

### 4. Handle the boundary error instead of widening the scope

When an agent under `drive.file` touches a file it was not given, Drive answers 403 with reason `appNotAuthorizedToFile` and the message "The user has not granted the app {appId} {verb} access to the file {fileId}." That is not an error to retry and not a signal to request more scope. It is the model working.

```python
if reason == "appNotAuthorizedToFile":
    # The user never handed this file to this app. No retry, no widening
    # of scope: send them back to the Picker.
    raise NotOurFile(message)
```

Distinguish it from the neighbours, because three different 403s arrive at the same handler. `insufficientFilePermissions` means the *user* lacks permission on the file, so a Picker round trip will not help either — the user has to ask the owner. `userRateLimitExceeded` and `rateLimitExceeded` are throttling and should back off. Branch on `error.errors[0].reason`, never on the status code alone.

### 5. Create the Notion connection and set its capabilities deliberately

Now switch models entirely. Notion issues a connection — internal, scoped to one workspace and authenticated by a static secret, or public, installed across workspaces through OAuth. In both cases, what the connection may *do* is fixed by capabilities set on the connection itself, and what it may *reach* is a separate list of pages that humans maintain.

The capabilities, by Notion's own names:

| Capability | What it allows |
| --- | --- |
| Read content | Read existing content in the workspace, such as retrieving databases, without modifying it. |
| Update content | Update existing content, such as updating pages, without creating new objects. |
| Insert content | Create new content, without granting full read access to objects. |
| Read comments | Read comments from a page or block. |
| Insert comments | Insert comments in a page or in an existing discussion. |
| No user information | User data is withheld from API responses. |
| User information without email addresses | Name and profile image, with the email address omitted. |
| User information with email addresses | Name, profile image, and email address. |

The user-information group is three mutually exclusive settings, not three switches, and it is the one most often left at its most generous value by accident. An agent that summarises pages does not need anyone's email address; pick the middle option and the addresses stop appearing in your prompt payloads without any code change on your side.

Read and insert are genuinely separable, and the split is useful. An agent that files incident reports needs *insert content* and nothing else — it can create pages without being able to read the ones already there. That is the closest thing Notion has to Gmail's write-only `gmail.send`, and the same reasoning applies. The [connect pillar](/connect/) covers where else this asymmetry shows up.

Capabilities are set once on the connection and there is no per-page capability to set. They are a ceiling rather than a fixed grant, though, and the distinction bites in production: Notion states that a connection's capabilities never supersede those of the user who added it, so one connection can hold *update content* in the developer portal and have read-only effect on a page whose adder has since lost edit access. The failure mode below is what that looks like from the outside. If one workflow needs write access and another needs read, that is two connections, not one connection used carefully.

Authenticate with the `Authorization: Bearer {INTEGRATION_TOKEN}` header and pin `Notion-Version`. Notion treats the version header as required and returns 400 `missing_version` without it. As of this writing the current version is `2026-03-11`; check [Notion's changelog](https://developers.notion.com/page/changelog) before you move that string, because the `2025-09-03` version was a breaking change that split databases into databases plus data sources and moved queries from `POST /v1/databases/{id}/query` to `POST /v1/data_sources/{id}/query`. (Check that and you will find Notion's own upgrade guide rendering the older form as `PATCH` in at least one place. The endpoint reference is the one to trust: it states `post /v1/data_sources/{data_source_id}/query`.) Notion's changelog also notes that tokens issued from 25 September 2024 carry an `ntn_` prefix instead of the older `secret_`, and advises treating the token as an opaque string rather than validating it with a regular expression — so use the prefix to recognise a leaked secret in a scanner, not to gate your own code.

### 6. Share exactly one page, then count what arrived

A new connection has no page access at all, and Notion is direct about the consequence: skip the sharing step and every API request returns an error. Access comes from a human, through the UI: open the page, click the `•••` menu in the top-right corner, select **Connections**, then **+ Add connection**, and choose the connection. For a public integration the equivalent happens during the OAuth prompt, where users select which pages to give the connection access to.

*Which* human is allowed to make that click is itself configurable, on Enterprise plans, and this is the one product control aimed squarely at the problem this guide describes. A workspace owner opens `Settings` → `Connections`, switches to the `Manage` tab, selects `•••` next to the connection and then `Manage page access`, and changes **Who can manage page access** from **Connection owners & workspace members** to **Connection owners only**. Enterprise owners can also restrict which connections members are allowed to install at all. If you are on Enterprise, set both before you write any code: it moves the grant decision from whoever happened to have a page open to a named, small set of people who can be asked what a subtree is. It does not change inheritance — a connection owner who shares a parent still hands over everything beneath it — and on every plan below Enterprise the control is not available, which is why the rest of this section is about detection rather than prevention.

And here is the sentence the whole Notion half of this guide exists for, from Notion's own documentation:

> Sharing a parent page with the connection grants access to all of its child pages as well.

The public-integration docs say the same thing from the other side, describing it as a feature: parent pages can be selected to quickly provide access to child pages, as giving access to a parent page will provide access to all available child pages.

That is a reasonable design for a human collaborator and a hazard for an agent, because the person clicking is choosing from the page they happen to have open, not from an inventory. So build the inventory. `POST /v1/search` "searches all parent or child pages and data_sources that have been shared with a connection", which makes it the closest thing to a permission list Notion offers, and walking `GET /v1/blocks/{block_id}/children` from each root turns that flat list into the tree somebody actually granted:

```python
for block in block_children(token, page_id):
    if block.get("type") == "child_page":
        print(f"{'  ' * depth}  page: {title_of(block)}")
        count += walk(token, block["id"], depth + 1, seen)
    elif block.get("type") == "child_database":
        # Databases hold rows the walk above never touches. Every row is a
        # page, and every row came with the parent.
        print(f"{'  ' * depth}  database: {title_of(block)} (rows not counted)")
```

Run `examples/connect-drive-notion/notion_subtree_audit.py` immediately after anybody shares a page, and compare the count against the number of pages they meant to share. When it is a hundred times larger, the fix is not in your code: remove the connection from the parent and add it to the leaves. Re-run it on a schedule too, because the tree grows without anyone telling you — a new child page under a shared parent is readable the moment it is created.

### 7. Read a page's blocks

Notion content is a block tree, not a document. `GET /v1/pages/{page_id}` returns the page object and its properties; the text lives in `GET /v1/blocks/{block_id}/children`, paginated with `start_cursor` and `has_more`, and any block with `has_children: true` needs its own call. A page with nested toggles and columns costs one request per level per branch, which is where the rate limit finds you — see the failure mode below before you write a recursive fetch with no throttle.

Pace yourself deliberately. Notion documents "an average of three requests per second, with some bursts beyond the average allowed" per connection, plus a separate per-workspace limit shared across all connections and scaled to the workspace's plan. That second limit is the one that makes your agent someone else's problem: your recursive walk can throttle an unrelated integration in the same workspace.

### 8. Treat a Notion 404 as an access answer

Notion's `object_not_found` carries a documented double meaning: "Given the bearer token used, the resource does not exist. This error can also indicate that the resource has not been shared with owner of the bearer token." Notion will not confirm that a page exists if you were not given it, which is correct behaviour and a genuinely confusing debugging experience.

So a 404 is never transient. Neither is 403 `restricted_resource`, which means the token is valid and the operation is not — the capability is missing, or the underlying user permission is. The example script raises a distinct exception for each and retries neither:

```python
if code == "object_not_found":
    raise NotShared(message)
if code == "restricted_resource":
    raise CapabilityDenied(message)
```

Everything else worth retrying is 429, 529, and 5xx. On 429 read `Retry-After`, which Notion documents as an integer number of seconds, and honour it instead of layering your own backoff curve on top.

## Decision table

| Grant model | When it wins | What it costs | What breaks first |
| --- | --- | --- | --- |
| Drive `drive.file` + Google Picker | The agent works on documents a user points at. The default, and correct far more often than it is chosen. | A UI surface you have to build; no discovery, so every id comes from a human or from a file the agent made. | Workflows that assumed search. `files.list` returns only what the app was given, which reads as "the API is broken". |
| Drive restricted scope (`drive.readonly` / `drive`) | The agent genuinely must index or search a whole Drive — e-discovery, migration, a corpus-wide classifier. | OAuth verification, plus a security assessment once restricted-scope data reaches your servers. Weeks, not days. | The launch date. The scope works immediately in testing and blocks you at production access. |
| Drive service account added to a shared drive | Unattended jobs over a fixed, org-owned corpus with no interactive user to consent. | An identity nobody consented to, whose access is added and removed by shared drive Managers and Workspace admins rather than by the people whose work is in the drive. | Attribution. Every action is the service account's, so "who read this" has one answer for every job. |
| Notion connection, shared per page | Any Notion agent. There is no alternative model — this is how Notion works. | Grants are made by humans in a UI and inherited by child pages. Invisible to code review by default, though Enterprise plans can restrict page-access management to connection owners. | Reach. The subtree grows without a deploy, and nothing notifies you. |

One caveat on the service account row that catches people: Google notes that service accounts do not belong to your Workspace domain the way user accounts do, so anything shared with your entire domain is *not* thereby shared with a service account. Its access is exactly the memberships and file permissions you granted it, which is a feature for blast radius and a surprise for anyone expecting domain-wide sharing to carry.

Be precise about revocation, because this is where the row differs from every other in the table and where it is easy to overclaim. The access *is* removable: Google's access-level table gives shared drive Managers, and Workspace admins, the ability to add or remove members of a shared drive and to change member access levels. What is missing is the per-user revocation that OAuth gives you. No individual consented to the service account, so none of them has a grant to withdraw from their own account permissions page, and someone whose documents live in the drive but who is only a Contributor has no membership control at all. The switch exists; it is on the drive, held by a Manager, not on the desk of the person whose work is exposed.

I could not settle one related question against a Google page: whether an ordinary member can *see* the service account in the drive's member list. Google's access-level table lists membership management as a Manager capability and does not list viewing members as a capability for any level, and the `permissions.list` reference says only that it "Lists a file's or shared drive's permissions" without naming a required role. So this guide does not claim the identity is hidden and does not claim it is visible. Open a shared drive you control, look at its members, and you will know in ten seconds what no page I found states.

The Drive rows are a real choice; the Notion row is a constraint. That asymmetry is worth planning around: on Drive you can move risk into your code, and on Notion you mostly cannot. What you can do on Notion is narrow who is permitted to make the grant — **Who can manage page access**, Enterprise only — and instrument it on every plan: audit the subtree on a schedule, alert when the reachable page count jumps, and pick capabilities that make a mis-share less costly. A connection with only *read content* and *user information without email addresses* that is accidentally given the whole workspace is a bad day. The same mistake with *update content* is a restoration from backup.

## Checklist

- [ ] Every Drive scope in your consent request maps to a specific API call you can name.
- [ ] `drive.readonly` and `drive` appear nowhere unless a documented requirement forces them, and a grep for them runs in CI.
- [ ] The Picker constrains selection with `setMimeTypes`, and `setSelectFolderEnabled` is off unless folder selection is a requirement.
- [ ] You have tested, on your own client, whether a picked folder extends the grant to its contents, and the code matches the answer.
- [ ] `appNotAuthorizedToFile` routes the user back to the Picker instead of triggering a retry or a scope change.
- [ ] Workspace documents go through `files.export`, and the 10 MB export limit has a handled path that names `files.download`.
- [ ] Multi-sheet spreadsheet exports carry a marker in the returned text saying which sheet was read, or are refused outright.
- [ ] `files.list` results are checked for `incompleteSearch`, so a truncated listing is never reported as an inventory.
- [ ] `corpora` is `user` or `drive` rather than `allDrives`, which Google's `files.list` reference asks you to prefer for efficiency.
- [ ] A 401 mid-run triggers exactly one token refresh, and a refresh that returns `invalid_grant` stops the run instead of continuing with partial results.
- [ ] The Notion connection's capabilities are the minimum set, and the user-information setting is not the email-inclusive one unless email is required.
- [ ] Read-only and write workflows use separate Notion connections, because capabilities are per-connection.
- [ ] `Notion-Version` is pinned to a literal in one place, and upgrading it is a reviewed change.
- [ ] On an Enterprise plan, **Who can manage page access** is set to **Connection owners only**, and the connections members may install are restricted.
- [ ] The subtree audit runs on a schedule and alerts when the reachable page count changes.
- [ ] `object_not_found` and `restricted_resource` are terminal in your retry logic; only 429, 529, and 5xx retry.
- [ ] Document text is truncated at the fetch boundary, before it can reach a prompt or a log.
- [ ] Offboarding removes the connection from Notion pages and revokes the Drive refresh token, and someone owns that step.

## Failure modes

### The agent answers a question from a document nobody meant to share

**Symptom:** no error. The agent cites a page the asker did not know existed — a postmortem naming an individual, a draft compensation band, an unreleased roadmap. It is usually a person, not a monitor, that finds this.

**Cause:** a parent page was shared with the connection, and every child came with it. On the Drive side, the same shape arrives when a broad scope makes the whole account reachable, or possibly when a folder is picked — see step 2 for why that last one is a question rather than a statement.

**Fix:** run the subtree audit and compare its count against intent. Remove the connection from the parent, add it to the specific pages, and re-run. Then make the audit periodic, because the leaf-level grant you just made will accumulate children of its own. On an Enterprise plan, close the door as well as sweeping up behind it: set **Who can manage page access** to **Connection owners only** and restrict which connections members may install, both from `Settings` → `Connections` → `Manage` (step 6 has the full path). That is the difference between this failure recurring monthly and it recurring once. Below Enterprise the control does not exist and the audit is the whole defence. If the workspace's structure makes leaf-level sharing impractical, the honest move is a dedicated Notion page tree for agent-readable content, with humans copying material into it. That is real work; it is less work than the alternative.

### A page the user is looking at right now returns 404

**Symptom:** the user pastes a Notion URL, the agent extracts the id correctly, and the API returns 404 `object_not_found`. The user is certain the page exists, because it is open on their screen.

**Cause:** the page was never shared with the connection. Notion documents `object_not_found` as covering both a non-existent resource and a resource not shared with the owner of the bearer token, so the response does not distinguish the two for you.

**Fix:** treat it as an access prompt, not an error. Tell the user to open the page's `•••` menu, choose **Connections**, and add the connection — and say which connection by name, because a workspace usually has several. Never retry a 404, and never let it fall into generic backoff, where it becomes three wasted attempts and a misleading log line.

### `files.list` returns almost nothing and the API looks broken

**Symptom:** you switch from `drive.readonly` to `drive.file`, and a listing that returned hundreds of files returns three, or none. Search stops finding known documents.

**Cause:** this is `drive.file` behaving correctly. The grant covers files the app created and files the user handed it. There is no Drive-wide corpus behind `files.list` any more.

**Fix:** stop treating listing as discovery. Ids come from the Picker or from files the agent created; persist them, keyed by user, and let the set grow as the user picks more. If a workflow genuinely cannot function without corpus-wide search, that is the case for a restricted scope — make it explicitly, with the verification timeline attached, rather than reaching for the scope to make an error go away.

### Shared drive files are invisible even under a broad scope

**Symptom:** files in a shared drive do not appear in `files.list` results, even though the user can see them and the scope is broad enough.

**Cause:** the default item collection is the user's own corpus. Shared drive items are excluded unless you say otherwise, and Drive's documentation is explicit that you must set `corpora` to reach collections beyond the default.

**Fix:** set `corpora`, `includeItemsFromAllDrives=true`, and `supportsAllDrives=true` together. Setting one or two of the three is the common mistake and produces the same empty result as setting none. Use `corpora=drive` with a `driveId` rather than `corpora=allDrives` — Google's `files.list` reference asks you to prefer `user` or `drive` for efficiency — and read `incompleteSearch` on the response, because a `true` there means results are missing and a listing you present as an inventory is not one.

### The Notion agent's writes stop working and nothing was deployed

**Symptom:** an agent that has been updating pages for weeks starts getting 403 `restricted_resource` on updates. Reads still work. No code changed, and the connection's capabilities in the developer portal still show update content.

**Cause:** capabilities are a ceiling, not a floor. Notion states that if a user loses edit access to the page where they added a connection, that connection also drops to read access, regardless of the capabilities it was created with. Someone changed a human's permissions, and the connection followed.

**Fix:** surface it as a permissions problem rather than an outage. Name the page in the error, and check the sharing state of the *person who added the connection*, not just the connection's own settings. When it matters operationally, have a workspace-level admin add the connection instead of an individual contributor whose access changes with their team.

### The recursive Notion walk 429s, then throttles someone else's integration

**Symptom:** a page with nested toggles or a database of any size produces a burst of 429 `rate_limited` responses. Sometimes a colleague reports that an unrelated Notion automation slowed down at the same moment.

**Cause:** every nested block level is another request, and Notion applies an average of three requests per second per connection *plus* a separate per-workspace limit shared across all connections. A depth-first walk with no pacing blows through both.

**Fix:** rate limit on the way out, not on the way back. The example script gates every call through a single throttle that paces *below* the documented average — a 0.4 second base with 0.1 seconds of jitter, rather than the 0.333 seconds that would target the average itself and put half your requests over it — and honours `Retry-After` on 429 and 529. Cap depth explicitly — `MAX_DEPTH` in the example — and keep a `seen` set, because a Notion tree with a linked structure can otherwise be walked more than once.

### Drive quota disappears into listing and downloading

**Symptom:** the agent gets throttled while doing what looks like very little work, long before it writes anything.

**Cause:** Drive meters quota units, not requests, and reads are not the cheap operation. Against 1,000,000 units per minute per project and 325,000 units per minute per user per project, a read such as `files.get` costs 5 units, a list such as `files.list` costs 100, a download costs 200, and an edit such as `files.update` costs 50. A crawler that lists a folder and downloads twenty files spends 4,100 units in one pass.

**Fix:** request narrow `fields` so one call answers the question, page with a sensible `pageSize` instead of many small pages, and cache file metadata keyed by `id` and `modifiedTime` so an unchanged file is never downloaded twice. Back off on 429 and on 403 with reason `rateLimitExceeded` or `userRateLimitExceeded`, and on nothing else.

### The spreadsheet answer is confidently wrong

**Symptom:** the agent summarises a workbook and its numbers do not match what the user sees. No error anywhere.

**Cause:** exporting a Google Sheet to `text/csv` yields one sheet. The other tabs were never fetched, and nothing in the response says so.

**Fix:** detect `application/vnd.google-apps.spreadsheet` and refuse the naive path. Either read the workbook through the Sheets API, which addresses ranges per sheet, or put the limitation in the returned content where the model will see it — `read_text()` in the example prefixes the CSV with `PARTIAL_SHEET_MARKER`, a line stating that other sheets were not read. An agent that says "based on sheet 1 of 4" is useful; one that silently averages a third of the data is worse than no agent.

## Doing this at scale

Everything above is one user and two providers. The operational shape at fifty users and eight providers is different in kind, not degree, and it is mostly bookkeeping that nobody budgeted for.

On Drive, you hold one refresh token per user per agent, and under `drive.file` you also hold the file-id set each grant covers, because the grant is no longer describable by a scope string — it is a list that grows every time somebody uses the Picker. On Notion, you hold connection secrets that do not expire on their own, and a page-access graph maintained entirely by people in a UI, with no webhook to subscribe to for the reason given above. Then the cross-cutting questions arrive: which agent read which document, on whose authority, and when. Which grants a departing employee's offboarding needs to unwind, on both providers, in one pass. Whether the Notion connection is still confined to the pages it was confined to last quarter. Ordinary application logs answer none of that unless you built them to, and each question is answerable only if you decided to record it before the auditor asked.

That lifecycle is what [Agentic Fabriq](https://agenticfabriq.com) exists to hold. The Drive refresh token and the Notion secret live in the control layer; the agent holds a gateway token and calls a named connection. Policy is evaluated per request, so "this agent may read Drive but not Notion" is a rule rather than a code path, and every call is attributed to an agent and the user it acted for — which is the record that answers the questions in the paragraph above without a grep.

```python
import asyncio
import os

from af_sdk.fabriq_client import FabriqClient


async def main() -> None:
    async with FabriqClient(
        base_url="https://dashboard.agenticfabriq.com",
        auth_token=os.environ["AF_TOKEN"],
    ) as af:
        for tool in await af.list_tools():
            print(tool)

        drive_files = await af.invoke_connection(
            "drive_docs",
            method="list_files",
            parameters={"page_size": 25, "q": "trashed = false"},
        )
        notion_pages = await af.invoke_connection(
            "notion_handbook",
            method="search",
            parameters={"page_size": 25},
        )
        print(len(drive_files.get("files", [])), len(notion_pages.get("results", [])))


asyncio.run(main())
```

The runnable version is `examples/connect-drive-notion/list_documents.py`. Connection names, method names, and response shapes are per-deployment, so run `afctl tools list` against your own gateway rather than trusting `drive_docs`, `notion_handbook`, or the `files` and `results` response keys here. The property worth noticing is not the shorter code — it is that neither long-lived credential is in the agent process, so a compromised agent leaks a revocable gateway token instead of a standing grant on a Drive and a workspace. A layer like [Agentic Fabriq](https://agenticfabriq.com) buys you the lifecycle, not the API call; every scope and capability decision above stays exactly as important if you own that lifecycle yourself.

## Further reading

The [connect pillar](/connect/) collects the other integrations, and the contrast in this guide generalises: the providers in this library grant by scope, by container, or by explicit share, and knowing which one you are dealing with tells you where the over-grant will come from. [Connecting an AI Agent to Gmail](/connect/gmail/) is the scope-shaped case in full detail, including Google's restricted-scope verification path, which applies unchanged to Drive's restricted scopes. The [govern pillar](/govern/) covers making grant changes visible and reviewable, which is the defence available on every Notion plan and the one that still matters on Enterprise after you have narrowed who may grant, and the [fail pillar](/fail/) covers what happens to document content once it is inside a prompt pipeline.

Primary sources for everything asserted above:

- [Choose Google Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth) — the scope strings, their non-sensitive, sensitive, and restricted classifications, the `drive.file` description, and the verification and security-assessment requirements for restricted scopes.
- [Overview of the Google Picker](https://developers.google.com/workspace/drive/picker/guides/overview) — what the Picker is, the recommendation to pair it with `drive.file`, and the desktop and mobile restriction to `drive.file` only.
- [Integrate the Google Picker into web apps](https://developers.google.com/workspace/drive/picker/guides/web-picker) — `setOAuthToken` and the scope required for the views.
- [Google Picker DocsView reference](https://developers.google.com/workspace/drive/picker/reference/picker.docsview) — `setMimeTypes`, `setParent`, `setIncludeFolders`, and `setSelectFolderEnabled`.
- [Search for files and folders](https://developers.google.com/workspace/drive/api/guides/search-files) — the `q` query syntax and the `corpora` parameter.
- [`files.list` REST reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list) — `driveId`, `includeItemsFromAllDrives`, `supportsAllDrives`, and `incompleteSearch`, and the instruction to prefer `user` or `drive` over `allDrives` for efficiency. These are not on the search guide; that link covers `q` and `corpora` only.
- [Manage shared drives](https://developers.google.com/workspace/drive/api/guides/manage-shareddrives) — the note that service accounts do not belong to your Workspace domain, so domain-wide sharing does not reach them.
- [Shared drive access levels](https://support.google.com/a/answer/7337554) — that Managers can add or remove members of a shared drive and change member access levels, and that no level below Manager can. The page does not list viewing the member list as a capability for any level.
- [`permissions.list` REST reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/list) — that it lists a file's or shared drive's permissions, and the scopes it accepts; it names no required role.
- [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads) — `alt=media`, `files.export`, and the 10 MB export limit.
- [Resolve errors](https://developers.google.com/workspace/drive/api/guides/handle-errors) — the `appNotAuthorizedToFile`, `insufficientFilePermissions`, `userRateLimitExceeded`, and `rateLimitExceeded` reason strings and the backoff guidance.
- [Drive API usage limits](https://developers.google.com/workspace/drive/api/guides/limits) — the per-minute quota ceilings and the per-method unit costs.
- [Notion integration capabilities](https://developers.notion.com/reference/capabilities) — the capability names and the statement that a connection follows the page access of the user who added it.
- [Notion authorization](https://developers.notion.com/docs/authorization) — internal versus public connections, the OAuth endpoints, and the statement that access to a parent page provides access to all available child pages.
- [Build an internal connection](https://developers.notion.com/guides/get-started/internal-connections) — the sharing steps, the requirement to share before any request succeeds, and the inheritance sentence quoted above.
- [Notion search endpoint](https://developers.notion.com/reference/post-search) — that search covers parent and child pages and data sources shared with a connection, and that results respect the connection's capabilities.
- [Notion API status codes](https://developers.notion.com/reference/status-codes) — the `object_not_found`, `restricted_resource`, `rate_limited`, and `missing_version` error codes.
- [Add and manage connections in Notion](https://www.notion.com/help/add-and-manage-connections-with-the-api) — the `Settings` → `Connections` → `Manage` → `•••` → `Manage page access` path, the **Who can manage page access** values, the ability to restrict which connections members may install, and that both are Enterprise-plan controls.
- [Notion webhook events](https://developers.notion.com/reference/webhooks-events-delivery) — the full event list, which covers pages, databases, data sources, and comments and contains no connection or page-access event.
- [Notion request limits](https://developers.notion.com/reference/request-limits) — the three-requests-per-second average, the per-workspace limit, `Retry-After`, and the payload size caps.
- [Notion changelog](https://developers.notion.com/page/changelog) — the current `Notion-Version` value, the `2025-09-03` data source change, and the `ntn_` token prefix.
