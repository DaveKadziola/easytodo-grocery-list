import asyncio
import importlib.util
import json
import random
import re
import statistics
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

BENCH_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bench.py"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench", BENCH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench"] = module
    spec.loader.exec_module(module)
    return module


bench = _load_bench()
percentile = bench.percentile


def test_percentile_matches_inclusive_quantiles():
    data = [float(x) for x in range(1, 101)]
    assert percentile(data, 50) == pytest.approx(50.5)
    assert percentile(data, 95) == pytest.approx(95.05)


def test_percentile_agrees_with_statistics_quantiles_on_unsorted_data():
    rng = random.Random(7)
    data = [rng.uniform(0.5, 80.0) for _ in range(237)]
    cuts = statistics.quantiles(data, n=100, method="inclusive")
    assert percentile(data, 50) == pytest.approx(cuts[49])
    assert percentile(data, 95) == pytest.approx(cuts[94])


def test_percentile_of_empty_data_is_an_error():
    with pytest.raises(ValueError):
        percentile([], 50)


@pytest.mark.parametrize("p", [-1, 101])
def test_percentile_rejects_p_outside_0_to_100(p):  # triage #9
    with pytest.raises(ValueError, match="between 0 and 100"):
        percentile([1.0, 2.0], p)


def test_parse_args_checks_credentials_and_realistic(capsys):  # triage #9
    base = ["--base-url", "http://bench.test"]
    with pytest.raises(SystemExit):
        bench.parse_args(["--target", "v2", *base])
    assert "--target v2 requires --email and --password" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        bench.parse_args(["--target", "legacy", "--realistic", *base])
    assert "--realistic requires --target v2" in capsys.readouterr().err

    credentials = ["--email", "b@example.com", "--password", "pw"]
    assert bench.parse_args(["--target", "v2", *base, *credentials]).realistic is False
    assert bench.parse_args(["--target", "v2", "--realistic", *base, *credentials]).realistic
    assert bench.parse_args(["--target", "legacy", *base]).realistic is False


async def test_measure_discards_ten_warmup_requests():
    calls = 0

    async def action() -> None:
        nonlocal calls
        calls += 1

    samples = await bench.measure(action, requests=7)
    assert calls == 10 + 7
    assert len(samples) == 7
    assert all(s >= 0 for s in samples)


def test_format_table_prints_markdown_rows():
    table = bench.format_table({"view_board": [1.0, 2.0, 3.0], "add_item": [10.0] * 5})
    lines = table.splitlines()
    assert lines[0] == "| scenario | p50 ms | p95 ms |"
    assert lines[1].startswith("| ---")
    assert lines[2] == "| view_board | 2.00 | 2.90 |"
    assert lines[3] == "| add_item | 10.00 | 10.00 |"


def _recording_client(handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url="http://bench.test"
    ), seen


