"""Sending mail. ``EASYTODO_MAIL_BACKEND`` picks the backend: ``console`` (the default) only logs
each mail, ``smtp`` sends it through ``EASYTODO_SMTP_HOST``.

Mails are plain text, rendered from ``templates/<name>.txt``; a template's first line is
``Subject: ...``.
"""

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Protocol

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from easytodo.config import Settings

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 10
# The addresses of the machine the app runs on: the console backend's log is read there.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
CONSOLE_MAIL_WARNING = (
    "No mail is sent (EASYTODO_MAIL_BACKEND=console) although the app runs at %s: "
    "password-reset and invite links are written to this log instead. "
    "Set EASYTODO_MAIL_BACKEND=smtp."
)
OUTBOX_SIZE = 100  # mails the console backend keeps
_SUBJECT_PREFIX = "Subject: "

# Plain text, not HTML, so nothing is escaped.
_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=False,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


class Mailer(Protocol):
    async def send(self, to: str, subject: str, body: str) -> bool:
        """Send the mail; True once a mail server took it, False when it went nowhere (the
        console backend, or a failure, which is logged and never raised)."""
        ...


@dataclass(frozen=True)
class SentMail:
    to: str
    subject: str
    body: str


class ConsoleMailer:
    """Sends nothing: logs each mail and keeps the newest ``OUTBOX_SIZE`` in ``outbox``
    (development and tests). Bounded, as a server left on this default backend runs for long."""

    def __init__(self) -> None:
        self.outbox: list[SentMail] = []

    async def send(self, to: str, subject: str, body: str) -> bool:
        # A warning, so it shows without any logging setup: this mail was not delivered.
        logger.warning(
            "Mail not sent (console backend)\nTo: %s\nSubject: %s\n\n%s", to, subject, body
        )
        self.outbox.append(SentMail(to, subject, body))
        del self.outbox[:-OUTBOX_SIZE]  # drop the oldest beyond the limit
        return False


class SmtpMailer:
    """Sends through the configured SMTP server. A failure is logged, never raised: the
    request that sent the mail goes on."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send(self, to: str, subject: str, body: str) -> bool:
        settings = self.settings
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = formatdate()
        sender_domain = parseaddr(settings.smtp_from)[1].rpartition("@")[2] or "localhost"
        message["Message-ID"] = make_msgid(domain=sender_domain)
        message.set_content(body)
        password = settings.smtp_password.get_secret_value() if settings.smtp_password else None
        try:
            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user,
                password=password,
                start_tls=settings.smtp_starttls,
                timeout=SMTP_TIMEOUT_S,
            )
        except (aiosmtplib.SMTPException, OSError):  # OSError covers refused and timed out
            logger.exception("Could not send mail to %s (subject %r)", to, subject)
            return False
        return True


def get_mailer(settings: Settings) -> Mailer:
    if settings.mail_backend == "smtp":
        return SmtpMailer(settings)
    if settings.public_url.host not in LOCAL_HOSTS:
        # Once per worker, at start: the app is not run on the machine of whoever reads its log.
        logger.warning(CONSOLE_MAIL_WARNING, settings.public_origin)
    return ConsoleMailer()


def render_mail(name: str, **ctx: Any) -> tuple[str, str]:
    """``(subject, body)`` of the mail template ``templates/<name>.txt`` rendered with ``ctx``."""
    first_line, _, body = _templates.get_template(f"{name}.txt").render(**ctx).partition("\n")
    if not first_line.startswith(_SUBJECT_PREFIX):
        raise ValueError(f"{name}.txt must start with a 'Subject: ' line")
    return first_line.removeprefix(_SUBJECT_PREFIX).strip(), body.lstrip("\n")
