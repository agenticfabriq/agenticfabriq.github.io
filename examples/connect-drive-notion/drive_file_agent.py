"""Read Google Drive from an agent using only the drive.file scope.

Consent is the same authorization-code + PKCE flow shown in
examples/connect-gmail/oauth_pkce_consent.py. Only the scope string changes:

    https://www.googleapis.com/auth/drive.file

Under drive.file the agent can only touch files the user handed it -- files the
app created, or files the user selected in the Google Picker. Every other file
in the Drive is invisible to files.list and returns 403 appNotAuthorizedToFile
on a direct files.get. That boundary is the point of this script: it does not
try to enumerate a Drive, it works from ids the Picker produced.

Environment:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN   refresh token from a drive.file consent
    DRIVE_FILE_IDS         optional: comma-separated ids the Picker returned
    DRIVE_ID               optional: shared drive id, if the files live in one

Run:
    python3 drive_file_agent.py
"""

from __future__ import annotations

import os
import random
import sys
import time
from typing import Any

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_V3 = "https://www.googleapis.com/drive/v3"

# https://developers.google.com/workspace/drive/api/guides/mime-types
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# Google Workspace editor files have no bytes to download; they must be
# exported to a concrete format.
EXPORT_AS = {
    GOOGLE_DOC_MIME: "text/plain",
    GOOGLE_SHEET_MIME: "text/csv",
}

# "Exported content is limited to 10 MB."
# https://developers.google.com/workspace/drive/api/guides/manage-downloads
EXPORT_SIZE_LIMIT_BYTES = 10 * 1024 * 1024

# A partial read must never be able to pass for a whole one. Exporting a
# spreadsheet to text/csv returns the first sheet; the caller -- and any model
# downstream of the caller -- gets told so in the payload itself.
PARTIAL_SHEET_MARKER = (
    "[PARTIAL READ] Exported as text/csv, which returns the first sheet only. "
    "Any other sheets in this workbook were NOT read. Use the Sheets API to "
    "address ranges per sheet before drawing a conclusion from these numbers.\n"
)

# Retry only these. Everything else is a decision, not a hiccup.
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class GrantRevoked(RuntimeError):
    """The grant is permanently dead. Re-consent, do not retry."""


class NotOurFile(RuntimeError):
    """The drive.file grant does not cover this id."""


class ExportTooLarge(RuntimeError):
    """The export failed and the 10 MB export limit is the likely cause."""


class DriveError(RuntimeError):
    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(f"{status} {reason}: {message}")
        self.status = status
        self.reason = reason


def refresh_access_token(refresh_token: str) -> tuple[str, int]:
    """Trade the durable refresh token for a short-lived access token."""
    response = requests.post(
        TOKEN_URL,
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


class AccessToken:
    """A short-lived access token that can renew itself mid-run.

    Access tokens expire on a timer that has nothing to do with how long your
    walk takes, so a long crawl WILL cross the boundary. Holding the refresh
    token here is what lets call() recover from a 401 without unwinding, and
    what lets a genuine revocation surface as GrantRevoked from anywhere in
    the run rather than only from the first refresh.
    """

    def __init__(self, refresh_token: str) -> None:
        self.refresh_token = refresh_token
        self.value = ""
        self.expires_in = 0
        self.renew()

    def renew(self) -> None:
        self.value, self.expires_in = refresh_access_token(self.refresh_token)


def _reason(body: dict[str, Any]) -> str:
    errors = body.get("error", {}).get("errors") or [{}]
    return errors[0].get("reason", "")


def call(
    token: AccessToken,
    path: str,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    """One Drive GET with backoff on throttling and no backoff on anything else."""
    url = f"{DRIVE_V3}{path}"
    refreshed = False

    for attempt in range(5):
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token.value}"},
            params=params,
            timeout=60,
        )
        if response.ok:
            return response

        try:
            body = response.json()
        except ValueError:
            body = {}
        reason = _reason(body)
        message = body.get("error", {}).get("message", response.text[:200])

        if response.status_code == 401:
            if refreshed:
                # We minted a token seconds ago and it is already rejected.
                # That is not expiry, that is a dead grant.
                raise GrantRevoked(f"401 after a fresh token: {message}")
            # Either the token aged out mid-run or the user revoked. renew()
            # raises GrantRevoked on invalid_grant, which is exactly how a
            # mid-run revocation reaches the handler in main().
            token.renew()
            refreshed = True
            continue

        if reason == "appNotAuthorizedToFile":
            # The user never handed this file to this app. No retry, no
            # widening of scope: send them back to the Picker.
            raise NotOurFile(message)

        throttled = response.status_code == 429 or (
            response.status_code == 403 and reason in RETRYABLE_REASONS
        )
        if (throttled or response.status_code >= 500) and attempt < 4:
            time.sleep((2**attempt) + random.random())
            continue

        raise DriveError(response.status_code, reason, message)

    raise DriveError(429, "rateLimitExceeded", "gave up after 5 attempts")


