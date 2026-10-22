# EasyToDo Grocery List

Self-hosted grocery and to-do lists for a household. Everyone has their own account and boards,
boards can be shared, and a change made on one phone shows up on every other open screen in a
moment, without reloading. It runs on your own server or home computer with docker compose, and
works on a home network without internet access.

This is v2, a rewrite of the original single-list Flask app as a multi-user FastAPI + HTMX app.
Coming from v1? See [docs/migration-from-v1.md](docs/migration-from-v1.md).

<p align="center">
  <img src="docs/images/board-phone.png" alt="A grocery board on a phone" width="260">
  &nbsp;
  <img src="docs/images/board-desktop.png" alt="The same board on a desktop browser" width="560">
</p>

## Features

- **Boards, categories and items.** As many boards as you like. Items have an optional
  description and are ticked off with one tap. Drag items between categories and reorder
  categories, with a mouse, by touch or from the keyboard (Move up / Move down in a category's
  menu). Fold a category away on one device. "Clear done" empties a category of ticked items.
- **Real-time sync.** Every open board stays current over a WebSocket. A phone that was asleep or
  lost its signal catches up as soon as it is back. The header shows "Live" (on a phone, a green
  dot) while connected.
- **Sharing.** Invite people by link or by email, as an editor (can change items) or a viewer
  (can only look). Owners manage members; every member other than the owner can leave a board.
- **Accounts.** Sign-up (can be closed), sign-in with a lockout after 10 wrong passwords,
  optional two-factor authentication (an authenticator app, plus 10 recovery codes), password
  reset by email, and a list of where you are signed in, with "Sign out other devices".
- **Admin panel.** Statistics, and a user list with search: create an account with a temporary
  password, send a password reset, reset 2FA, disable or delete an account.
- **Made for phones.** Responsive layout, dark mode that follows the system, and it can be added
  to the home screen. All scripts and styles are served by the app itself, without CDNs.
- **Self-hosted.** One docker compose stack (the app and PostgreSQL), with an optional Caddy
  reverse proxy for automatic HTTPS.

## Quick start

You need Docker with Compose (Docker 25 or newer), `git` and `openssl`, in a Linux or macOS shell
(on Windows, use WSL).

```sh
git clone https://github.com/DaveKadziola/easytodo-grocery-list.git
cd easytodo-grocery-list
cp .env.example .env
sed -i.bak -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" \
    -e "s/^EASYTODO_SECRET_KEY=.*/EASYTODO_SECRET_KEY=$(openssl rand -hex 32)/" .env && rm .env.bak
docker compose up -d --wait
docker compose exec app python -m easytodo.cli create-admin --email you@example.com
```

- The `sed` line puts a random database password and a random `EASYTODO_SECRET_KEY` into `.env`.
  Run it only once, before the first start: the database keeps the password it was created
  with.
- The first `docker compose up` builds the image, which takes a few minutes. `--wait` returns once
  the app is up and has created its database tables.
- `create-admin` asks for a password twice (10 to 256 characters).

Open <http://localhost:8000> and sign in with that email and password. To use the app from your
phone, see "Phone on the home network" in the checklist below.

`docker compose down` stops the app and keeps the data. `docker compose up -d` starts it again.

On a server with a public IP address (a VPS, say), set `EASYTODO_BIND=127.0.0.1` in `.env`
before the first start and follow [docs/deployment.md](docs/deployment.md) (b) or (c): otherwise
the app is reachable from the internet over plain HTTP, with registration open, and ports that
Docker publishes bypass firewalls such as ufw.

## Manual test checklist

A walk through everything by hand on a fresh install. The steps with two people need two
browsers, or a normal and a private window, each signed in as someone else.

1. **Registration.** Sign out, choose "Create an account" and create a second account, e.g.
   `bob@example.com`. It starts with a board named "My list". With
   `EASYTODO_REGISTRATION_OPEN=false` in `.env` (then `docker compose up -d`), `/register` is
   closed (404), except from a valid invite link (step 4): its sign-in page still offers
   "Create an account". An e-mail invite works only for its own address. A link invite lets
   anyone who has the link create an account until it expires (7 days) or is revoked, so revoke
   links you no longer need.
2. **Two-factor authentication.** Account → "Set up two-factor authentication". Scan the QR code
   with an authenticator app (or type the key shown under it), enter the 6-digit code, and keep
   the recovery codes. Sign out and in again: after the password the app asks for a code. A
   recovery code works instead, once.
