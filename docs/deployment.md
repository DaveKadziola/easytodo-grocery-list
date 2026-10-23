# Deployment

EasyToDo runs as one docker compose stack from the repository root:

- `db`: PostgreSQL 17. Its port is not published.
- `app`: the image built from `deploy/Dockerfile`. It applies the database migrations every time it starts.
- `caddy` (optional, the `proxy` profile): an HTTPS reverse proxy in front of the app.

Everything is configured in `.env`:

```sh
cp .env.example .env   # then edit it; see the checklist (d) before the first start
```

Choose one of the ways (a)–(c) to reach the app, then go through the checklist (d) before people start using it.

## (a) Home network, without TLS

Phones and laptops on the same network reach the app over plain http.

1. In `.env`, set `EASYTODO_PUBLIC_URL` to the address people will type: the server's LAN IP and the port, e.g. `EASYTODO_PUBLIC_URL=http://192.168.1.20:8000`. If you change `EASYTODO_PORT`, use the same port here.
2. Start the stack: `docker compose up -d`
3. Open `http://192.168.1.20:8000` on a device on the network.

- The app accepts form submissions and live updates only from the origin in `EASYTODO_PUBLIC_URL` and the ones in `EASYTODO_EXTRA_ORIGINS`. If every save fails with "This page was opened at a different address.", the address in the browser (IP, host name or port) is none of them. To use the app at more than one address (`http://localhost:8000` on the server itself next to the LAN address, say, or a port forward next to the machine's own address), list the others comma-separated, e.g. `EASYTODO_EXTRA_ORIGINS=http://localhost:8000, http://192.168.122.50:8000`. Links in mails and invites always use `EASYTODO_PUBLIC_URL`. With an `https://` `EASYTODO_PUBLIC_URL`, as in (b) and (c), the extra addresses must be `https://` too, or the app refuses to start: the session cookie is then `Secure`, so the browser never sends it to an http address and signing in there would loop.
- Everything, passwords included, crosses the network unencrypted, and the cookies are not `Secure`. Use this only on a network you trust. Never forward port 8000 on the router: to reach the app from outside, use (b) or (c).
- Keep `EASYTODO_FORWARDED_ALLOW_IPS=127.0.0.1` and leave `EASYTODO_BIND` unset. The app then sees each device's own address, which is what the per-IP rate limits count.

## (b) Internet, with your own domain (Caddy, automatic HTTPS)

Caddy gets a certificate from Let's Encrypt and renews it. It redirects http to https and forwards every request, WebSockets included, to `app:8000` over the compose network.

You need:

- A domain name, e.g. `todo.example.com`, whose DNS A record points at your public IP address (and an AAAA record if you use IPv6). If your provider changes that address, use a dynamic DNS service.
- On the router, port forwarding of TCP 80 and 443 (and UDP 443 for HTTP/3) to the server. Caddy redirects port 80 to https, and the certificate authority may use it to check the domain when it issues or renews the certificate.

In `.env`:

```sh
EASYTODO_PUBLIC_URL=https://todo.example.com
EASYTODO_DOMAIN=todo.example.com
ACME_EMAIL=you@example.com
EASYTODO_BIND=127.0.0.1
EASYTODO_FORWARDED_ALLOW_IPS=*
```

Start the stack with the proxy:

```sh
docker compose --profile proxy up -d
```

The first request can take a few seconds while Caddy gets the certificate. `docker compose logs caddy` shows its progress. Both `EASYTODO_DOMAIN` and `ACME_EMAIL` must be set: if either is empty, Caddy cannot load `deploy/Caddyfile` and its container keeps restarting. To have every `docker compose` command include Caddy without `--profile proxy` (restarts, the update in (d)), add `COMPOSE_PROFILES=proxy` to `.env`.

**`EASYTODO_FORWARDED_ALLOW_IPS=*` and `EASYTODO_BIND=127.0.0.1` go together:**

- The per-IP rate limits (sign-in, second sign-in step, sign-up, forgotten password, password change) need each visitor's own address. Behind Caddy, the connection to the app always comes from Caddy's container. With `*`, the app takes the visitor's address from the `X-Forwarded-For` header that Caddy sets; Caddy drops any such header a visitor sends. Without `*`, all visitors share one budget, so one person's typos, or an attacker, block sign-in for everyone.
- `*` trusts that header from anyone who can connect to the app's port. `EASYTODO_BIND=127.0.0.1` publishes that port only on the server itself, so only Caddy (over the compose network) and programs on the server can connect. If you set `*` while the port is open to the network, anyone on the network can send a made-up `X-Forwarded-For` with each request and never hit a limit. Never set `*` without `EASYTODO_BIND=127.0.0.1`.
- On Linux, Docker Engine before 28.0 can still let other machines on your local network reach a port published on 127.0.0.1. Check with `docker version` (Server: Engine). If it is older than 28.0, upgrade Docker or block port 8000 from the network in the server's firewall. Otherwise, with `*`, a device on your network can bypass the rate limits.

The `caddy_data` volume holds the certificates and the ACME account. Keep it: Let's Encrypt limits how often the same certificate can be issued again.

**To try it on one machine**, set `EASYTODO_DOMAIN=localhost` and `EASYTODO_PUBLIC_URL=https://localhost`, with the other lines as above (`ACME_EMAIL` must still be set, to any address). Caddy then issues the certificate from its own local authority instead of Let's Encrypt. `curl -kfsS https://localhost/healthz` should print `{"status":"ok","db":"ok"}`. In a browser, open `https://localhost` and accept the certificate warning. The app sends HSTS, so afterwards the browser may switch `http://localhost` to https for any port. Use a private window, or delete the HSTS entry for localhost afterwards.

## (c) Without opening ports: Cloudflare Tunnel or Tailscale Funnel

A tunnel connector on your side opens an outgoing connection to the provider. The provider serves the app on an https address and passes the requests to it through the connector. The router stays closed, you need no certificate of your own, and the `proxy` profile is not used.

1. Point the tunnel at the app: at `http://app:8000` when the connector runs as a container on the compose network, or at `http://127.0.0.1:8000` when it runs on the server itself.
   - **Cloudflare Tunnel:** in the Cloudflare Zero Trust dashboard (Networks → Tunnels), create a tunnel and add a public hostname (e.g. `todo.example.com`) with the service `http://app:8000`. Then run the connector on the compose network, for example from a `docker-compose.override.yml` next to `docker-compose.yml` (docker compose reads it automatically), with `CLOUDFLARE_TUNNEL_TOKEN=<the tunnel's token>` in `.env`:

     ```yaml
     services:
       cloudflared:
         image: cloudflare/cloudflared:latest
         command: tunnel --no-autoupdate run
         environment:
           TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN}
         restart: unless-stopped
     ```

   - **Tailscale Funnel:** with Tailscale installed on the server, `tailscale funnel --bg 8000` serves `https://<machine>.<tailnet>.ts.net` from the app's port. If Funnel is not enabled for your tailnet yet, the command tells you how to enable it.
2. In `.env`, set `EASYTODO_PUBLIC_URL=https://<the tunnel's host name>` and `EASYTODO_BIND=127.0.0.1`, so the tunnel is the only way in.
3. Start the stack: `docker compose up -d`

**Rate limits behind a tunnel.** The app sees the tunnel's peer address, not the visitor's, as the client of every request. That is the cloudflared container's address, or the compose network's gateway for a connector on the server. So all visitors share one per-IP budget: one person's typos, or an attacker, can block sign-in, sign-up and password resets for everyone. The visitor's address reaches the app only if the app trusts the connector's forwarding headers. To do that, set `EASYTODO_FORWARDED_ALLOW_IPS` to the connector's address as the app sees it: an IP address, or a range in CIDR form such as the compose network's subnet (`docker network inspect <project>_default`). Do not use `*` here. Cloudflare, for one, keeps an `X-Forwarded-For` header the visitor sent and appends the real address. With `*` the app takes the first entry, which the visitor controls. With named addresses it takes the last entry that is not one of them, which is the one the tunnel added.

## (d) Checklist

- [ ] **`POSTGRES_PASSWORD`**: set a random value before the first `docker compose up`, e.g. `openssl rand -hex 24`. Postgres stores it in the `pgdata` volume on its first start, so changing `.env` later does not change it.
- [ ] **`EASYTODO_SECRET_KEY`**: a strong random key, e.g. `openssl rand -hex 32` (at least 32 characters). It encrypts the 2FA secrets stored in the database. Keep it unchanged, and keep a copy in a safe place apart from the backups (a password manager, say): without it, the 2FA secrets in a restored database cannot be read, and with it anyone holding a dump can read them.
- [ ] **`EASYTODO_REGISTRATION_OPEN=false`** once everyone has an account, then apply it with `docker compose up -d`. While it is `true`, anyone who can reach the app can sign up. With `false`, people can still sign up from a valid invite link: an e-mail invite works only for its own address, but a link invite lets anyone who has the link create an account until it expires (after 7 days) or is revoked. Revoke the links you no longer need on the board's "Invite and members" page (⋮ on the board list → Invite).
- [ ] **Mail over SMTP, not the console.** Set `EASYTODO_MAIL_BACKEND=smtp` and the `EASYTODO_SMTP_*` settings (the README's SMTP section has a Proton Mail example). The default `console` backend sends nothing: it writes every mail, password-reset and invite links included, to the app's log as a WARNING. Never use it in production.
- [ ] **Logs contain live links. Restrict who can read them.**
  - `/invite/{token}` links let anyone who has one join the board (and create an account, even with registration closed) until the link expires (after 7 days) or is revoked.
  - `/password/reset/{token}` links work once within 60 minutes, and until then they are a full account takeover.
  - Both appear in the app's access log (`docker compose logs app`), in a proxy's access log if you turn one on (Caddy logs no requests by default), and, with the console mail backend, in the mails the app writes to that same log.
  - On Linux, anyone in the `docker` group can read the container logs.
  - Revoke a leaked invite link on the board's "Invite and members" page (⋮ on the board list → Invite). A password change invalidates all of the account's reset links.
- [ ] **Backups** with `pg_dump` and a `tar` of the attachments volume, run from the repository root, into `backups/` (git-ignored). A dump holds every account's password hash, so it is created readable by your user only (`umask 077`). Copy both off the server, e.g. from a daily cron job (the one that runs at 03:15 must include the volume `easytodo_attachments`, i.e. the second line, next to the dump: a database restored without its files shows "This attachment no longer exists." for every file):

  ```sh
  mkdir -p backups
  (umask 077 && docker compose exec -T db pg_dump -U easytodo -d easytodo -Fc > backups/easytodo-$(date +%F).dump)
  (umask 077 && docker compose exec -T app tar -C /app/backend/data/attachments -cf - . > backups/attachments-$(date +%F).tar)
  ```

  To restore a dump and the files of the same day: stop the app, restore the database, restore the files with the app still stopped, start the app:

  ```sh
  docker compose stop app
  docker compose exec -T db pg_restore -U easytodo -d easytodo --clean --if-exists < backups/easytodo-2026-10-21.dump
  docker compose run --rm --no-deps -T --entrypoint tar app -C /app/backend/data/attachments -xf - < backups/attachments-2026-10-21.tar
  docker compose start app
  ```

  The app's periodic purge (every 24 hours by default; off with `EASYTODO_PURGE_INTERVAL_HOURS=0`, then run `docker compose exec -T app python -m easytodo.cli purge` yourself) removes the files that have no row in the database (attached after the dump); `tar` keeps the files' old modification times, so a restore does not make files look new to it.

  Never run `docker compose down -v`: `-v` deletes the database and attachments volumes, and Caddy's certificates with it.
- [ ] **Clean-up runs inside the app.** There is nothing to schedule: 5 minutes after it starts, and then every `EASYTODO_PURGE_INTERVAL_HOURS` hours (24 by default), the app deletes the archived boards and categories past their owner's retention, tasks deleted more than 10 minutes ago, notifications older than 30 days and attached files that no task has any more, and logs one line (`Purged: ...`) when it deleted anything. With `EASYTODO_PURGE_INTERVAL_HOURS=0` it is off, and a cron job has to run `docker compose exec -T app python -m easytodo.cli purge` instead (a second run while one is going does nothing and exits with an error). The files are cleaned only if the database holds at least one attachment (see the restore note above).
- [ ] **Updates:** take a backup first, then run:

  ```sh
  git pull && docker compose up -d --build
  ```

  With the proxy, run `docker compose --profile proxy up -d --build` instead, or set `COMPOSE_PROFILES=proxy` in `.env`. Database migrations run when the app starts.
