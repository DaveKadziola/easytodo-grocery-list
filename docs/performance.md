# Performance

v2 must not be slower than v1 where users feel it. The acceptance criterion: for **view board**,
**add item** and **toggle item**, the v2 p95 is at most the v1 p95 in the table below, on the same
machine, with the same seed (20 categories x 30 items) and 200 measured requests per scenario.

## How it is measured

`scripts/bench.py` seeds a board, then times three scenarios with one request at a time (no
concurrency), using `time.perf_counter` around each request including the response body. Each
scenario first sends 10 warm-up requests that are not measured. Scenarios run in the order below
on the same seed, so `add_item` grows the board (200 more items, spread over the categories) before
`toggle_item` runs. Percentiles use linear interpolation, like
`statistics.quantiles(method="inclusive")`.

| scenario | v1 (legacy) | v2 |
|---|---|---|
| `view_board` | `GET /` | `GET /boards/{id}` |
| `add_item` | `POST /v1/add_task/` | `POST /categories/{id}/items` |
| `toggle_item` | `PUT /v1/toggle_task/{id}` | `POST /items/{id}/toggle` |

Each side uses the requests its own web client sends: v1 adds and toggles through its JSON API and
updates the page in the browser, v2 answers with HTML fragments. The numbers therefore compare what
the browser waits for per action.

```
cd backend
uv run python ../scripts/bench.py --target legacy --base-url http://localhost:8100
uv run python ../scripts/bench.py --target v2 --base-url http://localhost:8000 \
    --email bench@example.com --password bench-password-123
```

For v2, `--base-url` must equal the app's `EASYTODO_PUBLIC_URL` (the CSRF check compares `Origin`).
v1 has a single global board, so `GET /` renders every category in the database: measure it on an
empty database, as below.

## Baseline v1 (legacy)

- **Date:** 2026-10-20
- **Machine:** 4 vCPU Intel Xeon @ 2.80 GHz (virtual machine), 16 GiB RAM, Ubuntu 24.04, Linux 6.18.
  The app, its database and the benchmark client all ran on this one machine over loopback.
- **Load:** 200 measured requests per scenario (plus 10 warm-up), sequential, seed of 20 categories
  x 30 items (600 items).
- **Result:** median of 3 runs, each on a freshly emptied database. These p95 values are the limits
  for v2.

| scenario | p50 ms | p95 ms |
| --- | ---: | ---: |
| view_board | 138.61 | 201.00 |
| add_item | 72.83 | 118.32 |
| toggle_item | 71.41 | 112.46 |

Individual runs:

| run | view_board p50 / p95 | add_item p50 / p95 | toggle_item p50 / p95 |
| --- | ---: | ---: | ---: |
| 1 | 138.61 / 201.00 | 76.57 / 118.32 | 71.41 / 118.36 |
| 2 | 124.62 / 179.13 | 72.57 / 119.08 | 73.41 / 112.46 |
| 3 | 142.73 / 213.00 | 72.83 / 116.93 | 71.21 / 104.37 |

### How v1 was run

v1 ran through the same entrypoint as its Docker image (`legacy/v1/docker/v1.0.0/entrypoint.sh`
writes `config.ini` and `socketio.json`, then `python3 run.py`), which starts Flask-SocketIO with
eventlet and `debug=True`, so debug mode and Socket.IO logging are part of the baseline. The stock
`docker compose -f legacy/v1/docker/v1.0.0/with-db/docker-compose.yml up --build` does not work in
this environment, so two parts were run differently:

- **Database:** the compose `db` service, unchanged (same DDL, roles, `pg_hba.conf`, host network,
  port 5433), but with `image: postgres:17-alpine` through an override file. The compose file uses
  the unpinned `postgres` image, and the current latest (18+) refuses to start on a volume mounted
  at `/var/lib/postgresql/data`.
- **Application:** a Python 3.12 virtualenv with `legacy/v1/requirements.txt` (all versions as
  pinned) instead of the image, because the image build needs the Alpine package mirror, which is
  not reachable here. `psycopg2==2.9.10` was replaced by `psycopg2-binary==2.9.10` (same version,
  prebuilt) because the libpq headers needed to compile it are not installed.

To repeat it (the `.env` placeholders in `with-db/.env` filled with local values, restored with
`git checkout` afterwards):

```
docker compose -f legacy/v1/docker/v1.0.0/with-db/docker-compose.yml -f db-override.yml up -d db
# in a scratch copy of legacy/v1/{app,docs,run.py}, with the .env exported and python3 from the venv:
sh entrypoint.sh            # the image's entrypoint, with /main replaced by the scratch directory
# between runs: TRUNCATE prod.tasks, prod.categories, prod.task_assignment,
#               prod.temporary_data, prod.updates_log RESTART IDENTITY CASCADE
```

`db-override.yml` contains only `services: {db: {image: "postgres:17-alpine"}}`. Stop everything
with `docker compose ... down -v` when done.
