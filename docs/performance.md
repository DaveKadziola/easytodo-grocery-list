# Performance

v2 must not be slower than v1 where users feel it. The acceptance criterion: for **view board**,
**add item** and **toggle item**, the v2 p95 is at most the v1 p95 of the baseline below, on the
same machine, with the same seed (20 categories x 30 items) and 200 measured requests per scenario.

## Result

Measured on 2026-10-21. v1 and v2 ran one after the other on the same machine as the baseline:
a 4 vCPU Intel Xeon @ 2.80 GHz virtual machine with 16 GiB RAM, Ubuntu 24.04, Linux 6.18. The
app, its database and the benchmark client all ran on that machine, over loopback. Each value is
the per-column median of 3 runs. Every run started on an empty database.

| scenario | v1 p50 ms | v1 p95 ms | v2 p50 ms | v2 p95 ms | limit: v1 baseline p95 ms | result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| view_board | 151.77 | 228.64 | 46.56 | 128.50 | 201.00 | pass |
| add_item | 78.70 | 120.21 | 15.96 | 20.76 | 118.32 | pass |
| toggle_item | 73.80 | 119.03 | 16.98 | 22.39 | 112.46 | pass |

Every v2 p95 is below the limit. The limit is the baseline measured on 2026-10-20, before the
rewrite. Measured again on 2026-10-21, v1 was 2 to 14% slower at p95 than that baseline, so the
v2 values are held to the lower, older numbers.

v2 only:

| scenario | p50 ms | p95 ms |
| --- | ---: | ---: |
| view_board_content (`GET /boards/{id}/content`) | 43.39 | 127.92 |
| ws_event: change to WebSocket event at a second client, 100 changes | 11.19 | 14.57 |

- **A board view in the browser makes two requests.** The page itself (`GET /boards/{id}`, the
  view_board row) arrives with the whole board rendered. Once the page's WebSocket is open,
  `realtime.js` fetches the board once more (`GET /boards/{id}/content`, the view_board_content
  row), so that nothing changed between the two is missed. The user sees the board after the
  first request. Per board view, the server therefore does about twice the work of one request:
  about 90 ms at p50 for both, against 152 ms for v1's single `GET /`. The criterion is per
  request, and each of the two is below v1's p95. v1's page also makes more requests after it
  has loaded (it fetches `socketio.json`, opens its Socket.IO connection and asks for the updates
  since its last timestamp); those are not timed either.
- **ws_event** runs from sending a toggle until a second client gets the event on the board's
  WebSocket. The second client is another login of the same account, as on a second phone. It
  usually arrives before the response to the toggle itself, because the event leaves at the
  commit and the response is rendered after it. The second client then fetches the changed
  category (`GET /categories/{id}`), which is not included.
- **The v2 tail.** In v2, the p95 of the two board reads is about 2.8 times their p50. About one
  request in seven includes one of Python's full garbage collections, which takes about 80 ms
  with 600-800 items on the page. This was measured with a `gc` callback on the same code. It is
  not a leak: the number of live objects stays flat (about 135,000).

Individual runs, p50 / p95 in ms:

| run | view_board | view_board_content | add_item | toggle_item | ws_event |
| --- | ---: | ---: | ---: | ---: | ---: |
| v1, 1 | 142.04 / 219.85 | | 78.70 / 120.21 | 73.80 / 108.87 | |
| v1, 2 | 151.77 / 228.64 | | 85.84 / 137.54 | 72.96 / 119.03 | |
| v1, 3 | 159.96 / 247.21 | | 76.54 / 119.47 | 76.99 / 125.77 | |
| v2, 1 | 46.56 / 125.99 | 43.39 / 127.92 | 15.96 / 20.76 | 16.98 / 23.53 | 11.47 / 14.62 |
| v2, 2 | 46.02 / 128.50 | 42.79 / 126.24 | 15.29 / 19.52 | 14.48 / 21.94 | 10.27 / 14.57 |
| v2, 3 | 47.11 / 133.19 | 43.57 / 134.70 | 16.43 / 22.46 | 17.00 / 22.39 | 11.19 / 14.07 |

## How it is measured

`scripts/bench.py` seeds a board, then times the scenarios with one request at a time (no
concurrency), using `time.perf_counter` around each request, response body included. Each
scenario first sends 10 warm-up requests that are not measured. Scenarios run in the order of the
table below on the same seed. `add_item` adds 210 items (200 measured and 10 warm-up), spread
over the categories, before `toggle_item` runs. Percentiles use linear interpolation, like
`statistics.quantiles(method="inclusive")`.

| scenario | v1 (legacy) | v2 |
|---|---|---|
| `view_board` | `GET /` | `GET /boards/{id}` |
| `view_board_content` | | `GET /boards/{id}/content`, right after `view_board` |
| `add_item` | `POST /v1/add_task/` | `POST /categories/{id}/items` |
| `toggle_item` | `PUT /v1/toggle_task/{id}` | `POST /items/{id}/toggle` |
| `ws_event` | | `POST /items/{id}/toggle` until the event arrives on a second login's `WS /ws/boards/{id}` (10 warm-up + 100 changes) |

Each side uses the requests its own web client sends: v1 adds and toggles through its JSON API and
updates the page in the browser, v2 answers with HTML fragments. The numbers therefore compare what
the browser waits for per action. The v2 client sends the headers the browser sends (CSRF token,
`Origin`, `X-Client-Id`).