3. **Password reset.** Sign out → "Forgot password?" → enter the email. With the default
   `EASYTODO_MAIL_BACKEND=console` nothing is sent: the mail, link included, is written to the
   app's log. Show the link with:

   ```sh
   docker compose logs app | grep /password/reset/
   ```

   Open it (it works once, for 60 minutes) and choose a new password. That signs the account out
   everywhere.
4. **Sharing.** As the owner, open the board list, choose ⋮ on the board → Invite. On the
   "Invite and members" page, under "Invite someone", pick the role "Viewer", then either leave
   the email empty to get a link (it is shown once: copy it) or enter `bob@example.com` (the
   console backend sends no mail: the page then shows the invite link to pass on, which only
   Bob's account can use). Open the link in Bob's browser and choose "Join board". As a viewer,
   Bob sees the board without edit controls, and it is listed under "Shared with me". Change his
   role to editor on the "Invite and members" page and he can edit. As Bob, choose ⋮ on the
   board in his list → "Leave board"; invite him again, and then remove him from the "Invite and
   members" page.
5. **Real time with two browsers.** Open the same board in both browsers; each header shows
   "Live". Add, tick, rename and drag items in one: the other follows without reloading. Then
   take one of them offline (airplane mode on a phone, or developer tools → Network → Offline),
   change something in the other, and go back online: the first one catches up by itself, without
   a reload (meanwhile its header may show "Reconnecting…"). On an iPhone or iPad, check the
   same in Safari: the board shows "Live" (the green dot), and a change made on another device
   appears without reloading.
6. **Admin panel.** As the admin: "Admin" in the header shows the statistics, "Manage users" the
   accounts. Create an account (its temporary password is shown once, and it must choose a new one
   at its first sign-in), send a password reset (link in the log, as in step 3), reset 2FA,
   disable an account (it is signed out at once) and delete one.
7. **Phone on the home network.** Find the computer's LAN address (Linux: `hostname -I`, macOS:
   `ipconfig getifaddr en0`), say `192.168.1.20`. In `.env` set
   `EASYTODO_PUBLIC_URL=http://192.168.1.20:8000`, then run `docker compose up -d` to apply it.
   Open `http://192.168.1.20:8000` on the phone, on the same Wi-Fi. The app accepts form
   submissions only from the address in `EASYTODO_PUBLIC_URL` and those in
   `EASYTODO_EXTRA_ORIGINS`, so at `http://localhost:8000` signing in and saving now fail with
   "This page was opened at a different address.", and a board page there shows "Offline" with
   a message naming the address to use. To keep `http://localhost:8000` working as well, also
   set `EASYTODO_EXTRA_ORIGINS=http://localhost:8000` (more addresses: comma-separated). If the
   phone cannot connect, allow incoming TCP port 8000 in the computer's firewall. The
   browser's "Add to Home Screen" installs it like an app.

## Configuration

Everything is set in `.env`, which docker compose reads; [.env.example](.env.example) explains
each line. After a change, `docker compose up -d` applies it.

