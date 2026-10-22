# Moving from v1 to v2

v2 is a new application with its own database. Nothing is converted in place. You start v2 next to
v1, and one command copies v1's list into a board of your v2 account. v1 keeps running unchanged
until you switch it off at the end. The v1 code stays available in the git history: commit
`0fcbd9c` (tagged `legacy-v1`), e.g. `git switch --detach 0fcbd9c`.

**What is copied.** v1 has one shared list. The import puts it into **one new board** owned by a
v2 account:

- Categories keep v1's order.
- Tasks become items in the category they were last assigned to. Tasks without a category go to a
  category named "Uncategorized".
- Each item keeps its description, its done state and its created/updated times. v1 stored times
  without a time zone; they are read as local time in `--legacy-tz` (default `Europe/Warsaw`).
  With v1's Docker setups, pass `--legacy-tz UTC` (see step 4).
- v1's internal tables (`updates_log`, `temporary_data`) are not copied.

Changes made in v1 after the import are not copied. Stop using v1 before you start, or import
again later (see the notes in step 4).

The commands below assume v1 runs with its Docker setup `with-db` (`docker/v1.0.0/with-db`, with
the values from its `.env`: database `todo_grocery`, schema `prod`, port 5433). Where a v1
installed with `setup.py` (no Docker) or `no-db` needs something else, the step says so.

## 1. Back up the v1 database

In your v1 directory:

```sh
docker compose -f docker/v1.0.0/with-db/docker-compose.yml exec db \
    pg_dump -U postgres -Fc -f /tmp/easytodo-v1.dump todo_grocery
docker compose -f docker/v1.0.0/with-db/docker-compose.yml cp db:/tmp/easytodo-v1.dump .
```

`pg_dump` asks for the password of `postgres` (`DB_ROOT_PASSWORD` in
`docker/v1.0.0/with-db/.env`; use `DB_ROOT_USER` and `DB_NAME` from there if you changed them).
Without Docker, run `pg_dump -h localhost -U postgres -Fc -f easytodo-v1.dump todo_grocery` on the
database server instead.

Check that the dump holds v1's tables. This lists `prod categories`, `prod tasks` and
`prod task_assignment`, among others:

```sh
docker compose -f docker/v1.0.0/with-db/docker-compose.yml exec db \
    pg_restore --list /tmp/easytodo-v1.dump | grep "TABLE DATA"
```

Keep `easytodo-v1.dump` somewhere safe until v2 has been in use for a while. The import only reads
from v1, so v1's data stays as it is; the dump is your copy for after v1 is switched off. To load
it back into a v1 database, run `pg_restore -U postgres -d todo_grocery --clean --if-exists
/tmp/easytodo-v1.dump` in the v1 `db` container.

## 2. Start v2 next to v1

v2 runs from its own directory, with its own database container (the `db` service of its
`docker-compose.yml`) and its own volume. Do not `git pull` v2 into your v1 checkout: that
deletes v1's files, the compose files you still need for v1 included. Clone v2 into a new
directory:

```sh
git clone https://github.com/DaveKadziola/easytodo-grocery-list.git easytodo-v2
cd easytodo-v2
```

