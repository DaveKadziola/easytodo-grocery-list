"""The files a deployment is made of (Makefile, Dockerfile, entrypoint, guide), which no other
test runs: the Tailwind pins, the start-up warning and the sentences the guide must not lose."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATABASE_URL = "postgresql+asyncpg://easytodo:db-password-0123@localhost:5432/easytodo"
SECRET_KEY = "secret-key-0123456789-abcdefghijklmnop"

# The settings check the entrypoint runs before it migrates and starts the app (a python heredoc).
CHECK = re.search(
    r"<<'PY'\n(.*?)\nPY\n", (REPO / "deploy" / "entrypoint.sh").read_text(), re.DOTALL
)[1]


def test_tailwind_pins_match_in_the_dockerfile_and_the_makefile():
    dockerfile = (REPO / "deploy" / "Dockerfile").read_text()
    makefile = (REPO / "Makefile").read_text()

    def pin(text: str, pattern: str) -> str:
        found = re.search(pattern, text, re.MULTILINE)
        assert found, pattern
        return found[1]

    version = pin(dockerfile, r"^ARG TAILWIND_VERSION=(\S+)$")
    assert pin(makefile, r"^TAILWIND_VERSION := (\S+)$") == f"v{version}"
    for makefile_platform, dockerfile_arch in (("linux-x64", "AMD64"), ("linux-arm64", "ARM64")):
        assert pin(makefile, rf"^TAILWIND_SHA256_{makefile_platform} := (\w+)$") == pin(
            dockerfile, rf"^ARG TAILWIND_SHA256_{dockerfile_arch}=(\w+)$"
        )


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_a_tailwind_version_bump_downloads_a_new_binary():
    """The binary's name has the version in it: a bump is not satisfied by the old file."""
    out = subprocess.run(
        ["make", "-n", "css", "TAILWIND_VERSION=v9.9.9"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "releases/download/v9.9.9/" in out
    assert "./bin/tailwindcss-v9.9.9 -i frontend/styles/input.css" in out


@pytest.mark.parametrize(
    "env,warns",
    [
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "*"}, True),
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "0.0.0.0/0"}, True),
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "10.0.0.2, ::/0"}, True),
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "*", "EASYTODO_BIND": "0.0.0.0"}, True),
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "*", "EASYTODO_BIND": "127.0.0.1"}, False),
        ({}, False),  # the default, 127.0.0.1
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "172.18.0.0/16"}, False),
        # uvicorn trusts everyone for "*" only when it is the whole value: here it is a literal.
        ({"EASYTODO_FORWARDED_ALLOW_IPS": "10.0.0.2, *"}, False),
    ],
)
def test_entrypoint_warns_when_proxy_headers_are_trusted_on_an_open_port(tmp_path, env, warns):
    result = subprocess.run(
        [sys.executable, "-c", CHECK],
        check=False,
        cwd=tmp_path,  # no .env here
        env={
            "PATH": os.environ["PATH"],
            "EASYTODO_DATABASE_URL": DATABASE_URL,
            "EASYTODO_SECRET_KEY": SECRET_KEY,
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr  # a warning, not a refusal to start
    assert ("entrypoint: warning: EASYTODO_FORWARDED_ALLOW_IPS=" in result.stderr) is warns
    if warns:
        value = env["EASYTODO_FORWARDED_ALLOW_IPS"]
        assert f"EASYTODO_FORWARDED_ALLOW_IPS={value} trusts" in result.stderr
    assert "db-password-0123" not in result.stderr and SECRET_KEY not in result.stderr


def test_deployment_guide():
    guide = (REPO / "docs" / "deployment.md").read_text()

    # The tunnel connector is pinned to a release, not to whatever "latest" is on the day.
    assert "cloudflared:latest" not in guide
    assert re.search(r"image: cloudflare/cloudflared:\d{4}\.\d+\.\d+\n", guide)
    # Docker publishes ports past ufw and the INPUT chain: the rule goes in DOCKER-USER.
    assert "block port 8000" not in guide and "DOCKER-USER" in guide
    # The compose project is called easytodo (docker-compose.yml), so the network is too.
    assert "<project>_default" not in guide
    assert "docker network inspect easytodo_default" in guide
    # What open registration costs: invite mails sent through the operator's SMTP account.
    assert "about 200 invite mails an hour" in guide