async def test_legacy_target_uses_the_v1_json_api():
    ids = {"category": 0, "task": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/add_category":
            ids["category"] += 1
            return httpx.Response(200, json={"status": "success", "category_id": ids["category"]})
        if path == "/v1/add_task/":
            ids["task"] += 1
            return httpx.Response(200, json={"status": "success", "task_id": ids["task"]})
        if (request.method, path) == ("GET", "/"):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(200, json={"status": "success"})

    client, seen = _recording_client(handler)
    async with client:
        target = bench.LegacyTarget(client)
        seeded = await target.seed(categories=2, items=3)
        seed_requests = len(seen)
        await target.view_board()
        await target.toggle_item(seeded.item_ids[0])
        await target.toggle_item(seeded.item_ids[0])

    assert seeded.category_ids == [1, 2]
    assert seeded.item_ids == [1, 2, 3, 4, 5, 6]
    assert seed_requests == 2 + 6
    for request in seen[:seed_requests]:
        assert request.headers["accept"] == "application/vnd.myapi.v1+json"
        assert request.headers["content-type"] == "application/json"
    assert json.loads(seen[2].content)["category_id"] == 1
    view, first, second = seen[seed_requests:]
    assert (view.method, view.url.path) == ("GET", "/")
    assert (first.method, first.url.path) == ("PUT", "/v1/toggle_task/1")
    assert first.headers["accept"] == "application/vnd.myapi.v1+json"
    assert json.loads(first.content) == {"is_done": True}
    assert json.loads(second.content) == {"is_done": False}


async def test_v2_target_logs_in_with_the_form_and_uses_the_htmx_routes():
    csrf = "csrf-token-value"
    category_ids = iter(range(100, 200))
    created_items: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if (request.method, path) == ("GET", "/login"):
            return httpx.Response(200, headers={"set-cookie": f"easytodo_csrf={csrf}; Path=/"})
        if (request.method, path) == ("POST", "/login"):
            form = parse_qs(request.content.decode())
            assert form["email"] == ["bench@example.com"]
            assert form["password"] == ["bench-password-123"]
            assert form["csrf_token"] == [csrf]
            return httpx.Response(
                303, headers={"location": "/", "set-cookie": "easytodo_session=s"}
            )
        # Every state-changing request must carry the CSRF header and a matching Origin.
        if request.method == "POST":
            assert request.headers["x-csrf-token"] == csrf
            assert request.headers["origin"] == "http://bench.test"
        if (request.method, path) == ("POST", "/boards"):
            return httpx.Response(303, headers={"location": "/boards/7"})
        if (request.method, path) == ("POST", "/boards/7/categories"):
            return httpx.Response(
                201, text=f'<section id="category-{next(category_ids)}"></section>'
            )
        if request.method == "POST" and path.startswith("/categories/") and path.endswith("/items"):
            created_items.append(len(created_items) + 1)
            return httpx.Response(200, text="<section></section>")
        if (request.method, path) == ("GET", "/boards/7"):
            html = "".join(f'<li id="item-{i}"></li>' for i in created_items)
            return httpx.Response(200, text=html)
        if (request.method, path) == ("GET", "/boards/7/content"):
            return httpx.Response(200, text='<div id="board-content"></div>')
        if (request.method, path) == ("POST", "/items/3/toggle"):
            return httpx.Response(200, text="<section></section>")
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    client, seen = _recording_client(handler)
    second, _ = _recording_client(handler)
    async with client, second:
        target = bench.V2Target(
            client, second, email="bench@example.com", password="bench-password-123"
        )
        seeded = await target.seed(categories=2, items=2)
        await target.view_board()
        await target.view_board_content()
        await target.add_item(seeded.category_ids[1], "extra")
        await target.toggle_item(3)

    assert seeded.category_ids == [100, 101]
    assert seeded.item_ids == [1, 2, 3, 4]
    routes = [(r.method, r.url.path) for r in seen]
    assert routes[:3] == [("GET", "/login"), ("POST", "/login"), ("POST", "/boards")]
    assert routes[-4:] == [
        ("GET", "/boards/7"),
        ("GET", "/boards/7/content"),
        ("POST", "/categories/101/items"),
        ("POST", "/items/3/toggle"),
    ]


def _v2_handler(*, on_toggle=lambda item_id: None):
    """A v2 server stub: logins (a new session each), board 7 with one category of items 1-3."""
    sessions = iter(f"session-{n}" for n in range(1, 100))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if (request.method, path) == ("GET", "/login"):
            return httpx.Response(200, headers={"set-cookie": "easytodo_csrf=csrf; Path=/"})
        if (request.method, path) == ("POST", "/login"):
            cookie = f"easytodo_session={next(sessions)}; Path=/"
            return httpx.Response(303, headers={"location": "/", "set-cookie": cookie})
        if (request.method, path) == ("POST", "/boards"):
            return httpx.Response(303, headers={"location": "/boards/7"})
        if (request.method, path) == ("POST", "/boards/7/categories"):
            return httpx.Response(201, text='<section id="category-100"></section>')
        if (request.method, path) == ("GET", "/boards/7"):
            return httpx.Response(200, text="".join(f'<li id="item-{i}"></li>' for i in (1, 2, 3)))
        if request.method == "POST" and path.startswith("/items/") and path.endswith("/toggle"):
            on_toggle(int(path.split("/")[2]))
            return httpx.Response(200, text="<section></section>")
        if request.method == "POST":  # /categories/100/items
            return httpx.Response(200, text="<section></section>")
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    return handler


async def test_v2_seed_names_the_route_when_a_category_fragment_has_no_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/boards/7/categories":
            return httpx.Response(201, text="<section></section>")
        return _v2_handler()(request)

    client, _ = _recording_client(handler)
    second, _ = _recording_client(handler)
    async with client, second:
        target = bench.V2Target(client, second, email="b@example.com", password="pw")
        with pytest.raises(RuntimeError, match="POST /boards/7/categories"):
            await target.seed(categories=1, items=3)


class FakeSocket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[str] = asyncio.Queue()

    async def recv(self) -> str:
        return await self.messages.get()


async def test_v2_event_latencies_time_a_toggle_until_a_second_clients_socket_has_it(
    monkeypatch,
):
    socket = FakeSocket()
    opened = []
    toggled = []

    @asynccontextmanager
    async def fake_connect(url, *, origin, additional_headers):
        opened.append((url, origin, additional_headers))
        yield socket

    def on_toggle(item_id: int) -> None:
        toggled.append(item_id)
        socket.messages.put_nowait(
            json.dumps({"type": "category.changed", "board_id": 7, "category_ids": [100]})
        )

    monkeypatch.setattr(bench, "connect", fake_connect)
    handler = _v2_handler(on_toggle=on_toggle)  # one server: each login gets a new session
    client, seen = _recording_client(handler)
    second, second_seen = _recording_client(handler)
    async with client, second:
        target = bench.V2Target(client, second, email="b@example.com", password="pw")
        seeded = await target.seed(categories=1, items=3)
        samples = await target.event_latencies(iter(seeded.item_ids * 10), trials=5)

    assert len(samples) == 5
    assert all(s >= 0 for s in samples)
    assert toggled == (seeded.item_ids * 10)[: 10 + 5]  # 10 warm-up changes are not timed
    # The socket belongs to the second client: its own session and its own tab id.
    assert [(r.method, r.url.path) for r in second_seen] == [("GET", "/login"), ("POST", "/login")]
    assert opened == [
        (
            "ws://bench.test/ws/boards/7?client_id=bench-second-client",
            "http://bench.test",
            {"Cookie": "easytodo_session=session-2"},
        )
    ]
    assert client.cookies["easytodo_session"] == "session-1"
    assert seen[-1].headers["x-client-id"] == "bench"  # the change comes from another tab


async def test_v2_event_latencies_reject_an_event_of_another_board(monkeypatch):
    socket = FakeSocket()

    @asynccontextmanager
    async def fake_connect(url, *, origin, additional_headers):
        yield socket

    def on_toggle(item_id: int) -> None:
        socket.messages.put_nowait(json.dumps({"type": "category.changed", "board_id": 8}))

    monkeypatch.setattr(bench, "connect", fake_connect)
    client, _ = _recording_client(_v2_handler(on_toggle=on_toggle))
    second, _ = _recording_client(_v2_handler())
    async with client, second:
        target = bench.V2Target(client, second, email="b@example.com", password="pw")
        seeded = await target.seed(categories=1, items=3)
        with pytest.raises(RuntimeError, match="board 7"):
            await target.event_latencies(iter(seeded.item_ids * 10), trials=5)


async def test_v2_event_latencies_name_the_board_and_item_on_a_timeout(monkeypatch):  # triage #151
    socket = FakeSocket()  # stays silent: no event ever arrives

    @asynccontextmanager
    async def fake_connect(url, *, origin, additional_headers):
        yield socket

    monkeypatch.setattr(bench, "connect", fake_connect)
    monkeypatch.setattr(bench, "EVENT_TIMEOUT_S", 0.05)
    client, _ = _recording_client(_v2_handler())
    second, _ = _recording_client(_v2_handler())
    async with client, second:
        target = bench.V2Target(client, second, email="b@example.com", password="pw")
        seeded = await target.seed(categories=1, items=3)
        with pytest.raises(
            RuntimeError, match=r"No event on board 7 for item 1 within 0\.05 s"
        ) as raised:
            await target.event_latencies(iter(seeded.item_ids), trials=5)
    assert raised.value.__suppress_context__  # `from None`: no TimeoutError chained under it


def _realistic_handler(
    *, invite_form: dict, attachments: list[httpx.Request], accept_status: int = 303
):
    """One server stub for the owner's, the second and the member's client: board 7 whose items
    are numbered in the order they were added, the invite page of ``/invite/tok``."""
    sessions = iter(f"session-{n}" for n in range(1, 100))
    items: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        route = (request.method, path)
        if request.method == "POST":  # what the browser sends on every change
            assert request.headers.get("x-csrf-token") == "csrf" or path in ("/login", "/register")
            assert request.headers["origin"] == "http://bench.test"
        if route in {("GET", "/login"), ("GET", "/register")}:
            return httpx.Response(200, headers={"set-cookie": "easytodo_csrf=csrf; Path=/"})
        if route in {("POST", "/login"), ("POST", "/register")}:
            cookie = f"easytodo_session={next(sessions)}; Path=/"
            return httpx.Response(303, headers={"location": "/", "set-cookie": cookie})
        if route == ("POST", "/invite/tok/accept"):
            return httpx.Response(accept_status, headers={"location": "/boards/7"})
        if route == ("POST", "/boards"):
            return httpx.Response(303, headers={"location": "/boards/7"})
        if route == ("POST", "/boards/7/invites"):
            invite_form.update(parse_qs(request.content.decode()))
            link = '<input id="invite-link" value="http://public.test/invite/tok" readonly>'
            return httpx.Response(201, text=link)
        if route == ("POST", "/boards/7/categories"):
            return httpx.Response(201, text='<section id="category-100"></section>')
        if request.method == "POST" and path.endswith("/items"):
            items.append(len(items) + 1)
            return httpx.Response(200, text="<section></section>")
        if request.method == "POST" and path.endswith("/checklist"):
            return httpx.Response(200, text="<div></div>")
        if request.method == "POST" and path.endswith("/attachments"):
            attachments.append(request)
            return httpx.Response(200, text='<li data-attachment-id="1"></li>')
        if route == ("GET", "/boards/7"):
            return httpx.Response(200, text="".join(f'<li id="item-{i}"></li>' for i in items))
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    return handler


def _routes(seen: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in seen]


async def test_v2_realistic_seed_shares_the_board_and_adds_checklists_and_attachments():
    invite_form: dict = {}
    attachments: list[httpx.Request] = []
    handler = _realistic_handler(invite_form=invite_form, attachments=attachments)
    owner, owner_seen = _recording_client(handler)
    second, _ = _recording_client(handler)
    member, member_seen = _recording_client(handler)
    async with owner, second, member:
        target = bench.V2Target(
            owner,
            second,
            email="bench@example.com",
            password="bench-password-123",
            member_client=member,
        )
        seeded = await target.seed(categories=2, items=10)

    assert seeded.category_ids == [100, 100]
    assert len(seeded.item_ids) == 20  # item-1 ... item-20, as the board page lists them

    # The member signs up through the invite page (the host of the link is not used) and accepts.
    member_routes = _routes(member_seen)
    assert member_routes[:3] == [
        ("GET", "/register"),
        ("POST", "/register"),
        ("POST", "/invite/tok/accept"),
    ]
    assert member_seen[0].url.params["next"] == "/invite/tok"
    assert member_seen[1].url.params["next"] == "/invite/tok"
    assert {r.url.host for r in member_seen} == {"bench.test"}
    form = parse_qs(member_seen[1].content.decode())
    assert re.fullmatch(r"bench-member-[0-9a-f]{8}@example\.com", form["email"][0])
    assert form["password"] == ["bench-password-123"] and form["csrf_token"] == ["csrf"]
    # A link for an editor, without an e-mail address.
    assert invite_form["role"] == ["editor"] and "email" not in invite_form

    # The two accounts add one task each in turn: the member the ones with an odd number.
    owner_item_posts = [
        r for r in owner_seen if r.method == "POST" and r.url.path.endswith("/items")
    ]
    member_item_posts = [r for r in member_seen if r.url.path.endswith("/items")]
    assert (len(owner_item_posts), len(member_item_posts)) == (10, 10)
    assert [parse_qs(r.content.decode())["name"][0] for r in member_item_posts] == [
        f"item {c}-{i}" for c in range(2) for i in (1, 3, 5, 7, 9)
    ]

    # Every 10th task gets 5 checklist entries and a file, from the owner.
    checklist_paths = [r.url.path for r in owner_seen if r.url.path.endswith("/checklist")]
    assert checklist_paths == ["/items/1/checklist"] * 5 + ["/items/11/checklist"] * 5
    assert [r.url.path for r in attachments] == ["/items/1/attachments", "/items/11/attachments"]
    assert all(r.headers["content-type"].startswith("multipart/form-data") for r in attachments)
    assert b'filename="note.txt"' in attachments[0].content
    assert b"EasyToDo bench\n" in attachments[0].content
    assert attachments[0].headers["x-csrf-token"] == "csrf"


async def test_v2_realistic_seed_names_the_failed_step_when_joining(monkeypatch):
    handler = _realistic_handler(invite_form={}, attachments=[], accept_status=403)
    owner, _ = _recording_client(handler)
    second, _ = _recording_client(handler)
    member, _ = _recording_client(handler)
    async with owner, second, member:
        target = bench.V2Target(
            owner, second, email="b@example.com", password="pw", member_client=member
        )
        with pytest.raises(RuntimeError, match=r"accepting the invite.*HTTP 403"):
            await target.seed(categories=1, items=2)


async def test_v2_seed_without_a_member_client_is_the_plain_seed():
    handler = _realistic_handler(invite_form={}, attachments=[])
    owner, owner_seen = _recording_client(handler)
    second, _ = _recording_client(handler)
    async with owner, second:
        target = bench.V2Target(owner, second, email="b@example.com", password="pw")
        await target.seed(categories=1, items=2)
    paths = [r.url.path for r in owner_seen]
    assert not any(p.endswith(("/invites", "/checklist", "/attachments")) for p in paths)


class RecordingTarget:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def seed(self, categories: int, items: int):
        return bench.Seeded([1, 2], [11, 12, 13])

    async def view_board(self) -> None:
        self.calls.append("view_board")

    async def add_item(self, category_id: int, name: str) -> None:
        self.calls.append("add_item")

    async def toggle_item(self, item_id: int) -> None:
        self.calls.append("toggle_item")


class RecordingV2Target(RecordingTarget, bench.V2Target):
    async def view_board_content(self) -> None:
        self.calls.append("view_board_content")

    async def event_latencies(self, item_ids, trials: int) -> list[float]:
        self.calls.append("event_latencies")
        self.event_items = [next(item_ids) for _ in range(3)]
        return [1.0] * trials


async def test_run_scenarios_measures_the_three_scenarios_for_v1():
    target = RecordingTarget()
    results = await bench.run_scenarios(target, requests=4, categories=2, items=3)

    assert list(results) == ["view_board", "add_item", "toggle_item"]
    assert all(len(samples) == 4 for samples in results.values())


async def test_run_scenarios_adds_the_content_fragment_and_ws_event_for_v2():
    target = RecordingV2Target()
    results = await bench.run_scenarios(target, requests=4, categories=2, items=3, events=6)

    assert list(results) == [
        "view_board",
        "view_board_content",
        "add_item",
        "toggle_item",
        "ws_event",
    ]
    assert [len(samples) for samples in results.values()] == [4, 4, 4, 4, 6]
    # The fragment is timed on the seeded board, before add_item grows it.
    calls = list(dict.fromkeys(target.calls))
    assert calls == [
        "view_board",
        "view_board_content",
        "add_item",
        "toggle_item",
        "event_latencies",
    ]
    assert set(target.event_items) <= {11, 12, 13}
