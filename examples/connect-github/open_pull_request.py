"""Read a file with a GitHub App installation token and open a pull request.

The agent never pushes to the default branch. It creates its own branch, commits
one file, and opens a pull request, so a human review is the gate on every change.

    pip install cryptography requests

Environment:
    GITHUB_APP_CLIENT_ID    the app's client ID
    GITHUB_APP_PRIVATE_KEY  PEM contents of a private key generated for the app
    GITHUB_INSTALLATION_ID  numeric installation id
    GITHUB_REPO             "owner/name"

Reference:
    https://docs.github.com/en/rest/repos/contents
    https://docs.github.com/en/rest/git/refs
    https://docs.github.com/en/rest/pulls/pulls
    https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api
"""

from __future__ import annotations

import base64
import os
import time

import requests

from mint_installation_token import (
    API_ROOT,
    InstallationToken,
    build_app_jwt,
    mint_installation_token,
    revoke,
)

MAX_RATE_LIMIT_RETRIES = 3


class PermissionDenied(RuntimeError):
    """A 403 that no amount of retrying will clear."""


class RateLimited(RuntimeError):
    """A 403 or 429 that a wait would clear, raised once the retries run out."""


def _sleep_for(response: requests.Response) -> float:
    """Return how long GitHub says to wait before retrying this request.

    GitHub documents three signals, in priority order: a `retry-after` header,
    an `x-ratelimit-remaining` of 0 paired with `x-ratelimit-reset`, and
    otherwise a minimum of one minute.
    """
    retry_after = response.headers.get("retry-after")
    if retry_after:
        return float(retry_after)
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = float(response.headers.get("x-ratelimit-reset", "0"))
        return max(reset - time.time(), 0.0)
    return 60.0


def call(
    token: InstallationToken,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> dict:
    """Issue one authenticated request, distinguishing the 403s from each other."""
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        response = requests.request(
            method,
            f"{API_ROOT}{path}",
            headers=token.headers(),
            params=params,
            json=json_body,
            timeout=30,
        )

        if response.status_code in (403, 429):
            # A 403 is either "you lack the permission" or "you are throttled".
            # GitHub only sends X-Accepted-GitHub-Permissions on the former, and
            # only sends rate limit headers on the latter.
            required = response.headers.get("X-Accepted-GitHub-Permissions")
            if required:
                raise PermissionDenied(
                    f"{method} {path} needs {required}; the token was minted with "
                    f"{token.permissions}"
                )
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise RateLimited(f"{method} {path} still throttled after retries")
            time.sleep(_sleep_for(response))
            continue

        if response.status_code == 404:
            raise PermissionDenied(
                f"404 for {method} {path}. GitHub returns 404 rather than 403 when "
                "a token cannot see a private resource, so treat this as either a "
                "missing permission or a repository outside the installation."
            )

        response.raise_for_status()
        return response.json() if response.content else {}

    raise RateLimited(f"{method} {path} exhausted retries")


def read_file(token: InstallationToken, repo: str, path: str, ref: str) -> tuple[str, str]:
    """Return (decoded text, blob sha) for one file. Needs contents: read."""
    body = call(token, "GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})

    encoding = body.get("encoding")
    if encoding != "base64":
        # Files above 1 MB come back with an empty content field. There is no
        # partial result to salvage, so say so instead of decoding an empty
        # string into a silently truncated file.
        raise ValueError(
            f"{path} came back with encoding {encoding!r}; files over 1 MB must be "
            "fetched with the application/vnd.github.raw+json media type instead"
        )
    return base64.b64decode(body["content"]).decode("utf-8"), body["sha"]


def default_branch_head(token: InstallationToken, repo: str) -> tuple[str, str]:
    """Return (default branch name, its head commit sha)."""
    branch = call(token, "GET", f"/repos/{repo}")["default_branch"]
    ref = call(token, "GET", f"/repos/{repo}/git/ref/heads/{branch}")
    return branch, ref["object"]["sha"]


def create_branch(token: InstallationToken, repo: str, name: str, sha: str) -> None:
    """Create refs/heads/<name> at <sha>. Needs contents: write."""
    call(
        token,
        "POST",
        f"/repos/{repo}/git/refs",
        json_body={"ref": f"refs/heads/{name}", "sha": sha},
    )


def commit_file(
    token: InstallationToken,
    repo: str,
    path: str,
    text: str,
    blob_sha: str,
    branch: str,
    message: str,
) -> None:
    """Replace one file on one branch. `sha` is required when updating."""
    call(
        token,
        "PUT",
        f"/repos/{repo}/contents/{path}",
        json_body={
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "sha": blob_sha,
            "branch": branch,
        },
    )


def open_pull_request(
    token: InstallationToken,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str:
    """Open a pull request and return its html_url. Needs pull_requests: write."""
    created = call(
        token,
        "POST",
        f"/repos/{repo}/pulls",
        json_body={
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": True,
            "maintainer_can_modify": True,
        },
    )
    return created["html_url"]


def main() -> None:
    repo = os.environ["GITHUB_REPO"]
    owner, name = repo.split("/", 1)

    app_jwt = build_app_jwt(
        client_id=os.environ["GITHUB_APP_CLIENT_ID"],
        private_key_pem=os.environ["GITHUB_APP_PRIVATE_KEY"],
    )
    token = mint_installation_token(
        app_jwt=app_jwt,
        installation_id=os.environ["GITHUB_INSTALLATION_ID"],
        repositories=[name],
        permissions={"contents": "write", "pull_requests": "write", "metadata": "read"},
    )
    print(f"token expires_at {token.expires_at} for {owner}/{name}")

    try:
        base, head_sha = default_branch_head(token, repo)
        text, blob_sha = read_file(token, repo, "README.md", base)

        branch = f"agent/readme-{int(time.time())}"
        create_branch(token, repo, branch, head_sha)
        commit_file(
            token,
            repo,
            "README.md",
            text + "\n<!-- reviewed by an agent -->\n",
            blob_sha,
            branch,
            "docs: note that an agent read this file",
        )
        url = open_pull_request(
            token,
            repo,
            head=branch,
            base=base,
            title="docs: note that an agent read this file",
            body="Opened by an automation. Nothing merges without a human approval.",
        )
        print(f"pull request {url}")
    finally:
        # The token is good for an hour whether or not this process lives that
        # long. Give it back at the end of the run.
        revoke(token)


if __name__ == "__main__":
    main()