Each version is measured 3 times, each run on an empty database, and the table takes the median of
each column separately. v1's `GET /` renders every category in the database, so v1 has to start
empty for its runs to compare. v2 starts empty to match.

### v2

The docker compose stack of this repository: the image built from `deploy/Dockerfile`, one
Uvicorn worker, `.env` as the README's quick start makes it (`cp .env.example .env` plus its
`sed` line, which fills in the required `EASYTODO_SECRET_KEY`; `EASYTODO_PUBLIC_URL` stays
`http://localhost:8000`, which `--base-url` must equal because the CSRF check compares the
`Origin`). Per run, from the
repository root (`down -v` deletes the database, so use a checkout that holds no data you need):

```sh
docker compose down -v && docker compose up -d --build --wait
docker compose exec app python -m easytodo.cli create-admin --email bench@example.com --password-stdin <<< "bench-password-123"
cd backend && uv run python ../scripts/bench.py --target v2 --base-url http://localhost:8000 \
    --email bench@example.com --password bench-password-123 && cd ..
```

### v1 (legacy)

v1's code is commit `0fcbd9c` (tagged `legacy-v1`). It ran through the same entrypoint as its
Docker image (`docker/v1.0.0/entrypoint.sh` writes `config.ini` and `socketio.json`, then runs
`python3 run.py`), which starts Flask-SocketIO with eventlet and `debug=True`, so debug mode and
Socket.IO logging are part of the numbers. The stock `docker compose -f
docker/v1.0.0/with-db/docker-compose.yml up --build` does not work on this machine, so two parts
ran differently:

- **Database:** the compose `db` service, unchanged (same DDL, roles, `pg_hba.conf`, host network,
  port 5433), but with `image: postgres:17-alpine` through an override file. The compose file uses
  the unpinned `postgres` image, and the current latest (18+) refuses to start on a volume mounted
  at `/var/lib/postgresql/data`.
- **Application:** a Python 3.12 virtualenv with v1's `requirements.txt` (all versions as pinned)
  instead of the image, because the image build needs the Alpine package mirror, which is not
  reachable here. `psycopg2==2.9.10` was replaced by `psycopg2-binary==2.9.10` (same version,
  prebuilt) because the libpq headers needed to compile it are not installed.

To repeat it, from the repository root (`$V1` is a scratch directory; the passwords are local
throwaway values):

```sh
V1=$(mktemp -d)
git worktree add "$V1/code" 0fcbd9c   # v1's last commit (tag legacy-v1)
cat > "$V1/v1.env" <<'EOF'
DB_HOST=127.0.0.1
DB_PORT=5433
DB_NAME=todo_grocery
DB_USER=prod_todo_grocery
DB_PASSWORD=bench-user-pw
DB_SCHEMA=prod
DB_ROOT_USER=postgres
DB_ROOT_PASSWORD=bench-root-pw
APP_HOST_NAME=0.0.0.0
APP_HOST_PORT=8100
SOCKETIO_HOST=0.0.0.0
SOCKETIO_PORT=8100
EOF
printf 'services:\n  db:\n    image: "postgres:17-alpine"\n' > "$V1/db-override.yml"
V1DB="docker compose --env-file $V1/v1.env -f $V1/code/docker/v1.0.0/with-db/docker-compose.yml -f $V1/db-override.yml"
$V1DB up -d --wait db

python3.12 -m venv "$V1/venv"
sed 's/^psycopg2==/psycopg2-binary==/' "$V1/code/requirements.txt" > "$V1/requirements.txt"
"$V1/venv/bin/pip" install -r "$V1/requirements.txt"
sed -i "s#/main#$V1/code#g" "$V1/code/docker/v1.0.0/entrypoint.sh"   # the image's entrypoint
(cd "$V1/code" && set -a && . "$V1/v1.env" && PATH="$V1/venv/bin:$PATH" exec sh docker/v1.0.0/entrypoint.sh) > "$V1/app.log" 2>&1 &
until curl -fs -o /dev/null http://localhost:8100/; do sleep 1; done

cd backend && uv run python ../scripts/bench.py --target legacy --base-url http://localhost:8100 && cd ..
# before each further run, empty the database:
$V1DB exec -e PGPASSWORD=bench-root-pw db psql -U postgres -d todo_grocery -c \
    "TRUNCATE prod.tasks, prod.categories, prod.task_assignment, prod.temporary_data, prod.updates_log RESTART IDENTITY CASCADE"
```

When done, stop `python3 run.py`, then run `$V1DB down -v` and `git worktree remove --force
"$V1/code"`.

## Baseline v1 (legacy), 2026-10-20

Measured before the rewrite, by the method above, on the same machine. These p95 values are the
limits for v2.

| scenario | p50 ms | p95 ms |
| --- | ---: | ---: |
| view_board | 138.61 | 201.00 |
| add_item | 72.83 | 118.32 |
| toggle_item | 71.41 | 112.46 |

Individual runs, p50 / p95 in ms (the table above takes the median of each column):

| run | view_board | add_item | toggle_item |
| --- | ---: | ---: | ---: |
| 1 | 138.61 / 201.00 | 76.57 / 118.32 | 71.41 / 118.36 |
| 2 | 124.62 / 179.13 | 72.57 / 119.08 | 73.41 / 112.46 |
| 3 | 142.73 / 213.00 | 72.83 / 116.93 | 71.21 / 104.37 |
