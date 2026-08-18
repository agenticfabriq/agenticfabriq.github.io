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
               the join from a called method to the scope that gates it is
               ours to write -- that is the METHOD_GATES table below.

  3. VERDICT   Two findings, because they have different fixes:
                 UNEXERCISED  granted, and no observed call needed it. Delete.
                 NARROWABLE   granted, load-bearing only because a narrower
                              scope was never granted. Swap it down.

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
from typing import Any, Iterable

DIRECTORY_TOKENS_URL = "https://admin.googleapis.com/admin/directory/v1/users/{user}/tokens"
REPORTS_TOKEN_ACTIVITY_URL = (
    "https://admin.googleapis.com/admin/reports/v1/activity/users/{user}/applications/token"
)

# Retention on OAuth log events is 6 months and the documented lag is "up to a
# few hours". Excluding the most recent day stops a scope being scored as
# unexercised only because its call has not landed in the log yet.
MAX_WINDOW_DAYS = 180
LAG_EXCLUSION_DAYS = 1


class AuditInconclusive(Exception):
    """The mapping table cannot explain the evidence, so no delta is reportable.

    An unmapped method could require any scope, including the one we are about
    to recommend deleting. Failing loudly beats reporting a confident and wrong
    delta -- this refusal is the reason the rest of the output is trustworthy.
    """

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = sorted(reasons)
        super().__init__("; ".join(self.reasons))


# Method -> the NARROWEST scope that satisfies it.
#
# Build the left column from your own log output. The exact string format
# Google emits in `method_name` is not documented on the audit-event reference,
# so the keys below are the shape used by the bundled fixture and not a
# verified literal. Mapping each method to the narrowest satisfying scope
# (rather than to any scope that would work) is what makes the delta
# meaningful: map to the broadest and the audit always passes.
METHOD_GATES: dict[str, str] = {
    "drive.files.list": "https://www.googleapis.com/auth/drive.metadata.readonly",
    "drive.files.get": "https://www.googleapis.com/auth/drive.metadata.readonly",
    # Content download is a different requirement from metadata even though the
    # method name is the same. Keep the variants as separate rows, or the audit
    # will score drive.readonly as exercised by a metadata-only summariser.
    "drive.files.get.alt=media": "https://www.googleapis.com/auth/drive.readonly",
    "drive.files.export": "https://www.googleapis.com/auth/drive.readonly",
    "gmail.users.messages.list": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.users.messages.get": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.users.messages.send": "https://www.googleapis.com/auth/gmail.send",
}

# Broad scope -> the narrower scopes it subsumes. Used only to work out which
# granted scope was actually carrying a call; it never excuses the broad grant.
SCOPE_IMPLIES: dict[str, set[str]] = {
    "https://www.googleapis.com/auth/drive": {
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
        "https://www.googleapis.com/auth/drive.file",
    },
    "https://www.googleapis.com/auth/drive.readonly": {
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    },
    "https://mail.google.com/": {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    },
    "https://www.googleapis.com/auth/gmail.modify": {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    },
}

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


class Audit:
    """The verdict for one client id over one evidence window."""

    def __init__(
        self,
        granted: set[str],
        required: set[str],
        unexercised: set[str],
        narrowable: dict[str, set[str]],
        identity: set[str],
    ) -> None:
        self.granted = granted
        self.required = required
        self.unexercised = unexercised
        self.narrowable = narrowable
        self.identity = identity

    @property
    def clean(self) -> bool:
        return not self.unexercised and not self.narrowable


def required_scopes(exercised_methods: Iterable[str]) -> set[str]:
    """Narrowest scope set that would have satisfied every observed call."""
    methods = set(exercised_methods)
    unmapped = sorted(method for method in methods if method not in METHOD_GATES)
    if unmapped:
        raise AuditInconclusive(f"no METHOD_GATES entry for {method}" for method in unmapped)
    return {METHOD_GATES[method] for method in methods}


def audit(granted: set[str], exercised_methods: Iterable[str]) -> Audit:
    """Split a granted scope set into load-bearing, narrowable, and unexercised."""
    required = required_scopes(exercised_methods)
    identity = granted & IDENTITY_SCOPES
    audited = granted - IDENTITY_SCOPES

    load_bearing: set[str] = set()
    narrowable: dict[str, set[str]] = {}

    for scope in sorted(required):
        if scope in audited:
            # The narrow scope was granted and used. Nothing to say about it.
            load_bearing.add(scope)
            continue
        covering = sorted(g for g in audited if scope in SCOPE_IMPLIES.get(g, set()))
        if not covering:
            # A call succeeded that nothing in the granted set explains, so the
            # granted set is stale or the mapping is wrong. Either way the
            # delta below would be guesswork.
            raise AuditInconclusive([f"no granted scope covers {scope}"])
        for broad in covering:
            load_bearing.add(broad)
            narrowable.setdefault(broad, set()).add(scope)

    return Audit(
        granted=granted,
        required=required,
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
        print(f"  {scope}")
    print(f"\nexercised methods ({len(methods)}):")
    for method in sorted(methods):
        print(f"  {method}  ->  {METHOD_GATES.get(method, '?? UNMAPPED')}")
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
            "before trusting any delta. A partial mapping silently under-reports.",
            file=sys.stderr,
        )
        return 2

    if result.identity:
        print(f"\nidentity scopes, not audited ({len(result.identity)}):")
        for scope in sorted(result.identity):
            print(f"  {scope}")

    if result.unexercised:
        print(f"\nUNEXERCISED ({len(result.unexercised)}) -- no observed call needed these:")
        for scope in sorted(result.unexercised):
            print(f"  {scope}")

    if result.narrowable:
        print(f"\nNARROWABLE ({len(result.narrowable)}) -- broader than the calls required:")
        for broad in sorted(result.narrowable):
            for narrow in sorted(result.narrowable[broad]):
                print(f"  {broad}\n    -> {narrow}")

    if result.clean:
        print("\nclean: every granted scope was needed, at the width it was granted")
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
    print(f"\n(exit code would be {second}: refused rather than guessed)")
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
