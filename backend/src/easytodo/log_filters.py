"""Keeps links that work out of the access log.

An invite or a password-reset link is a secret while it is valid (see "Logs contain live links" in
docs/deployment.md), and uvicorn's access log writes the address of every request, ``?next=``
included: a sign-in that an invite link led to is ``/login?next=%2Finvite%2F<token>``.
"""

import logging
import re

# A "/" as it is written in a path or in a query: plain, or percent-encoded once or more (%2F,
# %252F, ...), in either case.
_SEP = r"(?:/|%(?:25)*2F)"
TOKEN_IN_PATH = re.compile(
    rf"({_SEP}(?:invite|password{_SEP}reset){_SEP})[A-Za-z0-9_-]+", re.IGNORECASE
)
REDACTED = "[redacted]"


class RedactTokens(logging.Filter):
    """Replaces the token of an invite or a reset link in the arguments of a log record.

    uvicorn logs ``(client, method, path, http_version, status)``: the filter changes only the
    strings among them and leaves the five fields, which ``AccessFormatter`` unpacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                TOKEN_IN_PATH.sub(r"\1" + REDACTED, arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


_redact_tokens = RedactTokens()  # one instance, so that adding it again changes nothing


def install_log_filters() -> None:
    """Put the filter on uvicorn's access log. uvicorn sets its logging up before the app is
    created, in every worker; creating an app again (the tests do, many times) adds nothing."""
    logging.getLogger("uvicorn.access").addFilter(_redact_tokens)