| Variable | Default | What it does |
| --- | --- | --- |
| `EASYTODO_DATABASE_URL` | required | PostgreSQL URL, `postgresql+asyncpg://user:password@host:port/db`. `.env.example` points it at the compose `db` service, with `POSTGRES_PASSWORD` filled in. |
| `EASYTODO_SECRET_KEY` | required | At least 32 characters (`openssl rand -hex 32`). Encrypts the 2FA secrets in the database: keep it unchanged, and keep a copy in a safe place apart from your backups. |
| `EASYTODO_PUBLIC_URL` | `http://localhost:8000` | The address people type in the browser, and the one links in mails and invites use. The app accepts form submissions (CSRF check) and live updates only from its origin and from `EASYTODO_EXTRA_ORIGINS`. An `https://` address also makes the session cookie `Secure`. |
| `EASYTODO_EXTRA_ORIGINS` | none | More addresses the app is opened at, comma-separated, e.g. `http://localhost:8000, http://192.168.1.20:8000`: form submissions and live updates work there too. Mail and invite links still use `EASYTODO_PUBLIC_URL`. An `https://` `EASYTODO_PUBLIC_URL` cannot be combined with `http://` extras (the app refuses to start): its session cookie is `Secure`, so the browser never sends it to an http address and signing in there would loop. |
| `EASYTODO_REGISTRATION_OPEN` | `true` | `false` closes `/register`, except from a valid invite link. An e-mail invite works only for its own address; a link invite lets anyone who has the link create an account until it expires (7 days) or is revoked, so revoke links you no longer need. Admins can still create accounts. |
| `EASYTODO_SESSION_TTL_DAYS` | `30` | How long a sign-in stays valid, in days. |
| `EASYTODO_MAIL_BACKEND` | `console` | `console` writes mails to the app's log and sends nothing. `smtp` sends them (see SMTP below). |
| `EASYTODO_SMTP_HOST` | none | SMTP server. |
| `EASYTODO_SMTP_PORT` | `587` | SMTP port. |
| `EASYTODO_SMTP_USER` | none | SMTP login. Leave it out for a server without login. |
| `EASYTODO_SMTP_PASSWORD` | none | SMTP password. |
| `EASYTODO_SMTP_FROM` | `EasyToDo <noreply@localhost>` | Sender of the mails. |
| `EASYTODO_SMTP_STARTTLS` | `true` | Upgrade the connection with STARTTLS. `false` never upgrades. |
| `EASYTODO_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted. Name only your reverse proxy ([docs/deployment.md](docs/deployment.md)). |
| `EASYTODO_ENV` | `dev` (`prod` in `.env.example`) | `dev`, `test` or `prod`. A label only: nothing depends on it yet. |
| `EASYTODO_PORT` | `8000` | Host port docker compose publishes the app on. Use the same port in `EASYTODO_PUBLIC_URL`. |
| `EASYTODO_BIND` | every interface | Host address for that port. `127.0.0.1` with the reverse proxy. |
| `EASYTODO_WORKERS` | `1` | Uvicorn worker processes. The rate limits live in each worker's memory, so N workers allow N times as many attempts (and a restart resets them): keep `1`, which is plenty for a household, on an install reachable from the internet. |
| `EASYTODO_DOMAIN` | none | Domain name for Caddy's certificate (only with the `proxy` profile). |

`.env` also holds two settings for the other containers: `POSTGRES_PASSWORD` (the `db` service's
password, used in `EASYTODO_DATABASE_URL`) and `ACME_EMAIL` (Caddy's contact address for Let's
Encrypt, only with the `proxy` profile).

## HTTPS and deployment

For use over the internet, put the app behind HTTPS: the optional Caddy reverse proxy
(`docker compose --profile proxy up -d`) gets and renews a Let's Encrypt certificate for your
domain. [docs/deployment.md](docs/deployment.md) covers the home network, your own domain with
Caddy, Cloudflare Tunnel or Tailscale Funnel, and a checklist for production (passwords, closing
registration, mail, logs, backups, updates).

## SMTP

Password resets and email invitations need mail. With the default `console` backend, mails only
go to the app's log. To send them, set in `.env`:

```sh
EASYTODO_MAIL_BACKEND=smtp
EASYTODO_SMTP_HOST=smtp.example.com
EASYTODO_SMTP_PORT=587
EASYTODO_SMTP_USER=you@example.com
EASYTODO_SMTP_PASSWORD=your-smtp-password
EASYTODO_SMTP_FROM=EasyToDo <you@example.com>
```

docker compose fills in `${...}` in `.env` values, so wrap a value that contains a `$` in single
quotes (`EASYTODO_SMTP_PASSWORD='pa$$word'`); otherwise it is silently changed and sign-in to the
SMTP server fails.

Then run `docker compose up -d`. The app connects on port 587 and upgrades with STARTTLS. A
server that needs TLS from the start (port 465) is not supported. For a relay without TLS (a
local server on port 25), set `EASYTODO_SMTP_STARTTLS=false`. To test it, request a password
reset for your own account. If no mail arrives, `docker compose logs app` shows "Could not send
mail to ..." with the reason.

### Proton Mail

Proton Mail takes mail from apps with an SMTP token, not with the account password. Create one in
Proton under Settings → IMAP/SMTP → SMTP tokens (this needs a paid plan with a custom domain),
for the address the app sends from. Then, in `.env`:

```sh
EASYTODO_MAIL_BACKEND=smtp
EASYTODO_SMTP_HOST=smtp.protonmail.ch
EASYTODO_SMTP_PORT=587
EASYTODO_SMTP_STARTTLS=true
EASYTODO_SMTP_USER=todo@your-domain.example
EASYTODO_SMTP_PASSWORD=<the SMTP token>
EASYTODO_SMTP_FROM=EasyToDo <todo@your-domain.example>
```

The From address must be the token's address (the one in `EASYTODO_SMTP_USER`). If the token
contains a `$`, wrap it in single quotes, as explained above.

## Backup and restore

The data lives in the `pgdata` volume of the `db` service. Back it up with `pg_dump`, from the
repository root, into `backups/` (git-ignored; the dumps are readable by your user only, as they
hold every account's password hash), and copy the dumps off the server:

```sh
mkdir -p backups
(umask 077 && docker compose exec -T db pg_dump -U easytodo -d easytodo -Fc > backups/easytodo-$(date +%F).dump)
```

Restore a dump:

```sh
docker compose stop app
docker compose exec -T db pg_restore -U easytodo -d easytodo --clean --if-exists < backups/easytodo-2026-10-21.dump
docker compose start app
```

Keep a copy of `EASYTODO_SECRET_KEY` too, in a safe place but not next to the dumps (a password
manager, say): without it, the 2FA secrets in a restored database cannot be read, and with it
anyone holding a dump can read them. Never run `docker compose down -v`: `-v` deletes the
database volume.

## Local development

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if needed), Docker for the
database and `make`. Node.js is not needed. From the repository root:

```sh
make test-db          # PostgreSQL 17 on localhost:54329, data kept in memory
docker compose -f compose.test.yml exec db createdb -U easytodo easytodo_dev
cd backend && uv sync && cd ..
make css              # downloads the Tailwind CLI into bin/, builds frontend/static/css/app.css
printf 'EASYTODO_DATABASE_URL=postgresql+asyncpg://easytodo:easytodo@localhost:54329/easytodo_dev\nEASYTODO_SECRET_KEY=%s\n' \
    "$(openssl rand -hex 32)" > backend/.env
