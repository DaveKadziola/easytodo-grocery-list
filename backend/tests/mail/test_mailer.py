"""Outgoing mail: the console backend, SMTP (a tiny local SMTP server stands in for a real one)
and the plain-text mail templates."""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from email import message_from_bytes, policy

import aiosmtplib
import pytest
from jinja2 import DictLoader

from easytodo.config import Settings
from easytodo.mail import mailer
from easytodo.mail.mailer import ConsoleMailer, SentMail, SmtpMailer, get_mailer, render_mail


def smtp_settings(settings: Settings, **overrides: object) -> Settings:
    """``settings`` with the SMTP backend and these fields, validated like the real ones."""
    values = settings.model_dump() | {"mail_backend": "smtp"} | overrides
    return Settings(_env_file=None, **values)


async def test_console_mailer_logs_and_keeps_the_mail(caplog):
    console = ConsoleMailer()

    with caplog.at_level(logging.INFO, logger="easytodo.mail"):
        await console.send("friend@example.com", "Hello", "Line one\nLine two\n")

    assert console.outbox == [SentMail("friend@example.com", "Hello", "Line one\nLine two\n")]
    [record] = caplog.records
    for text in ("friend@example.com", "Hello", "Line one\nLine two"):
        assert text in record.getMessage()


def test_get_mailer_picks_the_configured_backend(settings):
    assert isinstance(get_mailer(settings), ConsoleMailer)
    assert isinstance(get_mailer(smtp_settings(settings, smtp_host="mail")), SmtpMailer)


def test_render_mail_takes_the_subject_from_the_first_line():
    subject, body = render_mail(
        "invite",
        inviter="owner@example.com",
        board_name='O\'Reilly <b>"x"</b>',
        role="editor",
        email="friend@example.com",
        link="http://testserver/invite/abc",
        valid_days=7,
    )

    assert subject == "owner@example.com invited you to a board on EasyToDo"
    assert not body.startswith("Subject") and not body.startswith("\n")
    assert 'the board "O\'Reilly <b>"x"</b>"' in body  # plain text: nothing is escaped
    assert "as an editor" in body
    assert "http://testserver/invite/abc\n" in body
    assert "(friend@example.com), and expires in 7 days." in body
    assert body.endswith("\n")


def test_render_mail_refuses_a_template_without_a_subject_line(monkeypatch):
    monkeypatch.setattr(mailer._templates, "loader", DictLoader({"bad.txt": "Hi\nthere\n"}))
    with pytest.raises(ValueError, match="bad.txt must start with a 'Subject: ' line"):
        render_mail("bad")


class FakeSmtpServer:
    """Speaks just enough SMTP for one plain (no TLS, no AUTH) delivery and records it."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.messages: list[bytes] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 fake ESMTP\r\n")
        while line := await reader.readline():
            command = line.decode().strip()
            self.commands.append(command)
            verb = command[:4].upper()
            if verb == "DATA":
                writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                await writer.drain()
                self.messages.append(await reader.readuntil(b"\r\n.\r\n"))
                writer.write(b"250 OK\r\n")
            elif verb == "QUIT":
                writer.write(b"221 Bye\r\n")
                break
            else:  # EHLO, MAIL, RCPT
                writer.write(b"250 OK\r\n")
            await writer.drain()
        await writer.drain()
        writer.close()
        await writer.wait_closed()


@pytest.fixture
async def smtp_server() -> AsyncIterator[tuple[FakeSmtpServer, int]]:
    fake = FakeSmtpServer()
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    async with server:
        yield fake, server.sockets[0].getsockname()[1]


async def test_smtp_mailer_delivers_the_mail(settings, smtp_server):
    fake, port = smtp_server
    smtp = SmtpMailer(
        smtp_settings(
            settings,
            smtp_host="127.0.0.1",
            smtp_port=port,
            smtp_starttls=False,
            smtp_from="EasyToDo <noreply@lists.example.com>",
        )
    )

    await smtp.send("friend@example.com", "You're invited", "Open this link:\nhttp://x/invite/a\n")

    assert "MAIL FROM:<noreply@lists.example.com>" in fake.commands
    assert "RCPT TO:<friend@example.com>" in fake.commands
    [raw] = fake.messages
    message = message_from_bytes(raw.removesuffix(b".\r\n"), policy=policy.default)
    assert message["From"] == "EasyToDo <noreply@lists.example.com>"
    assert message["To"] == "friend@example.com"
    assert message["Subject"] == "You're invited"
    assert message["Date"] and message["Message-ID"].endswith("@lists.example.com>")
    assert message.get_content().splitlines() == ["Open this link:", "http://x/invite/a"]


async def test_smtp_mailer_uses_starttls_credentials_and_a_10s_timeout(settings, monkeypatch):
    calls: list[dict] = []

    async def fake_send(message, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(aiosmtplib, "send", fake_send)
    smtp = SmtpMailer(
        smtp_settings(
            settings,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="lists",
            smtp_password="s3cret",
        )
    )

    await smtp.send("friend@example.com", "Hi", "Body\n")

    assert calls == [
        {
            "hostname": "smtp.example.com",
            "port": 587,
            "username": "lists",
            "password": "s3cret",
            "start_tls": True,
            "timeout": 10,
        }
    ]


async def test_smtp_failure_is_logged_not_raised(settings, caplog):
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    smtp = SmtpMailer(
        smtp_settings(settings, smtp_host="127.0.0.1", smtp_port=port, smtp_starttls=False)
    )

    with caplog.at_level(logging.ERROR, logger="easytodo.mail"):
        await smtp.send("friend@example.com", "Hi", "Body\n")

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert "friend@example.com" in record.getMessage()
    assert record.exc_info is not None
