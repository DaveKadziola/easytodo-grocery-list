"""Sequential latency benchmark for EasyToDo v1 (legacy) and v2.

Seeds a board (categories x items), then times three scenarios one request at a time and prints
a Markdown table of p50/p95 latencies in milliseconds:

    view_board    legacy: GET /                       v2: GET /boards/{id}
    add_item      legacy: POST /v1/add_task/          v2: POST /categories/{id}/items
    toggle_item   legacy: PUT /v1/toggle_task/{id}    v2: POST /items/{id}/toggle

Every scenario discards 10 warm-up requests before the measured ones. The scenarios run in the
order above on the same seed, so add_item grows the board before toggle_item runs.

    uv run python ../scripts/bench.py --target legacy --base-url http://localhost:8100
    uv run python ../scripts/bench.py --target v2 --base-url http://localhost:8000 \
        --email bench@example.com --password bench-password-123

For --target v2, --base-url must be the app's EASYTODO_PUBLIC_URL (CSRF checks the Origin header).
"""

import argparse
import asyncio
import itertools
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

WARMUP_REQUESTS = 10
LEGACY_HEADERS = {"Accept": "application/vnd.myapi.v1+json", "Content-Type": "application/json"}
CSRF_COOKIE = "easytodo_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"


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


class V2Target:
    """v2: FastAPI + HTMX app; logs in through the form and uses the fragment routes."""

    def __init__(self, client: httpx.AsyncClient, *, email: str, password: str) -> None:
        self.client = client
        self.email = email
        self.password = password
        self.board_id: int | None = None

    async def _login(self) -> None:
        base = self.client.base_url
        self.client.headers["Origin"] = f"{base.scheme}://{base.netloc.decode()}"
        (await self.client.get("/login")).raise_for_status()
        token = self.client.cookies.get(CSRF_COOKIE)
        if token is None:
            raise RuntimeError(f"GET /login did not set the {CSRF_COOKIE} cookie")
        form = {"email": self.email, "password": self.password, CSRF_FIELD: token}
        response = await self.client.post("/login", data=form)
        if response.status_code != 303:
            raise RuntimeError(f"Login failed: HTTP {response.status_code}")
        self.client.headers[CSRF_HEADER] = token

    async def _post(self, path: str, **form: str) -> httpx.Response:
        response = await self.client.post(path, data=form)
        response.raise_for_status()
        return response

    async def seed(self, categories: int, items: int) -> Seeded:
        await self._login()
        run = uuid.uuid4().hex[:8]
        response = await self.client.post("/boards", data={"name": f"bench {run}"})
        match = re.fullmatch(r"/boards/(\d+)", response.headers.get("location", ""))
        if response.status_code != 303 or match is None:
            raise RuntimeError(f"Creating the board failed: HTTP {response.status_code}")
        self.board_id = int(match[1])

        category_ids = []
        for c in range(categories):
            response = await self._post(f"/boards/{self.board_id}/categories", name=f"cat {c}")
            category_id = int(re.search(r'id="category-(\d+)"', response.text)[1])
            category_ids.append(category_id)
            for i in range(items):
                await self._post(f"/categories/{category_id}/items", name=f"item {c}-{i}")

        page = await self.client.get(f"/boards/{self.board_id}")
        page.raise_for_status()
        item_ids = [int(i) for i in dict.fromkeys(re.findall(r'id="item-(\d+)"', page.text))]
        if len(item_ids) != categories * items:
            raise RuntimeError(
                f"Seeded {categories * items} items, the board lists {len(item_ids)}"
            )
        return Seeded(category_ids, item_ids)

    async def view_board(self) -> None:
        (await self.client.get(f"/boards/{self.board_id}")).raise_for_status()

    async def add_item(self, category_id: int, name: str) -> None:
        await self._post(f"/categories/{category_id}/items", name=name)

    async def toggle_item(self, item_id: int) -> None:
        await self._post(f"/items/{item_id}/toggle")


async def run_scenarios(
    target: Target, *, requests: int, categories: int, items: int
) -> dict[str, list[float]]:
    seeded = await target.seed(categories, items)
    category_ids = itertools.cycle(seeded.category_ids)
    item_ids = itertools.cycle(seeded.item_ids)
    new_names = (f"added {n}" for n in itertools.count())

    async def add_item() -> None:
        await target.add_item(next(category_ids), next(new_names))

    async def toggle_item() -> None:
        await target.toggle_item(next(item_ids))

    actions = {"view_board": target.view_board, "add_item": add_item, "toggle_item": toggle_item}
    return {name: await measure(action, requests) for name, action in actions.items()}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Latency benchmark for EasyToDo v1 and v2.")
    parser.add_argument("--target", choices=("legacy", "v2"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", help="v2 only: account to log in with")
    parser.add_argument("--password", help="v2 only")
    parser.add_argument("--requests", type=int, default=200, help="measured requests per scenario")
    parser.add_argument("--categories", type=int, default=20)
    parser.add_argument("--items", type=int, default=30, help="items per category")
    args = parser.parse_args(argv)
    if args.target == "v2" and not (args.email and args.password):
        parser.error("--target v2 requires --email and --password")
    return args


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        target: Target
        if args.target == "legacy":
            target = LegacyTarget(client)
        else:
            target = V2Target(client, email=args.email, password=args.password)
        results = await run_scenarios(
            target, requests=args.requests, categories=args.categories, items=args.items
        )
    print(format_table(results))


if __name__ == "__main__":
    asyncio.run(main())