cd backend && uv run alembic upgrade head && cd ..
make run              # http://localhost:8000, restarts on code changes
```

- The app reads `backend/.env` when it runs from `backend/`. Keep `EASYTODO_PUBLIC_URL` out of it:
  the default `http://localhost:8000` is what `make run` serves, and one test checks that default.
- The test database container keeps its data in memory, so after it restarts, run the
  `createdb` and `alembic upgrade head` lines again.
- Run `make css` again after adding Tailwind classes to the templates. The Docker image builds the
  CSS by itself.
- The tests use their own database (`easytodo_test` in the same container) and empty it after
  each test.

```sh
make test             # tests, without the browser tests
cd backend && uv run playwright install chromium && cd ..   # once, before the first make e2e
make e2e              # browser tests (Playwright, Chromium); builds the CSS first
make lint             # ruff
```

`scripts/bench.py` measures response times; [docs/performance.md](docs/performance.md) has the
v1-to-v2 comparison and how to repeat it.

## Database migrations

The schema is managed by Alembic (`backend/migrations/`). **Every model change is a new
migration.** After changing the models in `backend/src/easytodo/models/`:

```sh
cd backend
uv run alembic upgrade head          # bring the dev database up to date first
uv run alembic revision --autogenerate -m "add item quantity"
uv run alembic upgrade head
uv run alembic check                 # "No new upgrade operations detected."
```

Read the generated file in `backend/migrations/versions/` before committing it: autogenerate
does not see everything (renames, CHECK constraints on existing tables, a partial index's
`WHERE`). CI runs `alembic upgrade head` and `alembic check`, and the test suite fails when the
models and the migrations differ, server defaults included.

The container applies the migrations itself: every time it starts, `deploy/entrypoint.sh` runs
`alembic upgrade head` before starting the app (retrying while the database starts up). An update
is therefore `git pull && docker compose up -d --build`, after a backup.

## Architecture

The backend is FastAPI (async), which serves the pages, the HTML fragments and the WebSockets in
one process, with SQLAlchemy and asyncpg on PostgreSQL. The browser side is server-rendered HTML
with HTMX, the CSP build of Alpine.js and Tailwind: each action sends a form and swaps in the HTML
that comes back, so there is no client-side state to keep in sync, and no Node.js anywhere. Real
time runs over PostgreSQL `LISTEN`/`NOTIFY`: every change sends a `pg_notify` inside its
transaction, so an event leaves only after the commit, and it reaches every Uvicorn worker
without Redis. Events are thin (a type and ids): the page fetches the HTML it needs through the
normal, authorised routes, and after every (re)connect it reloads the whole board, so no event
log is needed. Sign-in uses server-side sessions in an httpOnly cookie with a CSRF token instead
of JWTs, so signing out, a password reset or an admin ends a session at once, its WebSockets
included.

- `backend/`: the Python package (`src/easytodo/`), migrations and tests.
- `frontend/`: Jinja templates, static files (vendored JS libraries) and the Tailwind source.
- `deploy/`: Dockerfile, entrypoint and Caddyfile.
- `docs/`: deployment, migration from v1, performance.

## License

[MIT](LICENSE)
