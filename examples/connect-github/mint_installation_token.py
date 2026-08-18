"""Mint a GitHub App installation access token.

Builds an RS256 JSON Web Token by hand so every claim is visible, signs it with
the app's private key, and exchanges it for an installation access token that
expires after one hour.

    pip install cryptography requests

Environment:
    GITHUB_APP_CLIENT_ID    the app's client ID, from the app's settings page
    GITHUB_APP_PRIVATE_KEY  PEM contents of a private key generated for the app
    GITHUB_INSTALLATION_ID  numeric installation id (GET /app/installations)

Reference:
    https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
    https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

# GitHub rejects a JWT whose exp is more than 10 minutes in the future, and
# recommends backdating iat by 60 seconds to absorb clock drift. 540 seconds
# leaves 60 seconds of headroom under the 600-second ceiling even after the
# backdated iat is taken into account.
IAT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540


class GitHubAuthError(RuntimeError):
    """Raised when GitHub rejects the app's own credentials."""


@dataclass(frozen=True)
class InstallationToken:
    """A `ghs_`-prefixed token plus the moment GitHub says it stops working."""

    token: str
    expires_at: str
    permissions: dict[str, str]
    repository_selection: str

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
        }


def _b64url(raw: bytes) -> str:
    """base64url without padding, which is what JWS compact serialization wants."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_private_key(pem: str) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubAuthError(
            "GITHUB_APP_PRIVATE_KEY is not an RSA private key; GitHub issues "
            "PKCS#1 RSAPrivateKey PEM files and RS256 requires RSA"
        )
    return key


def build_app_jwt(client_id: str, private_key_pem: str, now: int | None = None) -> str:
    """Return a signed RS256 JWT that authenticates as the app itself.

    The token carries exactly three claims. `iat` is backdated so a slow clock
    on this machine does not produce a token GitHub considers future-dated.
    `exp` stays inside GitHub's 10-minute ceiling. `iss` is the app's own
    identifier -- GitHub accepts the app ID and recommends the client ID.
    """
    issued_at = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": issued_at - IAT_BACKDATE_SECONDS,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
        "iss": client_id,
    }

    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")

    signature = _load_private_key(private_key_pem).sign(
        signing_input,
        padding.PKCS1v15(),  # RS256 is RSASSA-PKCS1-v1_5 with SHA-256
        hashes.SHA256(),
    )
    segments.append(_b64url(signature))
    return ".".join(segments)


def mint_installation_token(
    app_jwt: str,
    installation_id: str,
    repositories: list[str] | None = None,
    permissions: dict[str, str] | None = None,
) -> InstallationToken:
    """Exchange an app JWT for an installation access token.

    `repositories` and `permissions` narrow the token below what the
    installation itself was granted. Neither can widen it: GitHub rejects a
    permission the app was never granted rather than silently downgrading it.
    """
    body: dict[str, object] = {}
    if repositories is not None:
        body["repositories"] = repositories
    if permissions is not None:
        body["permissions"] = permissions

    response = requests.post(
        f"{API_ROOT}/app/installations/{installation_id}/access_tokens",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "X-GitHub-Api-Version": API_VERSION,
        },
        json=body,
        timeout=30,
    )

    if response.status_code == 401:
        raise GitHubAuthError(
            "401 from the token exchange. The JWT was rejected: check that the "
            "private key belongs to this app, that iss is the app's client ID "
            "or app ID, and that this machine's clock is correct."
        )
    if response.status_code == 404:
        raise GitHubAuthError(
            f"404 for installation {installation_id}. Either the id is wrong or "
            "the app was uninstalled from that account."
        )
    if response.status_code == 422:
        raise GitHubAuthError(f"422 from the token exchange: {response.text}")
    response.raise_for_status()

    payload = response.json()
    return InstallationToken(
        token=payload["token"],
        expires_at=payload["expires_at"],
        permissions=payload.get("permissions", {}),
        repository_selection=payload.get("repository_selection", "unknown"),
    )


def revoke(token: InstallationToken) -> None:
    """Hand the token back early instead of leaving it valid for the full hour."""
    response = requests.delete(
        f"{API_ROOT}/installation/token",
        headers=token.headers(),
        timeout=30,
    )
    # 401 means the token already expired or was already revoked; either way the
    # credential is dead, which is the outcome this function exists to produce.
    if response.status_code not in (204, 401):
        response.raise_for_status()


def token_for_repository(repository: str) -> InstallationToken:
    """Mint a token narrowed to one repository with read-only contents access."""
    app_jwt = build_app_jwt(
        client_id=os.environ["GITHUB_APP_CLIENT_ID"],
        private_key_pem=os.environ["GITHUB_APP_PRIVATE_KEY"],
    )
    return mint_installation_token(
        app_jwt=app_jwt,
        installation_id=os.environ["GITHUB_INSTALLATION_ID"],
        repositories=[repository],
        permissions={"contents": "read", "metadata": "read"},
    )


if __name__ == "__main__":
    minted = token_for_repository(os.environ.get("GITHUB_REPO_NAME", "example-repo"))
    print(f"prefix          {minted.token[:4]}")
    print(f"expires_at      {minted.expires_at}")
    print(f"repo selection  {minted.repository_selection}")
    print(f"permissions     {minted.permissions}")
    revoke(minted)
