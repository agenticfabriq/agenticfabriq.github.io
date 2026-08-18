#!/usr/bin/env python3
"""Why credential redaction belongs in the formatter, not at the call site.

Companion to the guide "Credential Vaulting and Rotation for Agents".

Three demonstrations, each printing what actually reaches the log stream:

  A. redaction at every call site — leaks through the one call site you do not
     own, which is usually a library's own logger;
  B. a logging.Filter on the root logger — leaks, because a logger's filters see
     only records logged directly on that logger, not records that propagate up
     from its descendants;
  C. a redacting Formatter on the handler — catches the message, the %-style
     arguments, and the traceback, because it runs after all three have been
     rendered into one string;
  E. the same formatter on one of two handlers — leaks, because a record goes
     to every handler of its logger and of every ancestor logger, and each one
     renders it with its own formatter. "A formatter" is not the invariant;
     "every handler that can receive a record" is.

Run it:  python3 redacting_logger.py
"""

from __future__ import annotations

import io
import logging


class SecretRegistry:
    """Every credential value the issuer has minted, newest first by length.

    Redacting by value beats redacting by pattern because the issuer already
    knows the exact strings. Pattern matching is the fallback for credentials
    you did not mint, and it is always incomplete: it catches the shapes you
    thought of.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> str:
        # Refuse loudly rather than silently. A caller that believes it
        # registered a credential and did not is demo D, and demo D leaks.
        if len(value) < 8:
            raise ValueError("refusing to register a value shorter than 8 characters")
        self._values.add(value)
        return value

    def forget(self, value: str) -> None:
        self._values.discard(value)

    def scrub(self, text: str) -> str:
        # Longest first, so a credential that contains another registered value
        # as a substring is masked whole rather than in pieces.
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text


REGISTRY = SecretRegistry()


class RedactingFormatter(logging.Formatter):
    """Scrub the fully rendered record: message, args, traceback, stack info."""

    def __init__(self, registry: SecretRegistry, fmt: str = "%(name)s %(levelname)s %(message)s") -> None:
        super().__init__(fmt)
        self._registry = registry

    def format(self, record: logging.LogRecord) -> str:
        return self._registry.scrub(super().format(record))


class RedactingFilter(logging.Filter):
    """The filter form. Attach it to the handler, never only to a logger.

    Note that it has to rewrite `record.args` as well as `record.msg`: the
    handler renders the line with `record.getMessage()`, which computes
    `msg % args`, so scrubbing only `msg` leaves the credential in `args`.

    Two blind spots survive even when it is written correctly, and demo B'
    shows the second one. It cannot reach exception text, because the formatter
    renders that after every filter has run. And it can only scrub arguments
    that are already strings: `log.warning("failed: %s", exc)` passes an
    exception object, which stringifies inside `getMessage()` long after this
    method returned. Coercing every argument to a string here would change what
    `%d` and `%r` produce, so the fix is not a better filter — it is demo C.
    """

    def __init__(self, registry: SecretRegistry) -> None:
        super().__init__()
        self._registry = registry

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._registry.scrub(record.msg)
        if record.args:
            record.args = tuple(
                self._registry.scrub(arg) if isinstance(arg, str) else arg
                for arg in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


# ---------------------------------------------------------------------------
# A stand-in for the HTTP library whose logger you do not control.
# ---------------------------------------------------------------------------

library_log = logging.getLogger("httplib.transport")


def call_upstream(token: str) -> None:
    library_log.debug("GET /v1/orders authorization=%s", f"Bearer {token}")
    raise RuntimeError(f"upstream rejected credential {token}")


def fresh_stream(*, formatter: logging.Formatter | None = None,
                 handler_filter: logging.Filter | None = None,
                 logger_filter: logging.Filter | None = None) -> io.StringIO:
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(formatter or logging.Formatter("%(name)s %(levelname)s %(message)s"))
    if handler_filter is not None:
        handler.addFilter(handler_filter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for existing in list(root.filters):
        root.removeFilter(existing)
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    if logger_filter is not None:
        root.addFilter(logger_filter)
    return buffer


def demo_two_handlers(token: str) -> None:
    """One formatter is not every formatter."""
    redacted, other = io.StringIO(), io.StringIO()
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for existing in list(root.filters):
        root.removeFilter(existing)
    root.setLevel(logging.DEBUG)

    guarded = logging.StreamHandler(redacted)
    guarded.setFormatter(RedactingFormatter(REGISTRY))
    # The handler a crash reporter, a log-export SDK, or your framework's own
    # file logging attaches beside yours. It renders with its own formatter and
    # never consults yours.
    unguarded = logging.StreamHandler(other)
    unguarded.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root.addHandler(guarded)
    root.addHandler(unguarded)

    logging.getLogger("agent").info("issued credential %s", token)
    leaked = token in other.getvalue()
    print(f"-- E. formatter on one of two handlers: {'LEAKED' if leaked else 'clean'}")
    print(f"     redacted handler: {redacted.getvalue().strip()}")
    print(f"     other handler:    {other.getvalue().strip()}")
    print()


def report(title: str, buffer: io.StringIO, token: str) -> None:
    body = buffer.getvalue()
    verdict = "LEAKED" if token in body else "clean"
    print(f"-- {title}: {verdict}")
    for line in body.strip().splitlines():
        print(f"     {line}")
    print()


def main() -> None:
    token = REGISTRY.register("v1.k1.eyJzdWIiOiJhbWFyYSJ9.G3nR4pQ7wZk1s0Tq")
    app_log = logging.getLogger("agent")

    # A. Redaction at the call site.
    buffer = fresh_stream()
    app_log.info("issued credential %s", "[redacted]")  # remembered
    try:
        call_upstream(token)  # the library's own logger did not
    except RuntimeError:
        app_log.warning("upstream call failed")
    report("A. call-site redaction", buffer, token)

    # B. A filter on the root logger.
    buffer = fresh_stream(logger_filter=RedactingFilter(REGISTRY))
    try:
        call_upstream(token)
    except RuntimeError:
        app_log.warning("upstream call failed")
    report("B. logging.Filter on the root logger", buffer, token)

    # B'. The same filter, on the handler.
    buffer = fresh_stream(handler_filter=RedactingFilter(REGISTRY))
    try:
        call_upstream(token)
    except RuntimeError as exc:
        app_log.warning("upstream call failed: %s", exc)
    report("B'. the same filter on the handler", buffer, token)

    # C. A redacting formatter on the handler, with a traceback.
    buffer = fresh_stream(formatter=RedactingFormatter(REGISTRY))
    try:
        call_upstream(token)
    except RuntimeError:
        app_log.exception("upstream call failed")
    report("C. RedactingFormatter on the handler, logging.exception", buffer, token)

    # E. The same formatter, on one of the two handlers this logger reaches.
    demo_two_handlers(token)

    # The formatter is only as good as the registry. Forget a value and it
    # travels again, which is why issuance and redaction share one registry.
    REGISTRY.forget(token)
    buffer = fresh_stream(formatter=RedactingFormatter(REGISTRY))
    try:
        call_upstream(token)
    except RuntimeError:
        app_log.warning("upstream call failed")
    report("D. the same formatter, value never registered", buffer, token)


if __name__ == "__main__":
    main()
