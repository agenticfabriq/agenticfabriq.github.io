"""Diff granted OAuth scopes against the scopes an agent actually exercised.

The audit in three parts:

  1. GRANTED   Google Workspace Directory API, tokens.list:
                 GET https://admin.googleapis.com/admin/directory/v1/users/{userKey}/tokens
               The Token resource carries `clientId` and `scopes`, documented as
               "a list of authorization scopes the application is granted".
               Needs https://www.googleapis.com/auth/admin.directory.user.security

  2. EXERCISED Google Workspace Reports API, OAuth token audit log:
                 GET https://admin.googleapis.com/admin/reports/v1/activity
                     /users/all/applications/token?eventName=activity
               Needs https://www.googleapis.com/auth/admin.reports.audit.readonly

               IMPORTANT: the `activity` event does NOT carry a `scope`
               parameter. Its documented parameters are api_name, app_name,
               client_id, client_type, method_name, num_response_bytes and
               product_bucket. Only the authorize/deny/request/revoke events
               carry `scope`, and those describe grants rather than calls. So
               the join from a called method to the scopes that gate it is
               ours to write -- that is the METHOD_GATES table below.

  3. VERDICT   Two findings, because they have different fixes:
                 UNEXERCISED  granted, and no observed call accepts it. Delete.
                 NARROWABLE   granted and load-bearing, but a scope over the
                              same corpus with strictly smaller REACH would
                              have covered every call it carried. Swap it down.

               Candidates are ranked by reach, not by the provider's capability
               tier, and a candidate must address the same corpus. See the
               comments on SCOPE_PROFILE for why both rules are needed.

Three ways to run it:

    python3 scope_audit.py --demo
        Bundled fixture, no credentials, no network. Prints one full audit and
        one that refuses because of a method the mapping table does not know.

    python3 scope_audit.py --tokens tokens.json --activities activities.json
        Offline diff over JSON somebody else fetched. This is the mode to use
        when a Workspace admin holds the credential and you do not: they run
        two curl commands, you run the diff.

    python3 scope_audit.py --access-token "$ADMIN_TOKEN" \
        --user alice@example.com --client-id 4172.apps.googleusercontent.com
        Live. Requires an admin access token carrying both scopes above.
        Requires: pip install requests

Exit codes: 0 clean, 1 findings, 2 the audit could not be trusted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Iterable, NamedTuple

DIRECTORY_TOKENS_URL = "https://admin.googleapis.com/admin/directory/v1/users/{user}/tokens"
REPORTS_TOKEN_ACTIVITY_URL = (
    "https://admin.googleapis.com/admin/reports/v1/activity/users/{user}/applications/token"
)

# Retention on OAuth Token log events data is 6 months and the documented lag on
# the Token log events row is "a couple of hours" -- not the "up to a few hours"
# on the separate OAuth row. Excluding the most recent day stops a scope being
# scored as unexercised only because its call has not landed in the log yet.
MAX_WINDOW_DAYS = 180
LAG_EXCLUSION_DAYS = 1

UNKNOWN_CORPUS = "unknown"


class AuditInconclusive(Exception):
    """The mapping table cannot explain the evidence, so no delta is reportable.

    An unmapped method could require any scope, including the one we are about
    to recommend deleting. Failing loudly beats reporting a confident and wrong
    delta -- this refusal is the reason the rest of the output is trustworthy.
    """

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = sorted(reasons)
        super().__init__("; ".join(self.reasons))


class ScopeProfile(NamedTuple):
    """reach: smaller is less reachable. corpus: WHICH resources, not how many."""

    reach: int
    classification: str
    corpus: str
    reaches: str


# TWO independent properties decide whether one scope may replace another.
#
# REACH ranks how much of a corpus a scope exposes. Rank by reach, not by the
# provider's capability tier: drive.metadata.readonly sits one rung below
# drive.readonly on Google's ladder but still enumerates every file in
# corpora=user, so that swap barely moves the blast radius, while drive.file is
# non-sensitive and bounded to what the user hands over.
#
# CORPUS names WHICH resources a scope addresses. Without it, a plain
# minimum-by-reach recommends drive.appdata for files.list -- it really is on
# that method's accepted-scope list and really does have the smallest reach --
# and it would break the agent instantly, because it addresses the app's own
# hidden folder and none of the user's files. A candidate must share the
# corpus of the scope it replaces. That is a structural rule rather than a
# denylist, so a special-purpose scope added later cannot slip through by
# being given a low rank.
#
# The reach ranks and the corpus labels are both judgements about the data
# model and you should edit them for yours. The `classification` column is the
# provider's own, from the Drive and Gmail scope references.
SCOPE_PROFILE: dict[str, ScopeProfile] = {
    # --- the user's own Drive corpus ---
    "https://www.googleapis.com/auth/drive.file": ScopeProfile(
        1, "non-sensitive", "user-drive", "only files the user hands over via the picker"),
    "https://www.googleapis.com/auth/drive.metadata.readonly": ScopeProfile(
        2, "restricted", "user-drive", "metadata for every file in corpora=user"),
    "https://www.googleapis.com/auth/drive.metadata": ScopeProfile(
        3, "restricted", "user-drive", "metadata for every file in corpora=user, read and write"),
    "https://www.googleapis.com/auth/drive.readonly": ScopeProfile(
        4, "restricted", "user-drive", "content of every file in corpora=user"),
    "https://www.googleapis.com/auth/drive": ScopeProfile(
        5, "restricted", "user-drive", "every file in corpora=user, including delete"),
    # --- disjoint corpora: never substitutes for anything above ---
    "https://www.googleapis.com/auth/drive.appdata": ScopeProfile(
        1, "non-sensitive", "app-private", "the app's own hidden configuration folder only"),
    "https://www.googleapis.com/auth/drive.appfolder": ScopeProfile(
        1, "non-sensitive", "app-private", "the app's own hidden configuration folder only"),
    "https://www.googleapis.com/auth/drive.meet.readonly": ScopeProfile(
        1, "restricted", "meet-drive", "only Drive files created or edited by Google Meet"),
    "https://www.googleapis.com/auth/drive.photos.readonly": ScopeProfile(
        1, "restricted", "photos-drive", "only photos stored in Drive"),
    # --- the mailbox ---
    "https://www.googleapis.com/auth/gmail.send": ScopeProfile(
        1, "sensitive", "mailbox", "nothing that already exists; send only"),
    "https://www.googleapis.com/auth/gmail.compose": ScopeProfile(
        2, "restricted", "mailbox", "drafts and sending, but not reading the inbox"),
    "https://www.googleapis.com/auth/gmail.metadata": ScopeProfile(
        3, "restricted", "mailbox", "headers and labels for every message, never bodies"),
    "https://www.googleapis.com/auth/gmail.readonly": ScopeProfile(
        4, "restricted", "mailbox", "every message in the mailbox"),
    "https://www.googleapis.com/auth/gmail.modify": ScopeProfile(
        5, "restricted", "mailbox", "every message, plus compose, send and trash"),
    "https://mail.google.com/": ScopeProfile(
        6, "restricted", "mailbox", "every message, including permanent deletion"),
}

# method -> every scope the provider accepts for it.
#
# The right-hand lists below are COPIED VERBATIM from the "Authorization
# scopes" block on each method's own reference page, in the order the page
# prints them. Do not curate them. A scope left out of this table is a scope
# the audit will report as UNEXERCISED, which means recommending the deletion
# of something the observed calls genuinely require -- the exact
# confident-and-wrong delta AuditInconclusive exists to prevent.
#
# Build the left column from your own log output: Google documents that
# `method_name` exists and is a string, but not the format it emits, so these
# keys match the bundled fixture rather than a verified literal.
METHOD_GATES: dict[str, tuple[str, ...]] = {
    # developers.google.com/workspace/drive/api/reference/rest/v3/files/list
    "drive.files.list": (
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.appdata",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.meet.readonly",
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.photos.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
    # developers.google.com/workspace/drive/api/reference/rest/v3/files/get
    "drive.files.get": (
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.appdata",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.meet.readonly",
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.photos.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
    # developers.google.com/workspace/drive/api/reference/rest/v3/files/export
    "drive.files.export": (
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.meet.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
    # developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list
    "gmail.users.messages.list": (
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.metadata",
    ),
    # developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get
    "gmail.users.messages.get": (
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.metadata",
    ),
    # developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
    "gmail.users.messages.send": (
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ),
}

# DERIVED, not copied. Google publishes one accepted-scope list for files.get
# and does not split it by `alt`, but a content download is not a metadata
# read, so the metadata-only scopes cannot satisfy it. This row is files.get's
# published list minus drive.metadata and drive.metadata.readonly. It is a
# judgement, flagged as one, and it is the only row here that is not verbatim.
METHOD_GATES["drive.files.get.alt=media"] = tuple(
    scope
    for scope in METHOD_GATES["drive.files.get"]
    if scope
    not in {
        "https://www.googleapis.com/auth/drive.metadata",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    }
)

DERIVED_GATES = {"drive.files.get.alt=media"}

# Sign-in scopes. They are granted by the identity flow rather than by a
# feature, so they are reported separately instead of being recommended for
# deletion by a tool that only watches Drive and Gmail methods.
IDENTITY_SCOPES = {
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
}


def profile(scope: str) -> ScopeProfile | None:
    return SCOPE_PROFILE.get(scope)


def describe(scope: str) -> str:
    found = profile(scope)
    if not found:
        return "unclassified -- add it to SCOPE_PROFILE before acting on it"
    return f"{found.classification}; {found.corpus}; reaches {found.reaches}"


def can_substitute(candidate: str, current: str) -> bool:
    """May `candidate` replace `current`?

    Only when both are profiled, both address the same known corpus, and the
    candidate reaches strictly less of it. An unprofiled scope can never win,
    which is the safe default: the tool declines to recommend what it cannot
    describe.
    """
    new, old = profile(candidate), profile(current)
    if new is None or old is None:
        return False
    if UNKNOWN_CORPUS in (new.corpus, old.corpus):
        return False
    return new.corpus == old.corpus and new.reach < old.reach


class Audit:
    """The verdict for one client id over one evidence window."""

    def __init__(
        self,
        granted: set[str],
        unexercised: set[str],
        narrowable: dict[str, str],
        identity: set[str],
    ) -> None:
        self.granted = granted
        self.unexercised = unexercised
        self.narrowable = narrowable
        self.identity = identity

    @property
    def clean(self) -> bool:
        return not self.unexercised and not self.narrowable


def check_mappable(exercised_methods: Iterable[str]) -> None:
    """Refuse the audit unless every observed method is in METHOD_GATES."""
    unmapped = sorted(m for m in set(exercised_methods) if m not in METHOD_GATES)
    if unmapped:
        raise AuditInconclusive(f"no METHOD_GATES entry for {method}" for method in unmapped)


def audit(granted: set[str], exercised_methods: Iterable[str]) -> Audit:
    """Split a granted scope set into load-bearing, narrowable, and unexercised."""
    methods = set(exercised_methods)
    check_mappable(methods)
    identity = granted & IDENTITY_SCOPES
    audited = granted - IDENTITY_SCOPES

    load_bearing: set[str] = set()
    narrowable: dict[str, str] = {}

    for scope in sorted(audited):
        carried = {m for m in methods if scope in METHOD_GATES[m]}
        if not carried:
            continue
        load_bearing.add(scope)

        # Every scope that would have covered this scope's entire workload...
        alternatives = set.intersection(*(set(METHOD_GATES[m]) for m in carried))
        # ...restricted to genuine substitutes: same corpus, smaller reach.
        candidates = [a for a in alternatives if can_substitute(a, scope)]
        if candidates:
            narrowable[scope] = min(candidates, key=lambda s: (profile(s).reach, s))

    if methods and not load_bearing:
        # Calls succeeded that nothing in the granted set explains, so the
        # granted set is stale or the mapping is wrong. Either way the verdict
        # below would be guesswork.
        raise AuditInconclusive(["no granted scope accepts any observed method"])

    return Audit(
        granted=granted,
        unexercised=audited - load_bearing,
        narrowable=narrowable,
        identity=identity,
    )


def _get(url: str, access_token: str, params: dict[str, Any]) -> dict[str, Any]:
    import requests  # imported lazily so --demo and offline modes need no deps

    for attempt in range(5):
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
        if response.status_code in (429, 500, 502, 503) and attempt < 4:
            # Both Admin SDK APIs throttle. Honour Retry-After when it is sent
            # rather than replacing it with a backoff curve of our own.
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else 2**attempt)
            continue
        if response.status_code == 403:
            raise SystemExit(
                "403 from the Admin SDK. The audit itself needs "
                "admin.directory.user.security and admin.reports.audit.readonly; "
                "ask an admin to run the two reads rather than granting yourself "
                "standing access.\n" + response.text
            )
        response.raise_for_status()
        return response.json()
    raise SystemExit(f"gave up after 5 attempts: {url}")


def _rfc3339(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def fetch_granted(access_token: str, user: str, client_id: str | None) -> dict[str, Any]:
    body = _get(DIRECTORY_TOKENS_URL.format(user=user), access_token, {})
    if client_id:
        body["items"] = [t for t in body.get("items", []) if t.get("clientId") == client_id]
    return body


def fetch_activities(access_token: str, user: str, days: int) -> dict[str, Any]:
    if days > MAX_WINDOW_DAYS:
        raise SystemExit(
            f"OAuth log events are retained for {MAX_WINDOW_DAYS} days; {days} is beyond it"
        )
    now = time.time()
    params: dict[str, Any] = {
        "eventName": "activity",
        "maxResults": 1000,
        "startTime": _rfc3339(now - days * 86400),
        "endTime": _rfc3339(now - LAG_EXCLUSION_DAYS * 86400),
    }
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        body = _get(REPORTS_TOKEN_ACTIVITY_URL.format(user=user), access_token, params)
        items.extend(body.get("items", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            return {"items": items}


def granted_scopes(tokens_body: dict[str, Any], client_id: str | None) -> set[str]:
    """Union of `scopes` across every Token item, optionally one client only."""
    scopes: set[str] = set()
    for token in tokens_body.get("items", []):
        if client_id and token.get("clientId") != client_id:
            continue
        scopes.update(token.get("scopes", []))
    return scopes


def exercised_methods(activities_body: dict[str, Any], client_id: str | None) -> set[str]:
    """Pull method_name out of `activity` events for one client.

    Reports API activities carry a list of `events`, each with a list of
    `parameters` shaped {"name": ..., "value": ...}.
    """
    methods: set[str] = set()
    for activity in activities_body.get("items", []):
        for event in activity.get("events", []):
            if event.get("name") != "activity":
                continue
            params = {p.get("name"): p.get("value") for p in event.get("parameters", [])}
            if client_id and params.get("client_id") != client_id:
                continue
            method = params.get("method_name")
            if method:
                methods.add(method)
    return methods


def report(granted: set[str], methods: set[str]) -> int:
    print(f"granted scopes ({len(granted)}):")
    for scope in sorted(granted):
        print(f"  {scope}\n      {describe(scope)}")

    print(f"\nexercised methods ({len(methods)}):")
    for method in sorted(methods):
        accepted = METHOD_GATES.get(method)
        if not accepted:
            print(f"  {method}  ->  ?? UNMAPPED")
            continue
        note = " (derived, not copied)" if method in DERIVED_GATES else ""
        print(f"  {method}  ->  {len(accepted)} accepted scopes{note}")
    if not methods:
        print("  (none in this window -- check the window before concluding anything)")

    try:
        result = audit(granted, methods)
    except AuditInconclusive as error:
        sys.stdout.flush()  # keep the two streams interleaved in the right order
        print("\nAUDIT INCONCLUSIVE:", file=sys.stderr)
        for reason in error.reasons:
            print(f"  {reason}", file=sys.stderr)
        print(
            "\nFill in METHOD_GATES from the provider's per-method scope reference\n"
            "before trusting any verdict. A partial mapping silently under-reports.",
            file=sys.stderr,
        )
        return 2

    if result.identity:
        print(f"\nidentity scopes, not audited ({len(result.identity)}):")
        for scope in sorted(result.identity):
            print(f"  {scope}")

    if result.unexercised:
        print(f"\nUNEXERCISED ({len(result.unexercised)}) -- no observed call accepts these:")
        for scope in sorted(result.unexercised):
            print(f"  {scope}\n      {describe(scope)}")

    if result.narrowable:
        print(f"\nNARROWABLE ({len(result.narrowable)}) -- smaller reach would have sufficed:")
        for broad in sorted(result.narrowable):
            narrow = result.narrowable[broad]
            print(f"  {broad}\n      from: {describe(broad)}")
            print(f"    -> {narrow}\n      to:   {describe(narrow)}")
            if profile(narrow).classification == "restricted":
                print("      NOTE: the recommendation is still restricted. Partial win --")
                print("            re-check whether a non-sensitive scope covers the workload.")
        print(
            "\n  Every recommendation is a candidate to test, not a fact. An accepted-scope\n"
            "  list says a call is permitted; it does not promise the response still\n"
            "  contains the fields your code reads. Run the narrower scope against a real\n"
            "  request before you ship it."
        )

    if result.clean:
        print("\nclean: every granted scope was needed, at the reach it was granted")
        return 0

    print(
        "\nBefore acting: confirm the window covered every periodic job this agent\n"
        "runs. Inside 30 days a quarterly task looks exactly like a dead one."
    )
    return 1


DEMO_CLIENT_ID = "4172.apps.googleusercontent.com"

DEMO_TOKENS = {
    "kind": "admin#directory#tokenList",
    "items": [
        {
            "clientId": DEMO_CLIENT_ID,
            "displayText": "meeting-brief-agent",
            "nativeApp": False,
            "anonymous": False,
            "userKey": "112233",
            "scopes": [
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/gmail.send",
                "openid",
            ],
        }
    ],
}


def _activity(api_name: str, method_name: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "auth",
                "name": "activity",
                "parameters": [
                    {"name": "client_id", "value": DEMO_CLIENT_ID},
                    {"name": "api_name", "value": api_name},
                    {"name": "method_name", "value": method_name},
                ],
            }
        ]
    }


DEMO_ACTIVITIES = {
    "items": [
        _activity("drive", "drive.files.list"),
        _activity("drive", "drive.files.get"),
    ]
}

DEMO_ACTIVITIES_UNMAPPED = {
    "items": DEMO_ACTIVITIES["items"] + [_activity("drive", "drive.permissions.list")]
}

# A grant whose only Drive scope addresses a disjoint corpus. The calls really
# do accept it, so it must NOT be reported as unexercised -- that would be the
# tool recommending the deletion of a scope the observed calls require.
DEMO_TOKENS_DISJOINT = {
    "items": [
        {
            "clientId": DEMO_CLIENT_ID,
            "displayText": "meet-recap-agent",
            "scopes": ["https://www.googleapis.com/auth/drive.meet.readonly"],
        }
    ],
}


def run_demo() -> int:
    granted = granted_scopes(DEMO_TOKENS, DEMO_CLIENT_ID)

    print("=" * 72)
    print("DEMO 1 -- a mapped audit, with both kinds of finding")
    print("=" * 72)
    first = report(granted, exercised_methods(DEMO_ACTIVITIES, DEMO_CLIENT_ID))
    print(f"\n(exit code would be {first})\n")

    print("=" * 72)
    print("DEMO 2 -- same grant, one method the table does not know")
    print("=" * 72)
    second = report(granted, exercised_methods(DEMO_ACTIVITIES_UNMAPPED, DEMO_CLIENT_ID))
    print(f"\n(exit code would be {second}: refused rather than guessed)\n")

    print("=" * 72)
    print("DEMO 3 -- a disjoint-corpus scope the calls genuinely need")
    print("=" * 72)
    third = report(
        granted_scopes(DEMO_TOKENS_DISJOINT, DEMO_CLIENT_ID),
        exercised_methods(DEMO_ACTIVITIES, DEMO_CLIENT_ID),
    )
    print(f"\n(exit code would be {third}: load-bearing, and never swapped across corpora)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true", help="run the bundled fixture, no network")
    parser.add_argument("--tokens", help="path to a tokens.list JSON response")
    parser.add_argument("--activities", help="path to an activities.list JSON response")
    parser.add_argument("--access-token", help="admin access token, for live mode")
    parser.add_argument("--user", default="all", help="userKey, or 'all' for the activity log")
    parser.add_argument("--client-id", help="restrict the audit to one OAuth client id")
    parser.add_argument("--days", type=int, default=90, help="evidence window in days, max 180")
    args = parser.parse_args()

    if args.demo:
        return run_demo()

    if args.tokens and args.activities:
        with open(args.tokens, encoding="utf-8") as handle:
            tokens_body = json.load(handle)
        with open(args.activities, encoding="utf-8") as handle:
            activities_body = json.load(handle)
    elif args.access_token:
        if args.user == "all":
            raise SystemExit("live mode needs --user <email>: tokens.list is per user")
        tokens_body = fetch_granted(args.access_token, args.user, args.client_id)
        activities_body = fetch_activities(args.access_token, args.user, args.days)
    else:
        parser.error("pass --demo, or --tokens with --activities, or --access-token")
        return 2

    return report(
        granted_scopes(tokens_body, args.client_id),
        exercised_methods(activities_body, args.client_id),
    )


if __name__ == "__main__":
    raise SystemExit(main())
