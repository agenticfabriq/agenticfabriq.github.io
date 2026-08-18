"""Read a file with a GitHub App installation token and open a pull request.

The agent never pushes to the default branch. It creates its own branch, commits
one file, and opens a pull request, so a human review is the gate on every change.

Run this from inside `examples/connect-github/`: it imports its sibling
`mint_installation_token` as a top-level module, so that directory has to be on
`sys.path`.

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
    https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
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


def _throttle_wait(response: requests.Response) -> float | None:
    """Return the wait GitHub's documented signals ask for, or None if neither fired.

    GitHub sends `x-ratelimit-*` headers with every response, so the presence of
    a rate limit header proves nothing. Their values do. GitHub documents two
    signals, in this order: a `retry-after` header, which is only present when
    GitHub wants you to wait, and an `x-ratelimit-remaining` of 0 paired with
    `x-ratelimit-reset`, which means the window is spent.
    """
    retry_after = response.headers.get("retry-after")
    if retry_after:
        return float(retry_after)
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = float(response.headers.get("x-ratelimit-reset", "0"))
        return max(reset - time.time(), 0.0)
    return None


def _error_message(response: requests.Response) -> str:
    """Return GitHub's `message` field, or "" for a body that is not JSON."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return body.get("message", "") if isinstance(body, dict) else ""


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
            # A 403 is either "you lack the permission" or "you are throttled",
            # and only documented signals can tell them apart.
            #
            # 1. retry-after, or x-ratelimit-remaining of 0: GitHub is asking for
            #    a wait. This is checked first on purpose. A permission failure
            #    that happens to land on a spent window costs one wasted wait and
            #    then classifies correctly on the retry, whereas the reverse
            #    ordering hard-fails a request a wait would have cleared.
            # 2. The documented error message. GitHub tells you to read
            #    "Resource not accessible by integration" (or "...by personal
            #    access token") as insufficient permissions.
            # 3. Anything else: GitHub's guidance for a rate limit with no
            #    explicit signal is to wait at least a minute, which covers the
            #    secondary limits.
            #
            # X-Accepted-GitHub-Permissions is deliberately NOT the classifier.
            # GitHub introduced it as the fine-grained counterpart to
            # x-accepted-oauth-scopes so callers can discover what a route needs,
            # and never documents it as exclusive to failures. It is used below
            # only to say WHICH permission was missing.
            wait = _throttle_wait(response)
            if wait is None:
                message = _error_message(response)
                if "Resource not accessible by" in message:
                    required = response.headers.get("X-Accepted-GitHub-Permissions")
                    detail = f" needs {required};" if required else ";"
                    raise PermissionDenied(
                        f"{method} {path}{detail} the token was minted with "
                        f"{token.permissions}"
                    )
                wait = 60.0
            if attempt == MAX_RATE_LIMIT_RETRIES:
                raise RateLimited(f"{method} {path} still throttled after retries")
            time.sleep(wait)
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
    """Open a pull request and return its html_url. Needs pull_requests: write.

    `draft` is left at GitHub's default rather than set here. Draft pull requests
    on private repositories have historically required a paid plan, and a 422 for
    that reason is indistinguishable from the other 422s this endpoint returns.
    """
    created = call(
        token,
        "POST",
        f"/repos/{repo}/pulls",
        json_body={
            "title": title,
            "head": head,
            "base": base,
            "body": body,
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
