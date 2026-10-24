"""Sequential latency benchmark for EasyToDo v1 (legacy) and v2.

Seeds a board (categories x items), then times three scenarios one request at a time and prints
a Markdown table of p50/p95 latencies in milliseconds:

    view_board    legacy: GET /                       v2: GET /boards/{id}
    add_item      legacy: POST /v1/add_task/          v2: POST /categories/{id}/items
    toggle_item   legacy: PUT /v1/toggle_task/{id}    v2: POST /items/{id}/toggle

v2 gets two more rows. view_board_content is GET /boards/{id}/content, the board fragment the
page fetches again once its WebSocket is open (so a board view in the browser makes both
requests); it runs right after view_board, on the same board. ws_event is the time from sending a
toggle until a second client (another login, with the board's WebSocket open) receives the event,
over 100 changes.

Every scenario discards 10 warm-up requests before the measured ones. The scenarios run in the
order above on the same seed, so add_item grows the board before toggle_item runs.

    uv run python ../scripts/bench.py --target legacy --base-url http://localhost:8100
    uv run python ../scripts/bench.py --target v2 --base-url http://localhost:8000 \
        --email bench@example.com --password bench-password-123

For --target v2, --base-url must be the app's EASYTODO_PUBLIC_URL or one of its
EASYTODO_EXTRA_ORIGINS: the CSRF check compares the Origin header, and a request from any other
origin gets a 403 (the board's WebSocket is closed with code 4400).

--realistic (v2 only) seeds the board the way a household uses it, so that the v2.1 features are
on the page: a second account (bench-member-<run>@example.com, with the --password) joins the
board as an editor through an invite link and adds every other task, which makes the board
shared (each task shows its author's initials), and every REALISTIC_EVERY-th task gets
CHECKLIST_ENTRIES checklist entries and a small text file. The registration is rate limited per
IP (10 an hour): start each realistic run on a fresh app.
"""

import argparse
import asyncio
import itertools
import json
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect

WARMUP_REQUESTS = 10
EVENT_TRIALS = 100
EVENT_TIMEOUT_S = 5
LEGACY_HEADERS = {"Accept": "application/vnd.myapi.v1+json", "Content-Type": "application/json"}
CSRF_COOKIE = "easytodo_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SESSION_COOKIE = "easytodo_session"
CLIENT_ID_HEADER = "X-Client-Id"
CLIENT_ID = "bench"  # the browser tab making the changes
SECOND_CLIENT_ID = "bench-second-client"  # the tab whose socket waits for them
REALISTIC_EVERY = 10  # --realistic: every 10th task gets a checklist and a file
CHECKLIST_ENTRIES = 5  # checklist entries of such a task
BENCH_FILE = ("note.txt", b"EasyToDo bench\n", "text/plain")  # the file of such a task


