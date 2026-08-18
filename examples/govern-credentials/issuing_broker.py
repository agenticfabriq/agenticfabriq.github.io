#!/usr/bin/env python3
"""Per-request, per-user, short-lived credential issuance.

Companion to the guide "Credential Vaulting and Rotation for Agents".

The point of this file is the shape, not the crypto. A real deployment replaces
`Broker` with AWS STS, GCP STS, HashiCorp Vault, or a gateway, and replaces
`Resource` with a database or an API. What stays the same:

  * the agent authenticates as *itself* (a workload identity it did not choose
    and cannot exfiltrate usefully), never as the resource;
  * the broker decides, per request, which user the call is for and which
    resource that user is entitled to;
  * the credential it hands back is scoped to that one user, that one resource,
    and a lifetime measured in minutes;
  * the agent uses it and drops it. Nothing is written to disk or to a variable
    that outlives the request.

Run it:  python3 issuing_broker.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# ---------------------------------------------------------------------------
# 0. What the environment-variable version has to grant
# ---------------------------------------------------------------------------

# Two users of the same agent. Their access differs. That is the whole problem.
ENTITLEMENTS: dict[str, set[str]] = {
    "amara@example.com": {"db:orders"},
    "bo@example.com": {"db:support"},
}


def union_of_entitlements(entitlements: dict[str, set[str]]) -> set[str]:
    """The smallest grant a single process-wide credential can carry.

    A credential narrower than this union fails somebody's request, so nobody
    ships one. This function is the environment variable, written out.
    """
    return set().union(*entitlements.values()) if entitlements else set()


# ---------------------------------------------------------------------------
# 1. Workload identity: how the agent proves it is the agent
# ---------------------------------------------------------------------------

# A Kubernetes projected service account token lands here by default. On EC2 the
# equivalent is an IMDSv2 call; in GitHub Actions it is a request to
# ACTIONS_ID_TOKEN_REQUEST_URL. All three share the property that matters: the
# agent did not choose the value, cannot mint one, and the value expires on its
# own.
DEFAULT_IDENTITY_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def read_workload_identity(path: str | None = None) -> str:
    """Read the agent's own identity token, or synthesise one for a local run."""
    candidate = Path(path or os.environ.get("AGENT_IDENTITY_TOKEN_PATH", DEFAULT_IDENTITY_PATH))
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8").strip()
    # Local development only. A real broker rejects an identity it cannot verify
    # against an issuer's public keys; this stand-in exists so the file runs.
    return "local-dev-identity"


# ---------------------------------------------------------------------------
# 2. The signing key ring: one current key, several accepted keys
# ---------------------------------------------------------------------------


@dataclass
class KeyRing:
    """Signing keys held by the broker; verification keys trusted by resources.

    `current_kid` is the key new credentials are signed with. `keys` is every
    key a resource will still verify. Rotation moves `current_kid` forward while
    `keys` briefly holds both — that overlap is what makes rotation survivable.
    """

    keys: dict[str, bytes] = field(default_factory=dict)
    current_kid: str = ""

    def add(self, kid: str, material: bytes) -> None:
        self.keys[kid] = material

    def retire(self, kid: str) -> None:
        if kid == self.current_kid:
            raise ValueError(f"refusing to retire the current signing key {kid!r}")
        self.keys.pop(kid, None)

    def promote(self, kid: str) -> None:
        if kid not in self.keys:
            raise ValueError(f"cannot promote {kid!r}: it is not an accepted key")
        self.current_kid = kid

    def accepted(self) -> Iterable[str]:
        return tuple(self.keys)


def new_key(kid: str) -> tuple[str, bytes]:
    return kid, secrets.token_bytes(32)


# ---------------------------------------------------------------------------
# 3. The credential itself
# ---------------------------------------------------------------------------


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Credential:
    """A bearer string plus the metadata the holder needs to reason about it."""

    token: str
    subject: str
    resource: str
    expires_at: float
    jti: str

    def seconds_left(self, now: float) -> float:
        return self.expires_at - now


class BrokerError(RuntimeError):
    """The broker refused to issue. Never retry this; fix the request."""


@dataclass
class Broker:
    """Issues short-lived credentials to an authenticated workload."""

    keyring: KeyRing
    entitlements: dict[str, set[str]]
    clock: Callable[[], float] = time.time
    trusted_identities: frozenset[str] = frozenset({"local-dev-identity"})
    # Every value this broker mints, so the log redactor can scrub by value
    # rather than by guessing at patterns. See redacting_logger.py.
    minted: set[str] = field(default_factory=set)

    def issue(
        self,
        identity: str,
        subject: str,
        resource: str,
        ttl_seconds: int = 300,
        kid: str | None = None,
    ) -> Credential:
        """Mint one credential. `kid` overrides the current signing key, which
        only the rotation job does, and only to test a pending key."""
        if identity not in self.trusted_identities:
            raise BrokerError("unrecognised workload identity")
        allowed = self.entitlements.get(subject, set())
        if resource not in allowed:
            # The check that an environment variable cannot perform, because an
            # environment variable does not know which user the request is for.
            raise BrokerError(f"{subject} is not entitled to {resource}")
        if not 1 <= ttl_seconds <= 3600:
            raise BrokerError("ttl must be between 1 and 3600 seconds")

        now = self.clock()
        payload = {
            "sub": subject,
            "res": resource,
            "exp": now + ttl_seconds,
            "jti": secrets.token_hex(8),
        }
        body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        kid = kid or self.keyring.current_kid
        if kid not in self.keyring.keys:
            raise BrokerError(f"no signing key {kid!r}")
        signature = hmac.new(self.keyring.keys[kid], body.encode("ascii"), hashlib.sha256).digest()
        token = f"v1.{kid}.{body}.{_b64u(signature)}"
        self.minted.add(token)
        return Credential(token, subject, resource, payload["exp"], payload["jti"])


