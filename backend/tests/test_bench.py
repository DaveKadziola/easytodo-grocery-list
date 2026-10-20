import importlib.util
import json
import random
import statistics
import sys
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
        if (request.method, path) == ("POST", "/items/3/toggle"):
            return httpx.Response(200, text="<section></section>")
        return httpx.Response(500, text=f"unexpected {request.method} {path}")

    client, seen = _recording_client(handler)
    async with client:
        target = bench.V2Target(client, email="bench@example.com", password="bench-password-123")
        seeded = await target.seed(categories=2, items=2)
        await target.view_board()
        await target.add_item(seeded.category_ids[1], "extra")
        await target.toggle_item(3)

    assert seeded.category_ids == [100, 101]
    assert seeded.item_ids == [1, 2, 3, 4]
    routes = [(r.method, r.url.path) for r in seen]
    assert routes[:3] == [("GET", "/login"), ("POST", "/login"), ("POST", "/boards")]
    assert routes[-3:] == [
        ("GET", "/boards/7"),
        ("POST", "/categories/101/items"),
        ("POST", "/items/3/toggle"),
    ]