Then go on with the [README quick start](../README.md#quick-start) from `cp .env.example .env`.

v2 listens on port 8000 (`EASYTODO_PORT`), v1 on its `APP_HOST_PORT` (8100 in `with-db/.env`),
so both run side by side. If v1 uses port 8000, set `EASYTODO_PORT=8001` and
`EASYTODO_PUBLIC_URL=http://<server>:8001` in v2's `.env`.

## 3. Create the account that gets the data

The new board belongs to an existing v2 account. Use the admin account from the quick start
(`create-admin`), or sign up at `/register` first. Other people get access later through the
board's Members page.

## 4. Import

Run this in the v2 directory. The v2 app container connects to v1's database. Leave the
password out of `--legacy-dsn`, where it would end up in your shell history and in the process
list. Pass it in `PGPASSWORD` instead:

```sh
read -rs PGPASSWORD && export PGPASSWORD      # type the v1 database password, then Enter
docker compose exec -e PGPASSWORD app python -m easytodo.cli import-legacy \
    --legacy-dsn postgresql://prod_todo_grocery@192.168.1.20:5433/todo_grocery \
    --schema prod --owner-email you@example.com --legacy-tz UTC
unset PGPASSWORD
```

- **Host:** the v1 database's address as the v2 container sees it. That is the LAN address of
  the v1 machine (`192.168.1.20` above), also when v1 runs on the same machine as v2: inside the
  container, `localhost` is the container itself. The `with-db` database listens on all addresses
  and accepts password logins from anywhere. A v1 on your own PostgreSQL (`setup.py`, `no-db`)
  usually listens on port 5432 and must accept connections from the Docker network
  (`listen_addresses` and `pg_hba.conf`, as v1's README describes for `no-db`).
- **User:** v1's app user (`DB_USER`, `prod_todo_grocery`) is enough. The import only reads, in
  one read-only transaction, so read access to v1's schema is all it needs. `PGPASSWORD` is that
  user's password (`DB_PASSWORD`). When you run the command outside Docker, `~/.pgpass` works
  too.
- **Schema:** v1's `DB_SCHEMA` (`prod`, or `dev`/`test` if you used those).
- **Options:** `--board-name NAME` (default "Imported from v1") and `--legacy-tz ZONE` (default
  `Europe/Warsaw`, the time zone v1's server ran in). v1's Docker setups (`with-db`, `no-db`) set
  no time zone, so v1 stored UTC times there: pass `--legacy-tz UTC`. Not sure? Run
  `SHOW timezone;` in v1's database (`psql`, as in step 5) and pass what it shows.

The command prints a report, for example:

```
Imported 23 categories and 811 items into the new board "Imported from v1" of you@example.com.
2 categories got another name in v2 (names are tidied, cut to 120 characters and unique ignoring case). In v1:
  dairy  products
  Dairy products
```

Here v1 had "dairy  products" (two spaces) and "Dairy products": in v2 they are
"dairy products" and "Dairy products (2)".

Notes:

- **Every run creates another board.** To import again (say, after more changes in v1), delete the
  earlier board first, or you get the data twice: "Archive board" in the ⋮ menu next to the
  board's name, then "Delete permanently" on the "Archived boards" page (`/boards/archived`).
- **Names are tidied and made unique.** Runs of spaces become one space. Two categories whose
  names differ only in letter case get " (2)", " (3)"... The report lists the v1 names that
  changed.
- **Long text is cut, and the report does not say so.** v2 allows 120 characters for a category
  name, 200 for an item name and 2000 for a description; v1 allowed 255-character names and
  descriptions of any length. Longer text is cut to these limits, and only category renames are
  reported.
- **All or nothing.** The import runs in one transaction. If it stops with an error (a wrong
  password, an unknown schema, a blank v1 name), nothing has been written.

## 5. Check the numbers

In the v1 directory, count v1's tasks and categories (`psql` asks for the same password as
`pg_dump` in step 1):

```sh
docker compose -f docker/v1.0.0/with-db/docker-compose.yml exec db \
    psql -U postgres -d todo_grocery -c "SELECT count(*) FROM prod.tasks" \
    -c "SELECT count(*) FROM prod.categories"
```

The report's item count must equal the tasks count. Its category count equals v1's, plus one
when there were tasks without a category ("Uncategorized"). In the v2 directory, the board itself
gives the same numbers:

```sh
docker compose exec db psql -U easytodo -d easytodo \
    -c "SELECT count(*) FROM items WHERE board_id = (SELECT max(id) FROM boards WHERE name = 'Imported from v1')" \
    -c "SELECT count(*) FROM categories WHERE board_id = (SELECT max(id) FROM boards WHERE name = 'Imported from v1')"
```

Then open the board in v2 and compare a few categories with v1.

## 6. Switch v1 off

Once v2 has everything, in the v1 directory:

```sh
docker compose -f docker/v1.0.0/with-db/docker-compose.yml down
```

`down` keeps v1's database volume. `down -v` deletes it: do that only when you no longer need v1
and have the dump from step 1. Without Docker, stop `python3 run.py` and whatever starts it, and
remove the firewall rule `setup.py` added for v1's port. Move bookmarks and home-screen icons to
v2's address.

## What is different in v2

- **No `/v1/*` JSON API**, and no API docs at `/apidocs`. The browser UI is the only interface.
  Its routes answer with HTML for the page, need a signed-in session and a CSRF token, and are not
  meant for scripts.
- **Configuration through environment variables** (`EASYTODO_*` in `.env`, all listed in the
  [README](../README.md#configuration)) instead of `config.ini` and `app/static/js/socketio.json`:

  | v1 | v2 |
  | --- | --- |
  | `[database]` host, port, dbname, user, password (`DB_*`) | `EASYTODO_DATABASE_URL`; with docker compose only `POSTGRES_PASSWORD` |
  | `[schema]` name (`DB_SCHEMA`) | none: the migrations own the database's tables |
  | `[host]` name, port (`APP_HOST_NAME`, `APP_HOST_PORT`) | `EASYTODO_PORT`, `EASYTODO_BIND`, `EASYTODO_PUBLIC_URL` |
  | `socketio.json` (`SOCKETIO_HOST`, `SOCKETIO_PORT`) | none: the WebSocket uses the page's own address |

- **docker compose does what `setup.py` and `setup_docker.py` did.** The `db` service creates the
  database and its user. The app applies the database migrations every time it starts, in place
  of the DDL scripts. The image holds Python and the dependencies, and `.env` replaces the
  generated config files. There is no interactive menu and nothing touches the firewall: open
  the port yourself if your firewall blocks it. The first admin account comes from
  `create-admin`.
- **Accounts.** Everyone signs in. Each person has their own boards and shares them with others,
  and an admin panel manages the accounts.