# ---------------------------------------------------------------------------
# 4. The resource: verifies, and can revoke
# ---------------------------------------------------------------------------


class Denied(Exception):
    """The resource rejected the credential. The reason is the message."""


@dataclass
class Resource:
    """Stands in for the database or API the agent is trying to reach."""

    name: str
    keyring: KeyRing
    clock: Callable[[], float] = time.time
    revoked_jti: set[str] = field(default_factory=set)

    def authorize(self, token: str, subject: str) -> dict:
        try:
            version, kid, body, signature = token.split(".")
        except ValueError as exc:
            raise Denied("malformed credential") from exc
        if version != "v1":
            raise Denied(f"unsupported credential version {version!r}")

        key = self.keyring.keys.get(kid)
        if key is None:
            # Either the key was retired (rotation finished) or it was never
            # ours. Both are the same rejection from here.
            raise Denied(f"signing key {kid!r} is not accepted")

        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64u(signature)):
            raise Denied("signature does not verify")

        payload = json.loads(_unb64u(body))
        if payload["exp"] <= self.clock():
            raise Denied("credential expired")
        if payload["jti"] in self.revoked_jti:
            raise Denied("credential revoked")
        if payload["res"] != self.name:
            raise Denied(f"credential is for {payload['res']!r}, not {self.name!r}")
        if payload["sub"] != subject:
            raise Denied(f"credential is for {payload['sub']!r}, not {subject!r}")
        return payload

    def revoke(self, jti: str) -> None:
        """Kill one credential without touching the key or anyone else's access."""
        self.revoked_jti.add(jti)


# ---------------------------------------------------------------------------
# 5. The agent: holds no credential between requests
# ---------------------------------------------------------------------------


def handle_request(broker: Broker, resource: Resource, subject: str) -> str:
    """One user request, start to finish.

    The credential is a local. It is never returned, never stored on the agent,
    never placed in a module-level cache, and never put anywhere a model can
    read it.
    """
    identity = read_workload_identity()
    credential = broker.issue(identity, subject=subject, resource=resource.name, ttl_seconds=300)
    claims = resource.authorize(credential.token, subject=subject)
    return f"read {resource.name} as {claims['sub']} (jti {claims['jti']})"


def main() -> None:
    print("== what one process-wide credential has to be able to reach ==")
    for user, grants in ENTITLEMENTS.items():
        print(f"  {user:22} needs {sorted(grants)}")
    print(f"  a single DATABASE_URL must therefore carry {sorted(union_of_entitlements(ENTITLEMENTS))}")
    print("  every request runs with that union, including the ones that need half of it\n")

    keyring = KeyRing()
    kid, material = new_key("k1")
    keyring.add(kid, material)
    keyring.promote(kid)

    broker = Broker(keyring=keyring, entitlements=ENTITLEMENTS)
    orders = Resource("db:orders", keyring)
    support = Resource("db:support", keyring)

    print("== per-request issuance ==")
    print(" ", handle_request(broker, orders, "amara@example.com"))
    print(" ", handle_request(broker, support, "bo@example.com"))

    print("\n== the same agent, the wrong user ==")
    try:
        handle_request(broker, support, "amara@example.com")
    except BrokerError as exc:
        print(f"  broker refused to issue: {exc}")

    print("\n== a credential that leaks reaches one user and one resource ==")
    identity = read_workload_identity()
    leaked = broker.issue(identity, "amara@example.com", "db:orders", ttl_seconds=300)
    try:
        support.authorize(leaked.token, subject="amara@example.com")
    except Denied as exc:
        print(f"  db:support rejected it: {exc}")
    try:
        orders.authorize(leaked.token, subject="bo@example.com")
    except Denied as exc:
        print(f"  db:orders rejected it for another subject: {exc}")

    print("\n== revocation is not rotation ==")
    orders.revoke(leaked.jti)
    try:
        orders.authorize(leaked.token, subject="amara@example.com")
    except Denied as exc:
        print(f"  the leaked credential is dead: {exc}")
    still_good = broker.issue(identity, "amara@example.com", "db:orders", ttl_seconds=300)
    orders.authorize(still_good.token, subject="amara@example.com")
    print("  every other credential signed by the same key still works")


if __name__ == "__main__":
    main()
