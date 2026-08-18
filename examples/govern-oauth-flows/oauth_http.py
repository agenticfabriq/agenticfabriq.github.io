"""Shared HTTP plumbing for the OAuth examples in this directory.

Standard library only, so the examples run with nothing installed.
Python 3.10+.

Every literal in here comes from a spec, not from a library's defaults:

* form encoding and the JSON error body: RFC 6749 sections 4.1.3 and 5.2
* authorization server metadata discovery: RFC 8414 section 3
* the HTTP Basic form of client authentication: RFC 6749 section 2.3.1
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 30


class DiscoveryError(RuntimeError):
    """Authorization server metadata is missing or unusable."""


class OAuthError(RuntimeError):
    """The authorization server returned an RFC 6749 section 5.2 error body."""

    def __init__(self, error: str, description: str = "", status: int = 0) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status


def basic_auth_header(client_id: str, client_secret: str) -> str:
    """Build the Authorization header for RFC 6749 section 2.3.1 client auth.

    RFC 6749 requires the client id and secret to be form-urlencoded before
    they are joined and base64-encoded. Most ids and secrets survive that
    step unchanged, which is exactly why the bug is hard to find when one
    does not.
    """
    user = urllib.parse.quote(client_id, safe="")
    password = urllib.parse.quote(client_secret, safe="")
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def post_form(
    url: str,
    form: dict[str, str] | list[tuple[str, str]],
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict:
    """POST an application/x-www-form-urlencoded body, return the JSON body.

    `form` accepts a list of pairs because RFC 8693 section 2.1 allows the
    `audience` and `resource` parameters to repeat, and a dict cannot carry a
    repeated key.

    OAuth error responses arrive with a 4xx status and a JSON body whose
    `error` member carries the code, so the body is parsed before raising.
    """
    body = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    if client_id and client_secret:
        request.add_header("Authorization", basic_auth_header(client_id, client_secret))

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload)
        except ValueError:
            raise OAuthError("http_error", payload[:200].decode("utf-8", "replace"), exc.code) from exc
        raise OAuthError(
            parsed.get("error", "http_error"),
            parsed.get("error_description", ""),
            exc.code,
        ) from exc


def metadata_urls(issuer: str) -> list[str]:
    """Both well-known locations for an issuer's metadata document.

    RFC 8414 section 3 inserts `/.well-known/oauth-authorization-server`
    BETWEEN the host and the issuer's path component. OpenID Connect
    Discovery appends `/.well-known/openid-configuration` to the issuer
    instead. For an issuer with no path the two happen to coincide; for
    anything path-scoped -- every Keycloak realm, for instance -- they do
    not, and code that only appends will 404 against an RFC 8414 server.
    """
    parts = urllib.parse.urlsplit(issuer.rstrip("/"))
    path = parts.path
    return [
        urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, f"/.well-known/oauth-authorization-server{path}", "", "")
        ),
        urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, f"{path}/.well-known/openid-configuration", "", "")
        ),
    ]


def discover(issuer: str) -> dict:
    """Fetch authorization server metadata, trying both well-known paths."""
    attempted: list[str] = []
    for url in metadata_urls(issuer):
        attempted.append(url)
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
                metadata = json.load(response)
        except (urllib.error.URLError, ValueError):
            continue
        for required in ("issuer", "authorization_endpoint", "token_endpoint"):
            if required not in metadata:
                raise DiscoveryError(f"{url} returned metadata with no {required}")
        if metadata["issuer"].rstrip("/") != issuer.rstrip("/"):
            # RFC 8414 section 3.3: the issuer in the document MUST match the
            # issuer used to build the request URL. A mismatch is a mix-up
            # attack signal, not a formatting quirk.
            raise DiscoveryError(f"metadata issuer {metadata['issuer']!r} does not match {issuer!r}")
        return metadata
    raise DiscoveryError("no authorization server metadata at " + " or ".join(attempted))
