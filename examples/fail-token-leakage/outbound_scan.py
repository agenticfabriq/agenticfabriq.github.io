"""Detection, and the exact shape of what detection misses.

    cd examples/fail-token-leakage
    python3 outbound_scan.py

Three controls, in the order they fail. Pattern scanning catches the formats it
was taught. Identity matching catches the values you registered. Canaries catch
nothing until someone uses the credential. Run all three; none of them is a
substitute for keeping the secret out of the context in the first place.
"""

from __future__ import annotations

import re

from toy_model import DEMO_CREDENTIAL

# A handful of patterns, of the kind a hosted scanner ships hundreds of.
#
# AKIA[0-9A-Z]{16} is the AWS access key id shape; AKIAIOSFODNN7EXAMPLE below
# is AWS's own documented example value, so it authenticates against nothing.
# X-Amz-Signature and X-Goog-Signature are the real query parameters that make
# a URL a bearer credential rather than a link.
#
# Do not treat this list as authoritative for anything. Vendor prefixes and
# lengths change, and the maintained list is the scanner vendor's, not yours.
# GitHub publishes theirs under "supported secret scanning patterns".
PATTERNS: dict[str, re.Pattern[str]] = {
    "aws-access-key-id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token-prefix": re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "signed-url": re.compile(r"[?&](X-Amz-Signature|X-Goog-Signature)="),
}

# A payload on its way to a model provider, a trace backend, or a log sink.
OUTBOUND_PAYLOAD = f"""\
role=system content=You are a billing agent.
  aws_key=AKIAIOSFODNN7EXAMPLE
  archive=https://archive.example.invalid/inv.zip?X-Amz-Signature=EXAMPLE0000
  billing_token={DEMO_CREDENTIAL}
"""

# Values your broker or vault actually issued. Identity matching over this set
# does not need to guess a format, because you know the exact bytes.
REGISTERED_SECRETS = {DEMO_CREDENTIAL}

# A credential that is valid, unused, and planted where a leak would find it.
# Its only job is to be stolen. Canarytokens ships an AWS API keys token that
# does this, and documents alert delays of 2-30 minutes because the signal
# comes from Amazon's own logging.
CANARY = "demo_live_CANARY_NOT_A_REAL_CREDENTIAL_0000"


def scan_by_pattern(payload: str) -> list[str]:
    return sorted(name for name, pattern in PATTERNS.items() if pattern.search(payload))


def scan_by_identity(payload: str, registered: set[str]) -> list[str]:
    return sorted(secret[:14] + "..." for secret in registered if secret in payload)


def canary_alert(used_credential: str) -> str | None:
    """Fires on use. Not on placement, not on leak, not on exfiltration.

    Between the leak and this alert, the credential was valid the whole time.
    A canary tells you a leak happened; it does not prevent one, and it stays
    silent forever if the thief never spends what they took.
    """
    if used_credential == CANARY:
        return "ALERT: canary credential used. Something read a place it should not have."
    return None


def main() -> None:
    print("payload on the wire:\n")
    print(OUTBOUND_PAYLOAD)

    by_pattern = scan_by_pattern(OUTBOUND_PAYLOAD)
    print(f"pattern scan hits ...... {by_pattern}")
    print(
        "  Two of the three credentials in that payload. The billing token has\n"
        "  an in-house prefix, so no shipped pattern matches it, and no amount\n"
        "  of tuning fixes that: a scanner cannot match a format nobody told it\n"
        "  about. Every internal token, every partner key with a bespoke shape,\n"
        "  and every credential a vendor changed the prefix of last quarter is\n"
        "  in the same blind spot."
    )

    by_identity = scan_by_identity(OUTBOUND_PAYLOAD, REGISTERED_SECRETS)
    print(f"\nidentity scan hits ..... {by_identity}")
    print(
        "  Caught, because the value was registered. This control inverts the\n"
        "  failure: it never misses a format, and it misses every secret that\n"
        "  was never registered — a key someone pasted into a config file, or\n"
        "  one a vendor rotated without telling you."
    )

    print("\ncanary, before anyone touches it:")
    print(f"  {canary_alert('some-other-token') or 'silence'}")
    print("canary, after the thief spends it:")
    print(f"  {canary_alert(CANARY)}")

    print(
        "\nRun all three and you still have gaps, in three different places.\n"
        "That is the argument for the broker in brokered_agent.py: it does not\n"
        "improve the odds of catching a leak, it removes the thing that leaks."
    )


if __name__ == "__main__":
    main()