def percentile(values: list[float], p: float) -> float:
    """p-th percentile (0-100), linear interpolation like quantiles(method="inclusive")."""
    if not values:
        raise ValueError("percentile of empty data")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low = math.floor(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


async def measure(action: Callable[[], Awaitable[None]], requests: int) -> list[float]:
    """Latencies in ms of ``requests`` sequential calls, after discarding the warm-up calls."""
    for _ in range(WARMUP_REQUESTS):
        await action()
    samples = []
    for _ in range(requests):
        started = time.perf_counter()
        await action()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def format_table(results: dict[str, list[float]]) -> str:
    lines = ["| scenario | p50 ms | p95 ms |", "| --- | ---: | ---: |"]
    for scenario, samples in results.items():
        lines.append(
            f"| {scenario} | {percentile(samples, 50):.2f} | {percentile(samples, 95):.2f} |"
        )
    return "\n".join(lines)


@dataclass
class Seeded:
    category_ids: list[int]
    item_ids: list[int]


class Target(Protocol):
    async def seed(self, categories: int, items: int) -> Seeded: ...
    async def view_board(self) -> None: ...
    async def add_item(self, category_id: int, name: str) -> None: ...
    async def toggle_item(self, item_id: int) -> None: ...


class LegacyTarget:
    """v1: Flask app with a JSON API (needs the vendor Accept header) and a server-rendered `/`."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._done: dict[int, bool] = {}

    async def _api(self, method: str, path: str, payload: dict) -> httpx.Response:
        response = await self.client.request(method, path, json=payload, headers=LEGACY_HEADERS)
        response.raise_for_status()
        return response

    async def seed(self, categories: int, items: int) -> Seeded:
        run = uuid.uuid4().hex[:8]
        seeded = Seeded([], [])
        for c in range(categories):
            response = await self._api(
                "POST", "/v1/add_category", {"category_name": f"bench {run} {c}"}
            )
            category_id = response.json()["category_id"]
            seeded.category_ids.append(category_id)
            for i in range(items):
                payload = {"category_id": category_id, "task_name": f"item {c}-{i}"}
                response = await self._api("POST", "/v1/add_task/", payload)
                seeded.item_ids.append(response.json()["task_id"])
        return seeded

    async def view_board(self) -> None:
        (await self.client.get("/")).raise_for_status()

    async def add_item(self, category_id: int, name: str) -> None:
        await self._api("POST", "/v1/add_task/", {"category_id": category_id, "task_name": name})

    async def toggle_item(self, item_id: int) -> None:
        # The v1 API sets the state instead of flipping it, so track it like the checkbox does.
        done = self._done[item_id] = not self._done.get(item_id, False)
        await self._api("PUT", f"/v1/toggle_task/{item_id}", {"is_done": done})


def origin_of(client: httpx.AsyncClient) -> str:
    base = client.base_url
    return f"{base.scheme}://{base.netloc.decode()}"


async def log_in(client: httpx.AsyncClient, email: str, password: str) -> None:
    """Sign ``client`` in through the v2 login form; it then sends what the browser sends on
    every change (the CSRF header and the Origin)."""
    client.headers["Origin"] = origin_of(client)
    (await client.get("/login")).raise_for_status()
    token = client.cookies.get(CSRF_COOKIE)
    if token is None:
        raise RuntimeError(f"GET /login did not set the {CSRF_COOKIE} cookie")
    form = {"email": email, "password": password, CSRF_FIELD: token}
    response = await client.post("/login", data=form)
    if response.status_code != 303:
        raise RuntimeError(f"Login failed: HTTP {response.status_code}")
    client.headers[CSRF_HEADER] = token


async def join_by_invite(
    client: httpx.AsyncClient, invite_link: str, email: str, password: str
) -> None:
    """Sign ``client`` up as ``email`` through the invite's page and accept the invite, as a
    person who follows the link does; it then sends the CSRF header and the Origin on every change.
    Only the path of ``invite_link`` is used: its host is EASYTODO_PUBLIC_URL, which is also the
    address ``client`` talks to. Any answer but 303 is a ``RuntimeError`` that names the step."""
    client.headers["Origin"] = origin_of(client)
    page = urlsplit(invite_link).path  # /invite/{token}
    after = {"next": page}  # sign-up works from an invite page even with registration closed
    response = await client.get("/register", params=after)
    token = client.cookies.get(CSRF_COOKIE)
    if response.status_code != 200 or token is None:
        raise RuntimeError(
            f"Joining by invite: opening the sign-up page failed: HTTP {response.status_code}"
        )
    form = {"email": email, "password": password, CSRF_FIELD: token}
    response = await client.post("/register", params=after, data=form)
    if response.status_code != 303:
        raise RuntimeError(f"Joining by invite: signing up failed: HTTP {response.status_code}")
    response = await client.post(f"{page}/accept", headers={CSRF_HEADER: token})
    if response.status_code != 303:
        raise RuntimeError(
            f"Joining by invite: accepting the invite failed: HTTP {response.status_code}"
        )
    client.headers[CSRF_HEADER] = token


async def _received(socket: ClientConnection) -> tuple[float, str]:
    """The next message on ``socket``, with the ``perf_counter`` time it arrived at."""
    message = await socket.recv()
    return time.perf_counter(), message


class V2Target:
    """v2: FastAPI + HTMX app; logs in through the form and uses the fragment routes.
    ``second_client`` is only used to sign in a second time, for the board socket of ws_event.
    With a ``member_client`` the seed is the realistic one (see the module docstring): a second
    account joins the board and the board gets checklists and files."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        second_client: httpx.AsyncClient,
        *,
        email: str,
        password: str,
        member_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client = client
        self.second_client = second_client
        self.member_client = member_client
        self.email = email
        self.password = password
        self.board_id: int | None = None

    async def _login(self) -> None:
        await log_in(self.client, self.email, self.password)
        self.client.headers[CLIENT_ID_HEADER] = CLIENT_ID  # as app.js sends it from each tab

    async def _post(self, path: str, **form: str) -> httpx.Response:
        return await self._post_as(self.client, path, **form)

    @staticmethod
    async def _post_as(client: httpx.AsyncClient, path: str, **form: str) -> httpx.Response:
        response = await client.post(path, data=form)
        response.raise_for_status()
        return response

    async def _share_board(self, member_client: httpx.AsyncClient, run: str) -> None:
        """A second account joins the board as an editor, through an invite link."""
        response = await self._post(f"/boards/{self.board_id}/invites", role="editor")
        match = re.search(r'<input id="invite-link" value="([^"]+)"', response.text)
        if match is None:
            raise RuntimeError("The invite page shows no invite link")
        email = f"bench-member-{run}@example.com"
        await join_by_invite(member_client, match[1], email, self.password)

    async def _fill_details(self, item_ids: list[int]) -> None:
        """A checklist and a file for every REALISTIC_EVERY-th task."""
        for item_id in item_ids[::REALISTIC_EVERY]:
            for n in range(CHECKLIST_ENTRIES):
                await self._post(f"/items/{item_id}/checklist", text=f"step {n}")
            response = await self.client.post(
                f"/items/{item_id}/attachments", files={"file": BENCH_FILE}
            )
            response.raise_for_status()

    async def seed(self, categories: int, items: int) -> Seeded:
        await self._login()
        run = uuid.uuid4().hex[:8]
        response = await self.client.post("/boards", data={"name": f"bench {run}"})
        match = re.fullmatch(r"/boards/(\d+)", response.headers.get("location", ""))
        if response.status_code != 303 or match is None:
            raise RuntimeError(f"Creating the board failed: HTTP {response.status_code}")
        self.board_id = int(match[1])
        if self.member_client is not None:
            await self._share_board(self.member_client, run)

        category_ids = []
        for c in range(categories):
            path = f"/boards/{self.board_id}/categories"
            response = await self._post(path, name=f"cat {c}")
            match = re.search(r'id="category-(\d+)"', response.text)
            if match is None:
                raise RuntimeError(f'POST {path} returned no id="category-N" fragment')
            category_id = int(match[1])
            category_ids.append(category_id)
            for i in range(items):
                # With a second account, it adds every other task: the authors differ.
                adder = self.member_client if self.member_client and i % 2 else self.client
                await self._post_as(adder, f"/categories/{category_id}/items", name=f"item {c}-{i}")

        page = await self.client.get(f"/boards/{self.board_id}")
        page.raise_for_status()
        item_ids = [int(i) for i in dict.fromkeys(re.findall(r'id="item-(\d+)"', page.text))]
        if len(item_ids) != categories * items:
            raise RuntimeError(
                f"Seeded {categories * items} items, the board lists {len(item_ids)}"
            )
        if self.member_client is not None:
            await self._fill_details(item_ids)
        return Seeded(category_ids, item_ids)

    async def view_board(self) -> None:
        (await self.client.get(f"/boards/{self.board_id}")).raise_for_status()

    async def view_board_content(self) -> None:
        (await self.client.get(f"/boards/{self.board_id}/content")).raise_for_status()

    async def add_item(self, category_id: int, name: str) -> None:
        await self._post(f"/categories/{category_id}/items", name=name)

    async def toggle_item(self, item_id: int) -> None:
        await self._post(f"/items/{item_id}/toggle")

    async def event_latencies(self, item_ids: Iterator[int], trials: int) -> list[float]:
        """ms from sending a toggle until the board socket of a second client (another login
        of the same account, as on a second phone) has its event; after 10 warm-up changes."""
        await log_in(self.second_client, self.email, self.password)
        origin = origin_of(self.second_client)
        url = f"ws{origin.removeprefix('http')}/ws/boards/{self.board_id}"
        cookie = {"Cookie": f"{SESSION_COOKIE}={self.second_client.cookies[SESSION_COOKIE]}"}
        samples = []
        async with connect(
            f"{url}?client_id={SECOND_CLIENT_ID}", origin=origin, additional_headers=cookie
        ) as socket:
            for trial in range(WARMUP_REQUESTS + trials):
                receiving = asyncio.create_task(_received(socket))
                item_id = next(item_ids)
                try:
                    started = time.perf_counter()
                    await self.toggle_item(item_id)
                    received_at, message = await asyncio.wait_for(receiving, EVENT_TIMEOUT_S)
                except TimeoutError:
                    raise RuntimeError(
                        f"No event on board {self.board_id} for item {item_id}"
                        f" within {EVENT_TIMEOUT_S} s"
                    ) from None
                finally:
                    receiving.cancel()  # when the toggle failed
                if json.loads(message).get("board_id") != self.board_id:
                    raise RuntimeError(f"Expected an event of board {self.board_id}: {message}")
                if trial >= WARMUP_REQUESTS:
                    samples.append((received_at - started) * 1000)
        return samples


async def run_scenarios(
    target: Target, *, requests: int, categories: int, items: int, events: int = EVENT_TRIALS
) -> dict[str, list[float]]:
    seeded = await target.seed(categories, items)
    category_ids = itertools.cycle(seeded.category_ids)
    item_ids = itertools.cycle(seeded.item_ids)
    new_names = (f"added {n}" for n in itertools.count())

    async def add_item() -> None:
        await target.add_item(next(category_ids), next(new_names))

    async def toggle_item() -> None:
        await target.toggle_item(next(item_ids))

    actions: dict[str, Callable[[], Awaitable[None]]] = {"view_board": target.view_board}
    if isinstance(target, V2Target):
        actions["view_board_content"] = target.view_board_content
    actions |= {"add_item": add_item, "toggle_item": toggle_item}
    results = {name: await measure(action, requests) for name, action in actions.items()}
    if isinstance(target, V2Target):
        results["ws_event"] = await target.event_latencies(item_ids, events)
    return results


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Latency benchmark for EasyToDo v1 and v2.")
    parser.add_argument("--target", choices=("legacy", "v2"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", help="v2 only: account to log in with")
    parser.add_argument("--password", help="v2 only")
    parser.add_argument("--requests", type=int, default=200, help="measured requests per scenario")
    parser.add_argument("--categories", type=int, default=20)
    parser.add_argument("--items", type=int, default=30, help="items per category")
    parser.add_argument(
        "--realistic",
        action="store_true",
        help="v2 only: a shared board, with checklists and files (see the module docstring)",
    )
    args = parser.parse_args(argv)
    if args.target == "v2" and not (args.email and args.password):
        parser.error("--target v2 requires --email and --password")
    if args.realistic and args.target != "v2":
        parser.error("--realistic requires --target v2")
    return args


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    async with (
        httpx.AsyncClient(base_url=args.base_url, timeout=30) as client,
        httpx.AsyncClient(base_url=args.base_url, timeout=30) as second_client,
        httpx.AsyncClient(base_url=args.base_url, timeout=30) as member_client,
    ):
        target: Target
        if args.target == "legacy":
            target = LegacyTarget(client)
        else:
            target = V2Target(
                client,
                second_client,
                email=args.email,
                password=args.password,
                member_client=member_client if args.realistic else None,
            )
        results = await run_scenarios(
            target, requests=args.requests, categories=args.categories, items=args.items
        )
    print(format_table(results))


if __name__ == "__main__":
    asyncio.run(main())
