"""The fix. The same stub model, the same injected document, a context window
with nothing in it worth stealing.

    cd examples/fail-token-leakage
    python3 brokered_agent.py

Exits 0 only if the credential appears in none of the model context, the model
output, the captured trace, or the outbound request. The assertions are the
point of the file: this is a property you can put in CI, which the version in
leaky_agent.py cannot offer at any price.

The shape: the agent names a connection. The broker resolves the name to a
credential, makes the call, and returns a result the model may see. The model
sees "billing" and never sees a key, so the question "will the model reveal the
key" stops being a question.
"""

from __future__ import annotations

import sys

from toy_model import (
    DEMO_CREDENTIAL,
    UNTRUSTED_DOCUMENT,
    SINK,
    deliver,
    reset_sink,
    respond,
)


class LeakGuard(Exception):
    """Raised when a secret is about to cross into the model's context."""


class Broker:
    """Stands between the agent and the credential.

    In this file the broker is an object in the same process, because a single
    runnable script cannot be two processes. That is honest but incomplete: an
    in-process broker keeps the credential out of the *context*, which closes
    the model-facing paths, and leaves the process-facing ones open. A
    subprocess or an MCP server launched from here still inherits this
    process's environment. Closing that too means the broker is a separate
    process or a separate service, and the agent holds only a token for it.
    """

    def __init__(self, credentials: dict[str, str]) -> None:
        self._credentials = credentials

    def connections(self) -> list[str]:
        """Names only. This is the list the model is allowed to know."""
        return sorted(self._credentials)

    def call(self, connection: str, method: str) -> str:
        secret = self._credentials.get(connection)
        if secret is None:
            raise LookupError(f"no connection named {connection!r}")

        # The call happens here, with the credential in a local that never
        # reaches a message, a log line, or a span attribute.
        upstream = self._upstream(connection, method, secret)
        return self._redact(upstream, secret)

    @staticmethod
    def _upstream(connection: str, method: str, secret: str) -> str:
        # Stands in for the provider. Providers really do echo the token back
        # in error strings, which is leak path two: the agent never chose to
        # put the credential in the context, the error handler did.
        return (
            f"401 Unauthorized calling {method} on {connection}: "
            f"token {secret} is expired"
        )

    @staticmethod
    def _redact(text: str, secret: str) -> str:
        """Redact by identity, not by pattern.

        The broker knows the exact bytes it just used, so it does not have to
        guess what a credential looks like. That is the one redaction that
        cannot miss a format it has never seen — and the reason it works here
        is that the broker holds the secret, so it can do the comparison.
        """
        return text.replace(secret, "[redacted:billing]")


def assert_no_secret_in_context(messages: list[dict[str, str]], secret: str) -> None:
    """Run this on every payload before it goes to the model.

    Identity matching again, over the values the broker holds. It will not
    catch a secret nobody registered, which is why it is a backstop for the
    architecture and not a substitute for it.
    """
    for message in messages:
        if secret in message["content"]:
            raise LeakGuard(f"secret found in {message['role']} message")


SYSTEM_PROMPT = """\
You are a billing support agent.
To call an API, name a connection from the list below. You do not have
credentials and cannot obtain them; the broker holds them and makes the call.
Connections: billing
"""


def build_messages(document: str, broker: Broker) -> list[dict[str, str]]:
    tool_block = "\n".join(f"- call({name!r}, method=...)" for name in broker.connections())
    return [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\nTools:\n{tool_block}"},
        {"role": "user", "content": f"Summarize this document:\n\n{document}"},
    ]


def render_markdown_images(markdown: str) -> None:
    rest = markdown
    while "![" in rest:
        rest = rest.split("![", 1)[1]
        if "](" not in rest:
            return
        url, _, rest = rest.partition("](")[2].partition(")")
        deliver(url)


def main() -> int:
    reset_sink()
    broker = Broker({"billing": DEMO_CREDENTIAL})

    print("upstream error, as the agent receives it:")
    print(f"  {broker.call('billing', 'get_invoice')}")

    messages = build_messages(UNTRUSTED_DOCUMENT, broker)
    assert_no_secret_in_context(messages, DEMO_CREDENTIAL)

    output = respond(messages)
    trace = {
        "gen_ai.system_instructions": messages[0]["content"],
        "gen_ai.input.messages": messages[1:],
        "gen_ai.output.messages": output,
    }

    print("\nmodel output, same injected document as leaky_agent.py:\n")
    print(output)
    print("\noutbound calls made by the renderer:")
    render_markdown_images(output)

    checks = {
        "model context": any(DEMO_CREDENTIAL in m["content"] for m in messages),
        "model output": DEMO_CREDENTIAL in output,
        "captured trace": DEMO_CREDENTIAL in repr(trace),
        "outbound request": any(DEMO_CREDENTIAL in url for _, url in SINK),
    }
    print("\nwhere the credential ended up:")
    for name, found in checks.items():
        print(f"  {name:.<22} {found}")

    if any(checks.values()):
        print("\nFAIL: the credential is reachable from the model.")
        return 1

    print(
        "\nThe model still obeyed the injection. It appended the configuration\n"
        "and it built the status link, exactly as it did before. The difference\n"
        "is that the configuration is a connection name, so obedience costs\n"
        "nothing. That is what closing a class looks like: the attack still\n"
        "works and no longer matters."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