def get_metadata(token: AccessToken, file_id: str) -> dict[str, Any]:
    """Metadata for one file. Ask for fields explicitly; the default set is thin."""
    return call(
        token,
        f"/files/{file_id}",
        params={
            "fields": "id,name,mimeType,size,modifiedTime,parents,owners(emailAddress),webViewLink",
            "supportsAllDrives": "true",
        },
    ).json()


def list_children(
    token: AccessToken,
    folder_id: str,
    page_size: int = 100,
    corpora: str = "user",
    drive_id: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Children of a folder, and whether the listing is complete.

    Under drive.file this returns only the children the app itself was given.
    A folder the user picked does not guarantee its contents are readable --
    check the result length against what the user sees in the Drive UI rather
    than assuming the subtree came with it.

    Google's files.list reference says to "Prefer user or drive to allDrives
    for efficiency", so the default here is the user corpus; pass a drive_id
    when the files live in a shared drive.

    The second return value is the negation of Drive's incompleteSearch flag.
    A script whose whole job is counting what you were given must not report a
    truncated listing as an inventory.
    """
    items: list[dict[str, Any]] = []
    complete = True
    page_token: str | None = None

    if drive_id and corpora == "user":
        corpora = "drive"

    while True:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, incompleteSearch, files(id,name,mimeType,size)",
            "pageSize": page_size,
            "corpora": corpora,
            "supportsAllDrives": "true",
        }
        if corpora in ("drive", "allDrives", "domain"):
            # Shared-drive items are excluded unless this is set alongside
            # supportsAllDrives.
            params["includeItemsFromAllDrives"] = "true"
        if drive_id:
            params["driveId"] = drive_id
        if page_token:
            params["pageToken"] = page_token

        body = call(token, "/files", params=params).json()
        items.extend(body.get("files", []))
        if body.get("incompleteSearch"):
            complete = False
        page_token = body.get("nextPageToken")
        if not page_token:
            return items, complete


def read_text(
    token: AccessToken, file_id: str, mime_type: str, limit: int = 20_000
) -> str:
    """Return at most `limit` characters of a file's text.

    Truncation happens here, before the content can reach a model prompt.
    Doing it after the fetch is not the same thing: the untruncated bytes have
    already been through your process, your logs, and your retry buffers.
    """
    if mime_type == FOLDER_MIME:
        return ""

    export_mime = EXPORT_AS.get(mime_type)
    if export_mime is not None:
        try:
            response = call(
                token, f"/files/{file_id}/export", params={"mimeType": export_mime}
            )
        except DriveError as error:
            # Drive's docs state the 10 MB export limit but do not publish the
            # error reason string returned when a document crosses it, so this
            # branches on the export failing rather than on a literal that
            # could not be verified against a Google page. Either way the
            # caller gets a named exception and the route that does work.
            if error.status in (403, 400):
                raise ExportTooLarge(
                    f"export of {file_id} as {export_mime} failed ({error}). "
                    "Exported content is limited to 10 MB; for larger content "
                    "use files.download with a long-running operation."
                ) from error
            raise

        if len(response.content) >= EXPORT_SIZE_LIMIT_BYTES:
            raise ExportTooLarge(
                f"export of {file_id} hit the 10 MB export limit; "
                "use files.download with a long-running operation."
            )

        text = response.text[:limit]
        if mime_type == GOOGLE_SHEET_MIME:
            return PARTIAL_SHEET_MARKER + text
        return text

    if mime_type.startswith("text/") or mime_type == "application/json":
        return call(token, f"/files/{file_id}", params={"alt": "media"}).text[:limit]

    # Binary. Do not pull the bytes speculatively.
    return f"<{mime_type}: not fetched>"


def who_else_can_see_it(token: AccessToken, file_id: str) -> list[str]:
    """Who the file is shared with, as far as the grant can tell.

    A 403 here is normal and is not a failure: the permissions list is readable
    by writers and owners, and a read-only grant is neither.
    """
    try:
        body = call(
            token,
            f"/files/{file_id}/permissions",
            params={
                "fields": "permissions(id,type,role,emailAddress,domain)",
                "supportsAllDrives": "true",
            },
        ).json()
    except DriveError as error:
        if error.status == 403:
            return ["<permissions not visible to this grant>"]
        raise

    out = []
    for permission in body.get("permissions", []):
        who = (
            permission.get("emailAddress")
            or permission.get("domain")
            or permission.get("type", "?")
        )
        out.append(f"{permission.get('role', '?')}:{who}")
    return out


def run(token: AccessToken, file_ids: list[str], drive_id: str | None) -> None:
    for file_id in file_ids:
        try:
            meta = get_metadata(token, file_id)
        except NotOurFile as error:
            print(f"{file_id}: outside the drive.file grant -- {error}")
            continue

        print(f"\n{meta['name']}  [{meta['mimeType']}]  {meta['id']}")
        print(f"  shared with: {', '.join(who_else_can_see_it(token, file_id)) or 'nobody else'}")

        if meta["mimeType"] == FOLDER_MIME:
            children, complete = list_children(token, file_id, drive_id=drive_id)
            print(f"  {len(children)} child item(s) reachable through this grant:")
            for child in children:
                print(f"    - {child['name']} [{child['mimeType']}]")
            if not complete:
                print(
                    "  WARNING: Drive returned incompleteSearch=true. This listing "
                    "is not a complete inventory; do not treat the count as one."
                )
            continue

        try:
            text = read_text(token, file_id, meta["mimeType"])
        except ExportTooLarge as error:
            print(f"  skipped: {error}")
            continue

        first_line = text.strip().splitlines()[0] if text.strip() else "<empty>"
        print(f"  first line: {first_line[:120]}")


def main() -> None:
    raw_ids = os.environ.get("DRIVE_FILE_IDS", "").strip()
    if not raw_ids:
        sys.exit(
            "Set DRIVE_FILE_IDS to ids the Google Picker returned. Under "
            "drive.file there is no way to discover ids you were not given."
        )
    file_ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
    drive_id = os.environ.get("DRIVE_ID") or None

    try:
        token = AccessToken(os.environ["GOOGLE_REFRESH_TOKEN"])
    except GrantRevoked as error:
        sys.exit(f"grant is dead, re-consent required: {error}")

    print(f"access token good for {token.expires_in}s; call() renews it on a 401")
    try:
        run(token, file_ids, drive_id)
    except GrantRevoked as error:
        # Reachable: call() raises this when a 401 leads to a refresh that
        # comes back invalid_grant, which is a revocation partway through the
        # walk. Stop here. Do not write partial results as though the task
        # completed; what the agent already read still happened and still
        # belongs in your audit trail.
        sys.exit(f"grant revoked mid-run, stopping: {error}")


if __name__ == "__main__":
    main()
