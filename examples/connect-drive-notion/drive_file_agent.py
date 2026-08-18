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
# exported to a concrete format. Exported content is limited to 10 MB.
EXPORT_AS = {
    GOOGLE_DOC_MIME: "text/plain",
    GOOGLE_SHEET_MIME: "text/csv",  # first sheet only
}

# Retry only these. Everything else is a decision, not a hiccup.
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class GrantRevoked(RuntimeError):
    """The refresh token is permanently dead. Re-consent, do not retry."""


class NotOurFile(RuntimeError):
    """The drive.file grant does not cover this id."""


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


def _reason(body: dict[str, Any]) -> str:
    errors = body.get("error", {}).get("errors") or [{}]
    return errors[0].get("reason", "")


def call(
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
    stream: bool = False,
) -> requests.Response:
    """One Drive GET with backoff on throttling and no backoff on anything else."""
    url = f"{DRIVE_V3}{path}"
    headers = {"Authorization": f"Bearer {token}"}

    for attempt in range(5):
        response = requests.get(
            url, headers=headers, params=params, timeout=60, stream=stream
        )
        if response.ok:
            return response

        try:
            body = response.json()
        except ValueError:
            body = {}
        reason = _reason(body)
        message = body.get("error", {}).get("message", response.text[:200])

        if reason == "appNotAuthorizedToFile":
            # The user never handed this file to this app. No retry, no
            # widening of scope: send them back to the Picker.
            raise NotOurFile(message)

        throttled = response.status_code == 429 or (
            response.status_code == 403 and reason in RETRYABLE_REASONS
        )
        if throttled and attempt < 4:
            time.sleep((2**attempt) + random.random())
            continue
        if response.status_code >= 500 and attempt < 4:
            time.sleep((2**attempt) + random.random())
            continue

        raise DriveError(response.status_code, reason, message)

    raise DriveError(429, "rateLimitExceeded", "gave up after 5 attempts")


def get_metadata(token: str, file_id: str) -> dict[str, Any]:
    """Metadata for one file. Ask for fields explicitly; the default set is thin."""
    return call(
        token,
        f"/files/{file_id}",
        params={
            "fields": "id,name,mimeType,size,modifiedTime,parents,owners(emailAddress),webViewLink",
            "supportsAllDrives": "true",
        },
    ).json()


def list_children(token: str, folder_id: str, page_size: int = 100) -> list[dict[str, Any]]:
    """Children of a folder, if the grant reaches them.

    Under drive.file this returns only the children the app itself was given.
    A folder the user picked does not guarantee its contents are readable --
    check the result length against what the user sees in the Drive UI rather
    than assuming the subtree came with it.
    """
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id,name,mimeType,size)",
            "pageSize": page_size,
            # Shared-drive items are excluded unless all three are set.
            "corpora": "allDrives",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        body = call(token, "/files", params=params).json()
        items.extend(body.get("files", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            return items


def read_text(token: str, file_id: str, mime_type: str, limit: int = 20_000) -> str:
    """Return at most `limit` characters of a file's text.

    Truncation happens here, before the content can reach a model prompt.
    Doing it after the fetch is not the same thing: the untruncated bytes have
    already been through your process, your logs, and your retry buffers.
    """
    if mime_type == FOLDER_MIME:
        return ""

    export_mime = EXPORT_AS.get(mime_type)
    if export_mime is not None:
        response = call(
            token, f"/files/{file_id}/export", params={"mimeType": export_mime}
        )
    elif mime_type.startswith("text/") or mime_type == "application/json":
        response = call(token, f"/files/{file_id}", params={"alt": "media"})
    else:
        # Binary. Do not pull the bytes speculatively.
        return f"<{mime_type}: not fetched>"

    return response.text[:limit]


def who_else_can_see_it(token: str, file_id: str) -> list[str]:
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


def run(token: str, file_ids: list[str]) -> None:
    for file_id in file_ids:
        try:
            meta = get_metadata(token, file_id)
        except NotOurFile as error:
            print(f"{file_id}: outside the drive.file grant -- {error}")
            continue

        print(f"\n{meta['name']}  [{meta['mimeType']}]  {meta['id']}")
        print(f"  shared with: {', '.join(who_else_can_see_it(token, file_id)) or 'nobody else'}")

        if meta["mimeType"] == FOLDER_MIME:
            children = list_children(token, file_id)
            print(f"  {len(children)} child item(s) reachable through this grant:")
            for child in children:
                print(f"    - {child['name']} [{child['mimeType']}]")
            continue

        text = read_text(token, file_id, meta["mimeType"])
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

    try:
        token, expires_in = refresh_access_token(os.environ["GOOGLE_REFRESH_TOKEN"])
    except GrantRevoked as error:
        sys.exit(f"grant is dead, re-consent required: {error}")

    print(f"access token good for {expires_in}s")
    try:
        run(token, file_ids)
    except GrantRevoked as error:
        # Revocation mid-run is the normal case, not the edge case.
        sys.exit(f"grant revoked mid-run, stopping: {error}")


if __name__ == "__main__":
    main()
