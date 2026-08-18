#!/usr/bin/env python3
"""Zero-downtime rotation, and the reordering that turns it into an outage.

Companion to the guide "Credential Vaulting and Rotation for Agents".

The sequence below is the one AWS Secrets Manager's rotation Lambda performs,
with its four steps named the same way: create, set, test, finish. The two steps
Secrets Manager does not do for you — draining the overlap window and retiring
the old credential — are steps 5 and 6 here, and skipping either of them is the
difference between rotating and pretending to.

Run it:  python3 rotate_with_overlap.py
"""

from __future__ import annotations

from dataclasses import dataclass

from issuing_broker import (
    ENTITLEMENTS,
    Broker,
    Credential,
    Denied,
    KeyRing,
    Resource,
    new_key,
)

CREDENTIAL_TTL = 300  # seconds; the longest any issued credential stays valid


@dataclass
class FakeClock:
    """A clock you can move, so the timeline is exact instead of approximate."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(clock: FakeClock) -> tuple[Broker, Resource, KeyRing, KeyRing]:
    """A broker that signs and a resource that verifies, with separate key sets.

    Keeping the two key sets separate is the whole reason this works. The broker
    decides what to sign with; the resource decides what to accept. Rotation is
    the art of never letting the first set get ahead of the second.
    """
    broker_ring = KeyRing()
    resource_ring = KeyRing()
    kid, material = new_key("k1")
    broker_ring.add(kid, material)
    broker_ring.promote(kid)
    resource_ring.add(kid, material)

    broker = Broker(keyring=broker_ring, entitlements=ENTITLEMENTS, clock=clock)
    resource = Resource("db:orders", resource_ring, clock=clock)
    return broker, resource, broker_ring, resource_ring


def check(resource: Resource, credential: Credential, label: str, clock: FakeClock) -> bool:
    try:
        resource.authorize(credential.token, subject=credential.subject)
    except Denied as exc:
        print(f"    t={clock.now:>5.0f}s  {label}: DENIED — {exc}")
        return False
    print(f"    t={clock.now:>5.0f}s  {label}: accepted ({credential.seconds_left(clock.now):.0f}s left)")
    return True


def correct_sequence() -> None:
    print("== correct ordering ==")
    clock = FakeClock()
    broker, resource, broker_ring, resource_ring = build(clock)
    identity = "local-dev-identity"

    in_flight = broker.issue(identity, "amara@example.com", "db:orders", CREDENTIAL_TTL)
    print(f"    t={clock.now:>5.0f}s  a worker takes a credential signed by k1, valid {CREDENTIAL_TTL}s")

    clock.advance(10)
    kid2, material2 = new_key("k2")
    broker_ring.add(kid2, material2)
    print(f"    t={clock.now:>5.0f}s  1. create: k2 generated and held pending. Nothing signs with it.")

    clock.advance(10)
    resource_ring.add(kid2, material2)
    print(f"    t={clock.now:>5.0f}s  2. set: resource now accepts {sorted(resource_ring.accepted())}. "
          "OVERLAP WINDOW OPENS.")
    check(resource, in_flight, "in-flight k1 credential", clock)

    clock.advance(10)
    probe = broker.issue(identity, "amara@example.com", "db:orders", 60, kid="k2")
    ok = check(resource, probe, "3. test: a k2 credential", clock)
    if not ok:
        print("    aborting rotation. Nothing has moved; every caller is still on k1.")
        return

    clock.advance(10)
    broker_ring.promote("k2")
    promoted_at = clock.now
    print(f"    t={clock.now:>5.0f}s  4. finish: k2 is now the signing key. New credentials are k2-signed.")
    check(resource, in_flight, "in-flight k1 credential", clock)

    drain_until = promoted_at + CREDENTIAL_TTL
    clock.advance(drain_until - clock.now)
    print(f"    t={clock.now:>5.0f}s  5. drain: waited {CREDENTIAL_TTL}s, the longest life a k1 "
          "credential could have had.")
    check(resource, in_flight, "in-flight k1 credential", clock)
    print("    it is denied because it ran out its own clock, not because rotation broke it. "
          "That is the precondition step 6 needs.")

    resource_ring.retire("k1")
    broker_ring.retire("k1")
    print(f"    t={clock.now:>5.0f}s  6. retire: resource accepts {sorted(resource_ring.accepted())}. "
          "OVERLAP WINDOW CLOSES.")
    print("    every check while the window was open passed, and no k1 credential can work again.\n")


def wrong_sequence() -> None:
    print("== the reordering that causes the outage ==")
    clock = FakeClock()
    broker, resource, broker_ring, resource_ring = build(clock)
    identity = "local-dev-identity"

    in_flight = broker.issue(identity, "amara@example.com", "db:orders", CREDENTIAL_TTL)
    print(f"    t={clock.now:>5.0f}s  same worker, same k1 credential, valid {CREDENTIAL_TTL}s")

    clock.advance(10)
    kid2, material2 = new_key("k2")
    broker_ring.add(kid2, material2)
    resource_ring.add(kid2, material2)
    broker_ring.promote(kid2)
    print(f"    t={clock.now:>5.0f}s  create, set and finish, all at once")

    clock.advance(10)
    resource_ring.retire("k1")
    broker_ring.retire("k1")
    print(f"    t={clock.now:>5.0f}s  retire k1 immediately, because the new key 'works'")
    check(resource, in_flight, "in-flight k1 credential", clock)
    print(f"    that credential had {in_flight.seconds_left(clock.now):.0f}s of validity left. "
          "Every holder of one fails at once.\n")


def rotation_without_revocation() -> None:
    print("== rotation is not revocation ==")
    clock = FakeClock()
    broker, resource, broker_ring, resource_ring = build(clock)
    identity = "local-dev-identity"

    stolen = broker.issue(identity, "amara@example.com", "db:orders", CREDENTIAL_TTL)
    print(f"    t={clock.now:>5.0f}s  a credential leaks into a prompt log")

    clock.advance(60)
    kid2, material2 = new_key("k2")
    broker_ring.add(kid2, material2)
    resource_ring.add(kid2, material2)
    broker_ring.promote(kid2)
    print(f"    t={clock.now:>5.0f}s  you rotate: steps 1 through 4 complete")
    check(resource, stolen, "the leaked credential", clock)
    print("    rotation alone bought nothing. The old key is still accepted, by design.")

    resource.revoke(stolen.jti)
    check(resource, stolen, "the leaked credential, after revoke(jti)", clock)
    survivor = broker.issue(identity, "amara@example.com", "db:orders", CREDENTIAL_TTL)
    check(resource, survivor, "an unrelated credential", clock)
    print("    revocation killed one credential in one call and nothing else.\n")


if __name__ == "__main__":
    correct_sequence()
    wrong_sequence()
    rotation_without_revocation()
