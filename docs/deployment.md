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

- The app accepts form submissions only from the origin in `EASYTODO_PUBLIC_URL`. If every save fails with "Invalid CSRF token.", the address in the browser (IP, host name or port) is different from it.
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
- [ ] **`EASYTODO_SECRET_KEY`**: a strong random key, e.g. `openssl rand -hex 32` (at least 32 characters). It encrypts the 2FA secrets stored in the database. Keep it unchanged, and keep a copy with your backups: without it, the 2FA secrets in a restored database cannot be read.
- [ ] **`EASYTODO_REGISTRATION_OPEN=false`** once everyone has an account, then apply it with `docker compose up -d`. While it is `true`, anyone who can reach the app can sign up.
- [ ] **Mail over SMTP, not the console.** Set `EASYTODO_MAIL_BACKEND=smtp` and the `EASYTODO_SMTP_*` settings. The default `console` backend sends nothing: it writes every mail, password-reset and invite links included, to the app's log as a WARNING. Never use it in production.
- [ ] **Logs contain live links. Restrict who can read them.**
  - `/invite/{token}` links let anyone who has one join the board until the link expires (after 7 days) or is revoked.
  - `/password/reset/{token}` links work once within 60 minutes, and until then they are a full account takeover.
  - Both appear in the app's access log (`docker compose logs app`), in a proxy's access log if you turn one on (Caddy logs no requests by default), and, with the console mail backend, in the mails the app writes to that same log.
  - On Linux, anyone in the `docker` group can read the container logs.
  - Revoke a leaked invite link on the board's Members page. A password change invalidates all of the account's reset links.
- [ ] **Backups** with `pg_dump`, run from the repository root. Copy the dumps off the server, e.g. from a daily cron job:

  ```sh
  docker compose exec -T db pg_dump -U easytodo -d easytodo -Fc > easytodo-$(date +%F).dump
  ```

  To restore a dump:

  ```sh
  docker compose stop app
  docker compose exec -T db pg_restore -U easytodo -d easytodo --clean --if-exists < easytodo-2026-10-21.dump
  docker compose start app
  ```

  Never run `docker compose down -v`: `-v` deletes the database volume, and Caddy's certificates with it.
- [ ] **Updates:** take a backup first, then run:

  ```sh
  git pull && docker compose up -d --build
  ```

  With the proxy, run `docker compose --profile proxy up -d --build` instead, or set `COMPOSE_PROFILES=proxy` in `.env`. Database migrations run when the app starts.
