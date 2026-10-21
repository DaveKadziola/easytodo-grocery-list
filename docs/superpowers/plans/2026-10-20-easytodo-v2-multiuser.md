# EasyToDo v2 (wieloużytkownikowa przebudowa) — plan implementacji

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zastąpić jednoużytkownikową aplikację Flask wieloużytkownikową aplikacją FastAPI z kontami, 2FA, boardami współdzielonymi z rolami, synchronizacją real-time, panelem admina i wdrożeniem przez docker-compose (z opcjonalnym HTTPS przez Caddy).

**Architecture:** Jeden proces FastAPI renderuje HTML po stronie serwera (Jinja2), a interakcje robi HTMX (podmiana fragmentów) + Alpine.js (stan lokalny: modale, menu). Każda zmiana danych w boardzie wysyła w tej samej transakcji `pg_notify('board_events', …)`; każdy worker trzyma jedno połączenie `LISTEN` (asyncpg) i rozsyła zdarzenia do WebSocketów subskrybujących dany board, a przeglądarka po zdarzeniu dociąga świeży fragment HTML. Sesje są serwerowe (tabela `user_sessions`, cookie httpOnly), więc każdą da się unieważnić.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn (`uvicorn[standard]`), SQLAlchemy 2.0 async + asyncpg, Alembic, pydantic-settings, Jinja2, argon2-cffi, pyotp, segno (QR w SVG), cryptography (Fernet), aiosmtplib, HTMX 2.x, Alpine.js 3.x (build CSP), SortableJS 1.15.x, Tailwind CSS 4 (standalone CLI, bez Node), PostgreSQL 17, Caddy 2, pytest + pytest-asyncio + httpx + pytest-playwright, uv.

**Spec:** `docs/superpowers/specs/2026-10-20-multiuser-refactor-spec.md`

---

## Analiza obecnego kodu (punkt wyjścia)

Stan na `main` @ `0fcbd9c` (~6 tys. linii, w tym ~1,2 tys. frontendu):

- **Backend:** Flask + Flask-SocketIO (eventlet), Flask-SQLAlchemy, Flasgger. Endpointy JSON `/v1/*` (kategorie: add/rename/delete/move; zadania: add/update/delete/move/toggle) wymagające nagłówka `Accept: application/vnd.myapi.v1+json`. Konfiguracja z `config.ini` czytanego przy imporcie modułu.
- **Real-time:** triggery PL/pgSQL piszą do `updates_log` i robią `pg_notify('data_changes')`; `run.py` w pętli `select()` odbiera NOTIFY i robi `socketio.emit` **do wszystkich klientów**. Usunięcia/przesunięcia kategorii idą przez sztuczkę z tabelą `temporary_data`. Replay po reconnect czyta `prod.updates_log` (schemat zahardkodowany niezależnie od `CURRENT_SCHEMA`), a `updates_log` rośnie bez końca.
- **Model:** `categories` (nazwa unikalna globalnie, `position`), `tasks`, `task_assignment` (de facto relacja 1:1 task→kategoria). Brak użytkowników.
- **Frontend:** Jinja + Bootstrap 5 z CDN + vanilla JS, drag-and-drop HTML5 (tylko mysz, brak dotyku), modal zadania.
- **Znalezione problemy, które przebudowa musi usunąć:**
  - XSS: `_task.html` wstawia `{{ task.name }}` do JS w atrybucie `onclick="openTaskModal(…, '{{ task.name }}', …)"` — autoescape HTML nie chroni kontekstu JS (encje są dekodowane przed wykonaniem). Nazwa `O'Reilly` psuje UI, a spreparowana nazwa wykonuje kod.
  - `default=datetime.now()` w modelach — wartość liczona raz przy imporcie, wszystkie rekordy dostają ten sam timestamp.
  - N+1 zapytań w `/` (osobne zapytanie o przypisania i zadania dla każdej kategorii).
  - `cors_allowed_origins="*"`, `debug=True`, brak CSRF, brak auth, nowe połączenie psycopg2 na każde `request_updates`.
  - `.gitignore` ignoruje każdy katalog `easytodo/` (nazwa venv) — trafiłby też w nowy pakiet.

**Co zachowujemy:** koncept NOTIFY z Postgresa jako szyny zdarzeń (bez Redisa), układ UX (kategorie jako kafelki, modal pozycji, przeciąganie pozycji między kategoriami, kolejność „niezrobione najpierw, potem alfabetycznie”), self-hosting w kontenerach.

## Decyzje architektoniczne

### Backend: FastAPI (async)
Natywne WebSockety w tym samym procesie i pętli co HTTP, async asyncpg do `LISTEN`, Pydantic do walidacji formularzy, brak eventlet/monkey-patchingu. Flask wymagałby dalej Flask-SocketIO + eventlet.

### Frontend: HTMX + Alpine.js + Tailwind (nie React) — uzasadnienie
1. **Charakter danych:** listy i kafelki, brak złożonego stanu po stronie klienta. Każda interakcja to „wyślij formularz → podmień fragment”, co HTMX robi natywnie; real-time sprowadza się do „dociągnij fragment X”.
2. **Wydajność:** HTMX (~16 KB gz) + Alpine CSP (~15 KB gz) + Sortable (~15 KB gz) to ułamek React + ReactDOM + router + klient stanu, a pierwszy widok przychodzi jako gotowy HTML (szybszy first paint na telefonie, brak hydratacji).
3. **Bezpieczeństwo:** zostajemy przy sesji w cookie httpOnly + CSRF, bez tokenów JWT w JS (mniejsza powierzchnia XSS). Ścisłe CSP (`script-src 'self'`) jest realne: HTMX bez inline handlerów i build CSP Alpine.
4. **Utrzymanie i self-hosting:** brak Node.js w runtime i w dev (Tailwind jako standalone CLI), jeden obraz Dockera, jedna warstwa walidacji i autoryzacji (serwer).
5. **Kiedy wrócić do Reacta:** tryb offline z kolejką zmian, edycja kolaboracyjna z merge na poziomie pól albo natywna aplikacja mobilna współdzieląca API. Wtedy dokładamy JSON API obok obecnych serwisów (warstwa `service.py` jest od routingu niezależna).

Tailwind zamiast obecnego Bootstrapa, bo daje mniejszy CSS (tylko użyte klasy) i łatwiejszy layout mobile-first; zasoby są serwowane lokalnie (bez CDN, działa w LAN bez internetu).

### Real-time: WebSocket + Postgres LISTEN/NOTIFY
- `pg_notify` w transakcji zmiany ⇒ zdarzenie wychodzi **tylko po commicie** (brak „duchów” po rollbacku).
- Działa z wieloma workerami Uvicorna bez Redisa.
- Zdarzenia są „cienkie” (typ + id kategorii), a klient dociąga HTML przez zwykły endpoint z autoryzacją, więc viewer i editor dostają różny HTML bez osobnych kanałów.
- Po (re)connect klient zawsze dociąga cały board, więc nie potrzeba logu zdarzeń (`updates_log` znika).

### Sesje zamiast JWT
Tabela `user_sessions` z hashem tokenu; cookie `easytodo_session` (httpOnly, `SameSite=Lax`, `Secure` przy https). Unieważnienie = usunięcie wiersza (logout, „wyloguj inne urządzenia”, reset hasła, admin), WebSockety tej sesji są zamykane zdarzeniem `sessions.revoked`.

### Struktura repo
`backend/` (pakiet Pythona, migracje, testy), `frontend/` (szablony Jinja, statyki, źródło Tailwinda), `deploy/` (Dockerfile, entrypoint, Caddyfile). Katalog `shared/` pomijamy świadomie: przy renderowaniu po stronie serwera nie ma kodu współdzielonego między backendem i frontendem. Kontrakt zdarzeń WebSocket jest opisany w `backend/src/easytodo/realtime/events.py`.

### Poza zakresem (YAGNI)
Publiczne JSON API/OpenAPI dla klientów zewnętrznych, wielojęzyczność (UI zostaje po angielsku jak obecnie), tryb offline z kolejką zmian, ręczna kolejność pozycji w kategorii, Redis.

## Global Constraints

- Python `>=3.12`; zależności zarządzane przez `uv` (`backend/pyproject.toml` + `backend/uv.lock`), wszystkie komendy uruchamiane z katalogu `backend/`.
- PostgreSQL `>=16` (compose i CI: `postgres:17-alpine`).
- Brak Node.js w runtime i w wymaganiach dev; biblioteki JS vendorowane w `frontend/static/vendor/` z wersją w nazwie pliku; żadnych CDN w szablonach.
- Konfiguracja wyłącznie przez zmienne `EASYTODO_*` (koniec z `config.ini` i `socketio.json`).
- Każda zmiana modelu = nowa migracja Alembic; CI uruchamia `alembic upgrade head` i `alembic check`.
- Hasła: argon2id (`argon2-cffi`, parametry domyślne `PasswordHasher()`); minimalna długość hasła 10, maksymalna 256 znaków.
- Każde żądanie POST/PUT/PATCH/DELETE wymaga poprawnego tokenu CSRF **i** nagłówka `Origin` (lub `Referer`) zgodnego z `EASYTODO_PUBLIC_URL`; inaczej 403.
- Dostęp do zasobów boardu przez nie-członka ⇒ 404 (nie zdradzamy istnienia); członek bez uprawnienia ⇒ 403.
- Role w boardzie dokładnie: `owner`, `editor`, `viewer`. Rola systemowa: flaga `users.is_admin`.
- Wszystkie timestampy `timestamptz` z `server_default=now()`.
- Ścisłe CSP (wartość w Task 4); w szablonach zakaz atrybutów `on*=` i inline `<script>`.
- UI po angielsku (jak obecna wersja); komunikaty błędów — dokładne teksty podane w taskach.
- Wydajność: p95 dla „widok boardu”, „dodanie pozycji” i „toggle pozycji” ≤ p95 legacy zmierzonego w Task 2 (ten sam seed: 20 kategorii × 30 pozycji, 200 żądań na scenariusz).
- Fixture'y użyte w testach, a nienazwane w żadnym bloku Interfaces (np. `admin_client`, `user_with_2fa`, `two_logged_in_pages`), tworzy task, który ich pierwszy używa, w `tests/<moduł>/conftest.py`.
- Każdy task kończy się zielonym `uv run pytest` (i `uv run pytest -m e2e`, jeśli task dotyka UI) oraz commitem.

## Review Focus

1. **Nazwy z cudzysłowami i HTML** (`O'Reilly <b>"x"</b>`, `<script>alert(1)</script>`) w kategorii/pozycji/boardzie — mają się wyświetlać dosłownie, w każdym widoku, fragmencie i modalu, bez wykonania kodu (obecna wersja się na tym psuje). Test: Task 8 `test_names_are_rendered_escaped`, Task 9 `test_templates_have_no_inline_handlers`.
2. **Działanie na pozycji, którą inne urządzenie właśnie usunęło** (dwie osoby w sklepie: jedna usuwa, druga odhacza) — oczekiwane 404 z komunikatem „This item no longer exists.” i automatycznym odświeżeniem boardu, nigdy 500 ani „wiszący” element. Test: Task 8 `test_action_on_deleted_item_returns_404_with_resync`.
3. **Telefon wraca z uśpienia / traci zasięg** — zmiany zrobione w tym czasie na innym urządzeniu mają się pojawić po odzyskaniu połączenia bez ręcznego odświeżania. Test: Task 17 `test_offline_device_resyncs_after_reconnect`.
4. **Zgadywanie id z cudzego boardu** — każdy endpoint (w tym przeniesienie pozycji do kategorii w innym boardzie i dociąganie fragmentów) zwraca 404 i niczego nie zmienia. Test: Task 8 `test_other_users_ids_are_404_everywhere`, `test_move_item_to_category_in_other_board_is_404`.
5. **E-mail z wielkimi literami/spacjami** (`" Dave@Example.COM "`) przy rejestracji, logowaniu, resecie i zaproszeniu e-mailowym — to jedno konto. Test: Task 5 `test_email_is_normalized_everywhere`, Task 10 `test_email_invite_matches_case_insensitively`.

---

## Struktura plików (docelowa)

```
backend/
  pyproject.toml, uv.lock, alembic.ini
  migrations/env.py, migrations/versions/0001_initial.py ...
  src/easytodo/
    main.py            create_app(), lifespan (engine, hub, mailer)
    config.py          Settings, get_settings()
    db.py              Base, create_engine(), get_db()
    errors.py          NotFound/Forbidden/Conflict/ValidationFailed/NotAuthenticated + handlery
    web.py             templates (Jinja2Templates), render(), is_htmx()
    middleware.py      SecurityHeadersMiddleware, CsrfCookieMiddleware, ClientIdMiddleware
    models/            user.py, auth.py, board.py, content.py, __init__.py
    security/          passwords.py, tokens.py, crypto.py, csrf.py, ratelimit.py, totp.py
    auth/              sessions.py, service.py, deps.py, routes.py, account_routes.py, twofa.py, reset.py
    boards/            permissions.py, service.py, routes.py, invites.py, invite_routes.py
    content/           service.py, routes.py, validation.py
    realtime/          events.py, hub.py, listener.py, routes.py
    admin/             service.py, routes.py
    mail/              mailer.py, templates/*.txt
    legacy_import.py   import danych z v1
    cli.py             python -m easytodo.cli create-admin | import-legacy
  tests/  conftest.py, factories.py, helpers.py, live_server.py, <moduł>/test_*.py, e2e/test_*.py
frontend/
  templates/  base.html, auth/*, account/*, boards/*, admin/*, errors/*, partials/*
  static/     js/app.js, js/realtime.js, js/board.js, css/app.css (generowany), vendor/*, images/favicon.webp
  styles/input.css
deploy/  Dockerfile, entrypoint.sh, Caddyfile
docker-compose.yml, compose.test.yml, .env.example, Makefile
scripts/bench.py
docs/performance.md, docs/migration-from-v1.md, docs/deployment.md
legacy/v1/   (stary kod do Task 19)
```

---

## Faza 0 — Fundamenty

### Task 1: Szkielet projektu, konfiguracja, baza testowa, CI

**Files:**
- Move: `app/ run.py setup.py setup_docker.py requirements.txt ddl/ docs/*.yml docker/ easytodo-grocery-list.code-workspace` → `legacy/v1/` (`docs/superpowers/` zostaje)
- Modify: `.gitignore` (zamień `easytodo/` na `/easytodo/`; dodaj `.venv/`, `frontend/static/css/app.css`, `.env`, `bin/`)
- Create: `backend/pyproject.toml`, `backend/alembic.ini`, `backend/migrations/env.py` (szablon `alembic init -t async`, URL z `Settings`), `backend/src/easytodo/{__init__,main,config,db}.py`, `compose.test.yml`, `Makefile`, `.github/workflows/ci.yml`
- Test: `backend/tests/conftest.py`, `backend/tests/test_health.py`, `backend/tests/test_config.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` (`env_prefix="EASYTODO_"`, `env_file=".env"`): `database_url: str` (format `postgresql+asyncpg://…`), `secret_key: SecretStr` (min 32 znaki), `public_url: AnyHttpUrl = "http://localhost:8000"`, `env: Literal["dev","test","prod"] = "dev"`, `registration_open: bool = True`, `session_ttl_days: int = 30`, `mail_backend: Literal["console","smtp"] = "console"`, `smtp_host: str | None = None`, `smtp_port: int = 587`, `smtp_user: str | None = None`, `smtp_password: SecretStr | None = None`, `smtp_from: str = "EasyToDo <noreply@localhost>"`, `smtp_starttls: bool = True`, `forwarded_allow_ips: str = "127.0.0.1"`; property `cookie_secure -> bool` (`public_url.scheme == "https"`), property `public_origin -> str` (`scheme://host[:port]`).
  - `get_settings() -> Settings` (`lru_cache`).
  - `db.py`: `class Base(DeclarativeBase)` z `MetaData(naming_convention={"ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"})`; `create_engine(settings) -> AsyncEngine` (`pool_size=10`, `pool_pre_ping=True`); `async def get_db(request: Request) -> AsyncIterator[AsyncSession]` (sessionmaker z `app.state.sessionmaker`, `expire_on_commit=False`).
  - `main.py`: `create_app(settings: Settings | None = None) -> FastAPI`; uruchamianie `uvicorn --factory easytodo.main:create_app`; `GET /healthz` → `{"status": "ok", "db": "ok"}` (200) lub `{"status": "degraded", "db": "error"}` (503) po `SELECT 1`. `GZipMiddleware(minimum_size=1000)`.
  - Fixtures w `conftest.py`: `settings` (test DB, `secret_key="x"*40`, `public_url="http://testserver"`, `env="test"`), `app`, `client` (`httpx.AsyncClient(transport=ASGITransport(app), base_url="http://testserver", headers={"Origin": "http://testserver"})` w `asgi_lifespan.LifespanManager`), `client_factory` (async callable `client_factory(**settings_overrides)`: bez argumentów zwraca nowy klient do tej samej aplikacji z osobnymi ciasteczkami, z argumentami buduje osobną aplikację z nadpisanymi polami `Settings`), `db` (`AsyncSession`). Session-scoped `alembic upgrade head` na `TEST_DATABASE_URL` (domyślnie `postgresql+asyncpg://easytodo:easytodo@localhost:54329/easytodo_test`); po każdym teście `TRUNCATE <wszystkie tabele z Base.metadata> RESTART IDENTITY CASCADE`.

- [ ] **Step 1: Otaguj stan legacy i przenieś stary kod**

```bash
git tag legacy-v1 0fcbd9c
git mv app run.py setup.py setup_docker.py requirements.txt ddl docker easytodo-grocery-list.code-workspace legacy/v1/
mkdir -p legacy/v1/docs && git mv docs/*.yml legacy/v1/docs/
```
Popraw `.gitignore` jak w **Files**. Na górze `README.md` dodaj jedną linię: „v2 w trakcie przebudowy — stara wersja w `legacy/v1/` i tagu `legacy-v1`”.

- [ ] **Step 2: Utwórz `compose.test.yml` i `pyproject.toml`**

`compose.test.yml`: serwis `db` z `postgres:17-alpine`, `POSTGRES_USER/PASSWORD=easytodo`, `POSTGRES_DB=easytodo_test`, port `54329:5432`, `tmpfs: /var/lib/postgresql/data`, healthcheck `pg_isready`.
`pyproject.toml`: zależności z sekcji Tech Stack (+ `email-validator`, `python-multipart`); grupa dev: `pytest`, `pytest-asyncio`, `httpx`, `asgi-lifespan`, `websockets`, `pytest-playwright`, `ruff`. `[tool.pytest.ini_options]`: `asyncio_mode="auto"`, `asyncio_default_fixture_loop_scope="session"`, `asyncio_default_test_loop_scope="session"`, `markers=["e2e: browser tests"]`, `addopts='-m "not e2e"'`, `testpaths=["tests"]`. Pakiet w układzie `src/`.

- [ ] **Step 3: Napisz failing testy**

```python
# tests/test_health.py
async def test_healthz_reports_db_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}

# tests/test_config.py
def test_secret_key_must_have_32_chars():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://u:p@h/db", secret_key="short")

def test_cookie_secure_follows_public_url():
    s = Settings(database_url="postgresql+asyncpg://u:p@h/db", secret_key="x" * 40, public_url="https://todo.example.com")
    assert s.cookie_secure is True
    assert s.public_origin == "https://todo.example.com"
```

- [ ] **Step 4: Uruchom i potwierdź porażkę**

Run: `docker compose -f compose.test.yml up -d --wait && cd backend && uv sync && uv run pytest -v`
Expected: FAIL (`ModuleNotFoundError: easytodo.main` / brak `Settings`)

- [ ] **Step 5: Zaimplementuj `config.py`, `db.py`, `main.py`, `migrations/env.py`, `conftest.py`**

`env.py` bierze URL z `get_settings().database_url`, chyba że `config.attributes["url"]` jest ustawione (conftest przekazuje URL testowy). Na tym etapie brak modeli — `target_metadata = Base.metadata`.

- [ ] **Step 6: Uruchom testy**

Run: `cd backend && uv run pytest -v`
Expected: PASS (3 testy)

- [ ] **Step 7: CI i Makefile**

`.github/workflows/ci.yml`: job `test` na `ubuntu-latest` z serwisem `postgres:17-alpine` (port 54329→5432, user/pass/db jak w compose.test), kroki: `astral-sh/setup-uv`, `uv sync`, `uv run ruff check`, `uv run alembic upgrade head`, `uv run alembic check`, `uv run pytest`. `Makefile` cele: `test-db`, `test`, `e2e`, `css`, `run`, `lint`.
Run: `cd backend && uv run ruff check && uv run alembic upgrade head && uv run alembic check`
Expected: `No new upgrade operations detected.`

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "chore: move v1 to legacy/, scaffold FastAPI backend with settings, healthz, test DB and CI"
```

### Task 2: Pomiar bazowej wydajności legacy

**Files:**
- Create: `scripts/bench.py`, `docs/performance.md`
- Test: `backend/tests/test_bench.py`

**Interfaces:**
- Produces: `scripts/bench.py` CLI: `uv run python ../scripts/bench.py --target {legacy,v2} --base-url URL [--email E --password P] [--requests 200] [--categories 20] [--items 30]`; drukuje wiersze tabeli Markdown `| scenario | p50 ms | p95 ms |` dla scenariuszy `view_board`, `add_item`, `toggle_item`. Funkcja `percentile(values: list[float], p: float) -> float` (interpolacja liniowa, jak `statistics.quantiles(method="inclusive")`).
  - legacy: seed przez `/v1/add_category` i `/v1/add_task/` (nagłówki `Accept: application/vnd.myapi.v1+json`, `Content-Type: application/json`), `view_board` = `GET /`, `toggle_item` = `PUT /v1/toggle_task/<id>`.
  - v2 (używane w Task 19): logowanie formularzem, seed przez endpointy z Task 8, `view_board` = `GET /boards/{id}`.

- [ ] **Step 1: Failing test**

```python
def test_percentile_matches_inclusive_quantiles():
    data = [float(x) for x in range(1, 101)]
    assert percentile(data, 50) == pytest.approx(50.5)
    assert percentile(data, 95) == pytest.approx(95.05)
```
(import przez `importlib` ze ścieżki `scripts/bench.py`)

- [ ] **Step 2: Run** `cd backend && uv run pytest tests/test_bench.py -v` → FAIL (brak pliku)
- [ ] **Step 3: Zaimplementuj `scripts/bench.py`** (httpx.AsyncClient, sekwencyjnie, 10 żądań rozgrzewki poza pomiarem, `time.perf_counter`)
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Zmierz legacy**

Uzupełnij `legacy/v1/docker/v1.0.0/with-db/.env`, potem:
Run: `docker compose -f legacy/v1/docker/v1.0.0/with-db/docker-compose.yml up -d --build && uv run python ../scripts/bench.py --target legacy --base-url http://localhost:<APP_HOST_PORT>`
Expected: trzy wiersze z liczbami. Zapisz je w `docs/performance.md` w sekcji „Baseline v1 (legacy)” razem z datą, CPU maszyny i liczbą żądań. Zatrzymaj legacy (`down -v`).

- [ ] **Step 6: Commit** `git add scripts/bench.py backend/tests/test_bench.py docs/performance.md && git commit -m "perf: add benchmark script and record v1 baseline"`

---

## Faza 1 — Izolacja danych, logowanie, CRUD (Priorytet 1)

### Task 3: Modele danych i migracja 0001

**Files:**
- Create: `backend/src/easytodo/models/{__init__,user,auth,board,content}.py`, `backend/migrations/versions/0001_initial.py` (autogenerate, potem ręczny przegląd), `backend/tests/factories.py`
- Test: `backend/tests/test_models.py`

**Interfaces:**
- Produces (tabela → kolumny; wszystkie PK `bigint identity`, FK `ondelete="CASCADE"` chyba że zaznaczono):
  - `User` / `users`: `id`, `email: str(320)` unikalny, `CHECK (email = lower(email))`, `password_hash: str`, `is_admin: bool=False`, `status: UserStatus` (`StrEnum`: `active`, `disabled`; Postgres enum `user_status`), `must_change_password: bool=False`, `totp_secret_enc: str|None`, `totp_enabled: bool=False`, `totp_last_step: int|None`, `failed_login_count: int=0`, `locked_until: datetime|None`, `created_at`, `last_login_at: datetime|None`.
  - `UserSession` / `user_sessions`: `id`, `user_id→users`, `token_hash: str(64)` unikalny, `mfa_pending: bool=False`, `mfa_failures: int=0`, `created_at`, `expires_at`, `last_seen_at`, `user_agent: str(255)|None`, `ip: str(45)|None`; indeks `user_id`.
  - `RecoveryCode` / `recovery_codes`: `id`, `user_id→users`, `code_hash: str(64)`, `used_at: datetime|None`.
  - `PasswordResetToken` / `password_reset_tokens`: `id`, `user_id→users`, `token_hash: str(64)` unikalny, `created_at`, `expires_at`, `used_at|None`.
  - `Board` / `boards`: `id`, `name: str(80)`, `owner_id→users`, `created_at`, `updated_at`.
  - `BoardMember` / `board_members`: PK (`board_id→boards`, `user_id→users`), `role: BoardRole` (`StrEnum`: `owner`, `editor`, `viewer`; enum `board_role`), `joined_at`; indeks `user_id`.
  - `BoardInvite` / `board_invites`: `id`, `board_id→boards`, `token_hash: str(64)` unikalny, `email: str(320)|None` (None = zaproszenie linkiem), `role: BoardRole` (`CHECK role <> 'owner'`), `created_by→users`, `created_at`, `expires_at`, `accepted_at|None`, `revoked_at|None`.
  - `Category` / `categories`: `id`, `board_id→boards`, `name: str(120)`, `position: int`, `created_at`, `updated_at`; unikalny indeks `uq_categories_board_id_lower_name` na `(board_id, lower(name))`; indeks `(board_id, position)`.
  - `Item` / `items`: `id`, `board_id→boards` (denormalizacja dla szybkiej autoryzacji i zapytania o cały board jednym SELECT-em), `category_id→categories`, `name: str(200)`, `description: str(2000)|None`, `is_done: bool=False`, `done_at|None`, `created_at`, `updated_at`; indeksy `category_id`, `board_id`.
  - Jeden model `Item` obsługuje zarówno zadania, jak i zakupy (te same pola co w v1; YAGNI dla osobnych typów).
  - `tests/factories.py`: `DEFAULT_PASSWORD = "correct-horse-battery-staple"`; `async def create_user(db, email="user@example.com", password=DEFAULT_PASSWORD, *, is_admin=False, must_change_password=False, status=UserStatus.ACTIVE) -> User` (hash przez `hash_password` z Task 4 — do tego czasu `argon2.PasswordHasher().hash`); `async def create_board(db, owner, name="Groceries", members: dict[User, BoardRole] = {}) -> Board`; `async def create_category(db, board, name="Dairy", position=0) -> Category`; `async def create_item(db, category, name="Milk", is_done=False) -> Item`.

- [ ] **Step 1: Failing testy**

```python
async def test_migrations_match_models():
    command.check(alembic_cfg())  # raises AutoGenerateDiffsDetected on drift

async def test_category_name_unique_per_board_case_insensitive(db):
    u = await create_user(db); b1 = await create_board(db, u); b2 = await create_board(db, u, "Other")
    await create_category(db, b1, "Milk"); await create_category(db, b2, "milk")  # OK
    with pytest.raises(IntegrityError):
        await create_category(db, b1, "MILK")

async def test_deleting_board_cascades(db):
    # board z kategorią, pozycją, członkiem i zaproszeniem → po delete wszystkie COUNT(*) == 0

async def test_email_must_be_lowercase(db):
    with pytest.raises(IntegrityError):
        db.add(User(email="Dave@x.com", password_hash="h")); await db.flush()

async def test_timestamps_default_to_insert_time(db):
    i1 = await create_item(db, cat); await asyncio.sleep(0.05); i2 = await create_item(db, cat, "Eggs")
    assert i2.created_at > i1.created_at
    assert abs(i1.created_at - datetime.now(UTC)) < timedelta(seconds=5)

async def test_downgrade_to_base_and_back(): # command.downgrade(cfg,"base"); command.upgrade(cfg,"head") bez błędów
```

- [ ] **Step 2: Run** `uv run pytest tests/test_models.py -v` → FAIL (brak modeli)
- [ ] **Step 3: Zaimplementuj modele**, potem `uv run alembic revision --autogenerate -m "initial" --rev-id 0001`. Przejrzyj plik ręcznie: indeks na `lower(name)` i typy enum muszą być w `upgrade()`, a `downgrade()` musi dropować enumy.
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(db): add users, sessions, boards, members, invites, categories, items with migration 0001"`

### Task 4: Prymitywy bezpieczeństwa, błędy, nagłówki

**Files:**
- Create: `backend/src/easytodo/security/{passwords,tokens,crypto,csrf,ratelimit}.py`, `backend/src/easytodo/errors.py`, `backend/src/easytodo/middleware.py`, `backend/src/easytodo/web.py`
- Modify: `backend/src/easytodo/main.py` (middleware + handlery błędów), `backend/tests/factories.py` (użyj `hash_password`)
- Test: `backend/tests/security/test_passwords.py`, `test_tokens.py`, `test_crypto.py`, `test_ratelimit.py`, `test_csrf.py`, `test_errors.py`, `test_headers.py`

**Interfaces:**
- Produces:
  - `passwords.py`: `hash_password(pw: str) -> str`, `verify_password(hash: str, pw: str) -> bool`, `needs_rehash(hash: str) -> bool`, `validate_password(pw: str) -> None` (rzuca `ValidationFailed("Password must be at least 10 characters.")` / `"Password must be at most 256 characters."`).
  - `tokens.py`: `new_token() -> str` (`secrets.token_urlsafe(32)`), `hash_token(token: str) -> str` (sha256 hex).
  - `crypto.py`: `encrypt(plaintext: str, settings) -> str`, `decrypt(ciphertext: str, settings) -> str` (Fernet; klucz = `urlsafe_b64encode(HKDF-SHA256(secret_key, info=b"easytodo-fernet", length=32))`); `class DecryptionError(Exception)`.
  - `ratelimit.py`: `class RateLimiter: __init__(self, clock: Callable[[], float] = time.monotonic)`, `def hit(self, key: str, limit: int, window_s: int) -> bool` (okno stałe, w pamięci procesu); dependency factory `rate_limit(scope: str, limit: int, window_s: int)` — klucz `f"{scope}:{client_ip}"`, przy przekroczeniu `HTTPException(429, "Too many requests. Try again later.")`. Instancja w `app.state.rate_limiter`.
  - `csrf.py`: `CSRF_COOKIE = "easytodo_csrf"`, `CSRF_HEADER = "X-CSRF-Token"`, `CSRF_FIELD = "csrf_token"`; `async def verify_csrf(request: Request) -> None` (dla metod bezpiecznych nic; inaczej: `Origin` albo `Referer` musi mieć origin == `settings.public_origin`, a nagłówek lub pole formularza == cookie, porównanie `hmac.compare_digest`; błąd ⇒ `Forbidden("Invalid CSRF token.")`). Dodawana jako zależność globalna `FastAPI(dependencies=[Depends(verify_csrf)])`.
  - `middleware.py`: `CsrfCookieMiddleware` (gdy brak cookie generuje `new_token()`, ustawia `request.state.csrf_token` i cookie `httponly, samesite=lax, secure=settings.cookie_secure, path=/`); `SecurityHeadersMiddleware` ustawia dokładnie:
    `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`,
    `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`, a przy `cookie_secure` także `Strict-Transport-Security: max-age=31536000; includeSubDomains`.
  - `errors.py`: `class AppError(Exception)` z `status_code` i `message`; `NotFound` (404, domyślnie `"Not found."`), `Forbidden` (403), `Conflict` (409), `ValidationFailed` (422), `NotAuthenticated` (401). Handler: dla żądań HTMX (`HX-Request: true`) zwraca pusty body ze statusem, `HX-Reswap: none` i `HX-Trigger: {"toast": {"level": "error", "message": <msg>}}`, a dla `NotFound` dodatkowo `"resync": true` w `HX-Trigger`; `NotAuthenticated` + HTMX ⇒ 401 i `HX-Redirect: /login`; `NotAuthenticated` bez HTMX ⇒ 303 na `/login?next=<urlencoded path+query>`; pozostałe bez HTMX ⇒ render `errors/error.html` z kodem. `RequestValidationError` mapowany jak `ValidationFailed` z pierwszym komunikatem.
  - `web.py`: `templates = Jinja2Templates(directory=<repo>/frontend/templates)` z autoescape; `def render(request, name: str, context: dict, status_code=200) -> HTMLResponse` (dokłada `csrf_token`, `current_user`, `settings`); `def is_htmx(request) -> bool`.

- [ ] **Step 1: Failing testy** (wybrane asercje; resztę w tym samym stylu)

```python
def test_hash_is_argon2id_and_salted():
    h1, h2 = hash_password("long-enough-pw"), hash_password("long-enough-pw")
    assert h1.startswith("$argon2id$") and h1 != h2
    assert verify_password(h1, "long-enough-pw") and not verify_password(h1, "wrong-password")

@pytest.mark.parametrize("pw,ok", [("a" * 9, False), ("a" * 10, True), ("a" * 256, True), ("a" * 257, False)])
def test_validate_password_length(pw, ok): ...

def test_decrypt_with_other_secret_fails(): # encrypt z secret A, decrypt z secret B → DecryptionError

def test_rate_limiter_resets_after_window():
    t = [0.0]; rl = RateLimiter(clock=lambda: t[0])
    assert all(rl.hit("k", 5, 60) for _ in range(5)); assert rl.hit("k", 5, 60) is False
    t[0] = 61; assert rl.hit("k", 5, 60) is True

async def test_post_without_csrf_header_is_403(csrf_app_client): ...      # mini-app z jednym POST
async def test_post_with_foreign_origin_is_403(csrf_app_client): ...     # Origin: https://evil.example
async def test_post_with_matching_token_and_origin_passes(csrf_app_client): ...
async def test_form_field_token_is_accepted(csrf_app_client): ...

async def test_security_headers_present(client):
    r = await client.get("/healthz")
    assert r.headers["content-security-policy"].startswith("default-src 'self'; script-src 'self';")
    assert r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers  # http w testach

async def test_htmx_not_found_triggers_toast_and_resync(err_app_client):
    r = await err_app_client.get("/raise-404", headers={"HX-Request": "true"})
    assert r.status_code == 404 and r.headers["hx-reswap"] == "none"
    assert json.loads(r.headers["hx-trigger"]) == {"toast": {"level": "error", "message": "Not found."}, "resync": True}
```
`csrf_app_client` / `err_app_client`: fixture budujące mini-FastAPI z tymi samymi middleware/handlerami (bez tras testowych w produkcyjnej aplikacji).

- [ ] **Step 2: Run** `uv run pytest tests/security -v` → FAIL
- [ ] **Step 3: Zaimplementuj moduły** wg Interfaces; podłącz w `create_app`
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(security): argon2 passwords, tokens, fernet, csrf, rate limiter, security headers, error handlers"`

### Task 5: Powłoka frontendu, rejestracja, logowanie, wylogowanie

**Files:**
- Create: `frontend/templates/base.html`, `frontend/templates/auth/{login,register}.html`, `frontend/templates/home.html` (tymczasowy: „Signed in as {email}”), `frontend/templates/errors/error.html`, `frontend/templates/partials/_toast.html`, `frontend/static/js/app.js`, `frontend/styles/input.css`, `frontend/static/vendor/{htmx-2.0.4.min.js, alpine-csp-3.14.8.min.js, sortable-1.15.6.min.js}`, `frontend/static/images/favicon.webp` (z `legacy/v1/app/static/images/`)
- Create: `backend/src/easytodo/auth/{sessions,service,deps,routes}.py`
- Modify: `main.py` (mount `/static` → `frontend/static`, router auth), `Makefile` (`css`: `./bin/tailwindcss -i frontend/styles/input.css -o frontend/static/css/app.css --minify`; `bin/tailwindcss` pobierany z wydania `v4.1.x` dla bieżącej platformy)
- Test: `backend/tests/auth/test_register_login.py`, `backend/tests/helpers.py`

**Interfaces:**
- Consumes: Task 3 modele, Task 4 `hash_password`, `verify_password`, `validate_password`, `new_token`, `hash_token`, `rate_limit`, `render`, błędy.
- Produces:
  - `sessions.py`: `SESSION_COOKIE = "easytodo_session"`; `async def create_session(db, user: User, *, mfa_pending: bool, user_agent: str|None, ip: str|None, settings) -> tuple[str, UserSession]` (zwraca surowy token); `async def load_session(db, raw_token: str) -> UserSession | None` (pomija wygasłe; `last_seen_at` aktualizuje najwyżej raz na 5 min); `async def revoke_session(db, session_id: int) -> None`; `async def revoke_all_sessions(db, user_id: int, *, except_session_id: int | None = None) -> list[int]` (zwraca usunięte id — Task 12 publikuje je jako `sessions.revoked`); `def set_session_cookie(response, raw_token, settings)`, `def clear_session_cookie(response)`.
  - `service.py`: `def normalize_email(email: str) -> str` (`strip().lower()`, walidacja `email_validator` bez sprawdzania DNS, błąd ⇒ `ValidationFailed("Enter a valid email address.")`); `async def register_user(db, email: str, password: str) -> User` (`Conflict("An account with this email already exists.")`); `async def authenticate(db, email: str, password: str) -> User` — błędy: `InvalidCredentials` (`"Invalid email or password."`, ten sam dla nieznanego e-maila; dla nieznanego e-maila i tak wykonaj `verify_password` na stałym haśle-atrapie, żeby czas odpowiedzi był podobny), `AccountLocked` (`"Too many failed attempts. Try again in 15 minutes."`, blokada po 10 kolejnych porażkach na 15 min), `AccountDisabled` (`"This account is disabled."`); udane logowanie zeruje `failed_login_count`, ustawia `last_login_at`, robi rehash, gdy `needs_rehash`.
  - `deps.py`: `async def current_session(request, db) -> UserSession | None`; `async def require_user(request, db) -> User` (brak sesji ⇒ `NotAuthenticated`; `mfa_pending` ⇒ redirect 303 na `/login/2fa` — od Task 13; `status=disabled` ⇒ `NotAuthenticated`); `request.state.user` i `request.state.session` ustawiane dla szablonów.
  - Trasy: `GET/POST /register` (404 gdy `registration_open=False`; limit 10/h na IP), `GET/POST /login` (limit 20/5 min na IP; parametr `next` akceptowany tylko jeśli zaczyna się od `/` i nie od `//`, inaczej `/`), `POST /logout`, `GET /` (na razie `home.html`, od Task 7 redirect na `/boards`). Formularze bez HTMX: błąd ⇒ ponowny render formularza ze statusem 400 i komunikatem.
  - Każde udane logowanie tworzy **nową** sesję (nowy token).
  - `base.html`: `<meta name="csrf-token" content="{{ csrf_token }}">`, `<meta name="htmx-config" content='{"includeIndicatorStyles": false, "defaultSwapStyle": "outerHTML"}'>`, skrypty vendor + `app.js` z `defer`; `app.js`: na `htmx:configRequest` dodaje `X-CSRF-Token` z meta oraz `X-Client-Id` (losowe `crypto.randomUUID()` per karta, w `sessionStorage`); nasłuchuje `toast` i pokazuje komunikat w `#toasts` (aria-live="polite").
  - `tests/helpers.py`: `async def login(client, email: str, password: str = DEFAULT_PASSWORD) -> None` (GET `/login` ⇒ cookie CSRF; POST formularza; ustawia `client.headers["X-CSRF-Token"]` na wartość cookie; asercja 303).

- [ ] **Step 1: Failing testy**

```python
async def test_register_logs_in_and_normalizes_email(client, db):
    await client.get("/register")
    r = await client.post("/register", data={"email": " Dave@Example.COM ", "password": DEFAULT_PASSWORD, "csrf_token": client.cookies["easytodo_csrf"]})
    assert r.status_code == 303
    assert (await db.scalar(select(User.email))) == "dave@example.com"
    cookie = r.headers["set-cookie"]; assert "easytodo_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

async def test_email_is_normalized_everywhere(client, db):   # Review Focus 5
    await create_user(db, "dave@example.com")
    await login(client, "  DAVE@example.com")                # logowanie działa
    # rejestracja " Dave@Example.com " → 400 "An account with this email already exists."

async def test_register_rejects_short_password(client): ... # 400, "Password must be at least 10 characters."
async def test_register_disabled_returns_404(client_factory): ...  # c = await client_factory(registration_open=False); GET /register → 404
async def test_wrong_password_and_unknown_email_same_message(client, db): ...  # oba 400 z "Invalid email or password."
async def test_lockout_after_10_failures(client, db):
    # 10× złe hasło, potem poprawne → 400 "Too many failed attempts. Try again in 15 minutes."
async def test_disabled_account_cannot_login(client, db): ...
async def test_login_rotates_session_token(client, db): ...  # dwa logowania ⇒ dwa różne cookie, oba wiersze w user_sessions
async def test_logout_revokes_session(client, db):
    # login; zapamiętaj cookie; POST /logout; GET / z zapamiętanym cookie → 303 /login?next=%2F; COUNT(user_sessions)==0
async def test_anonymous_htmx_request_gets_hx_redirect(client):
    r = await client.get("/", headers={"HX-Request": "true"}); assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"
async def test_next_param_rejects_external_urls(client, db): # next=https://evil.example i next=//evil.example → Location "/"
async def test_expired_session_is_rejected(client, db): ... # ustaw expires_at w przeszłości → 303 /login
async def test_login_rate_limited_per_ip(client): ...        # 21. POST w oknie → 429
```

- [ ] **Step 2: Run** `uv run pytest tests/auth -v` → FAIL
- [ ] **Step 3: Zaimplementuj** sessions/service/deps/routes, szablony (formularze z polem `csrf_token`, mobile-first: pojedyncza kolumna `max-w-sm`, inputy min. 44 px wysokości, `autocomplete="email"` / `"current-password"` / `"new-password"`), `app.js`, vendor; zbuduj CSS `make css`.
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Sprawdź ręcznie** `make run` → `/register` w przeglądarce: konsola bez błędów CSP.
- [ ] **Step 6: Commit** `git commit -m "feat(auth): registration, login with lockout, logout, server-side sessions, base layout"`

### Task 6: Konto — zmiana hasła, wymuszona zmiana, sesje

**Files:**
- Create: `backend/src/easytodo/auth/account_routes.py`, `frontend/templates/account/{index,password}.html`
- Modify: `backend/src/easytodo/auth/deps.py` (wymuszenie zmiany hasła)
- Test: `backend/tests/auth/test_account.py`

**Interfaces:**
- Consumes: Task 5 `require_user`, `revoke_all_sessions`, `validate_password`.
- Produces:
  - `GET /account` (dane konta, lista sesji: urządzenie z `user_agent`, IP, `last_seen_at`, oznaczenie „This device”), `GET/POST /account/password` (pola `current_password`, `new_password`, `new_password_confirm`; błędy: `"Current password is incorrect."`, `"Passwords do not match."`; sukces ⇒ `revoke_all_sessions(except_session_id=current)`, `must_change_password=False`, redirect 303 `/account` z toastem `"Password changed."`), `POST /account/sessions/{session_id}/revoke` (tylko własne; cudze ⇒ 404), `POST /account/sessions/revoke-others`.
  - `require_user`: gdy `user.must_change_password` i ścieżka nie jest w `{"/account/password", "/logout"}` ⇒ dla GET redirect 303 na `/account/password`, dla innych `Forbidden("You must change your password first.")`.

- [ ] **Step 1: Failing testy**

```python
async def test_change_password_keeps_current_and_revokes_other_sessions(client_factory, db): ...
    # 2 klienty zalogowane; zmiana hasła z klienta A; A GET /account → 200; B GET /account → 303 /login
async def test_change_password_requires_current_password(client, db): ...  # 400 "Current password is incorrect."
async def test_must_change_password_redirects_everything(client, db):
    await create_user(db, "temp@example.com", must_change_password=True); await login(client, "temp@example.com")
    r = await client.get("/"); assert r.status_code == 303 and r.headers["location"] == "/account/password"
async def test_must_change_password_flag_cleared_after_change(client, db): ...
async def test_cannot_revoke_someone_elses_session(client_factory, db): ...  # 404
async def test_revoke_others_leaves_only_current(client_factory, db): ...
```
- [ ] **Step 2: Run** `uv run pytest tests/auth/test_account.py -v` → FAIL
- [ ] **Step 3: Zaimplementuj** trasy, szablony i warunek w `require_user`
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(account): password change, forced password change, session management"`

### Task 7: Boardy — uprawnienia i CRUD

**Files:**
- Create: `backend/src/easytodo/boards/{permissions,service,routes}.py`, `backend/src/easytodo/realtime/events.py`, `frontend/templates/boards/{list,board}.html`
- Modify: `auth/service.py` (`register_user` tworzy board „My list”), `auth/routes.py` (`GET /` ⇒ 303 na `/boards`)
- Test: `backend/tests/boards/test_permissions.py`, `backend/tests/boards/test_boards.py`

**Interfaces:**
- Consumes: Task 3 modele, Task 5 `require_user`.
- Produces:
  - `permissions.py`: `class Action(StrEnum)`: `VIEW`, `EDIT_CONTENT`, `MANAGE_MEMBERS`, `MANAGE_BOARD`; `def can(role: BoardRole, action: Action) -> bool` — viewer: VIEW; editor: VIEW, EDIT_CONTENT; owner: wszystko; `async def require_board(db, board_id: int, user: User, action: Action) -> tuple[Board, BoardRole]` (brak członkostwa ⇒ `NotFound("Board not found.")`, brak uprawnienia ⇒ `Forbidden("You don't have permission to do that.")`). Admin **nie** ma dostępu do cudzych boardów.
  - `realtime/events.py`: `class EventType(StrEnum)`: `BOARD_UPDATED="board.updated"`, `BOARD_DELETED="board.deleted"`, `CATEGORY_CHANGED="category.changed"`, `CATEGORY_DELETED="category.deleted"`, `CATEGORIES_REORDERED="categories.reordered"`, `MEMBER_REMOVED="member.removed"`, `SESSIONS_REVOKED="sessions.revoked"`, `RESYNC="resync"`; `@dataclass(frozen=True) class BoardEvent: type: EventType; board_id: int | None = None; category_ids: tuple[int, ...] = (); user_id: int | None = None; session_ids: tuple[int, ...] = (); origin: str | None = None`, metody `to_json() -> str`, `from_json(s) -> BoardEvent`; `current_origin: ContextVar[str | None]` (ustawiana przez `ClientIdMiddleware` z nagłówka `X-Client-Id`, max 64 znaki); `async def commit_and_publish(db: AsyncSession, *events: BoardEvent) -> None` — w tym tasku tylko `await db.commit()` (Task 12 dokłada `pg_notify`). **Każdy serwis zmieniający dane boardu commituje wyłącznie przez tę funkcję.**
  - `service.py`: `async def create_board(db, owner: User, name: str) -> Board`; `async def list_boards(db, user) -> list[BoardSummary]` (`BoardSummary(board, role, member_count, open_items)`, sort: najpierw własne, potem po nazwie; jedno zapytanie z agregacją); `async def rename_board(db, board_id, user, name) -> Board` (MANAGE_BOARD, event `BOARD_UPDATED`); `async def delete_board(db, board_id, user) -> None` (MANAGE_BOARD, event `BOARD_DELETED`); `async def get_board_view(db, board_id, user) -> BoardView` — `BoardView(board, role, categories: list[CategoryView])`, `CategoryView(category, items: list[Item])`; **dokładnie dwa zapytania** po autoryzacji: kategorie po `position`, pozycje boardu po `(is_done, lower(name), id)`, grupowane w Pythonie. Walidacja nazwy boardu: po `strip()` 1–80 znaków, inaczej `ValidationFailed("Board name must be 1–80 characters.")`.
  - Trasy: `GET /boards`, `POST /boards` (⇒ 303 `/boards/{id}`), `GET /boards/{id}` (pełna strona; zawartość z Task 9), `PATCH /boards/{id}` (pole `name`), `DELETE /boards/{id}` (HTMX ⇒ `HX-Redirect: /boards`).

- [ ] **Step 1: Failing testy**

```python
@pytest.mark.parametrize("role,action,allowed", [
    (BoardRole.VIEWER, Action.VIEW, True), (BoardRole.VIEWER, Action.EDIT_CONTENT, False),
    (BoardRole.EDITOR, Action.EDIT_CONTENT, True), (BoardRole.EDITOR, Action.MANAGE_MEMBERS, False),
    (BoardRole.EDITOR, Action.MANAGE_BOARD, False), (BoardRole.OWNER, Action.MANAGE_BOARD, True)])
def test_permission_matrix(role, action, allowed): assert can(role, action) is allowed

async def test_registration_creates_default_board(client, db): ...  # board "My list", rola owner
async def test_user_sees_only_own_and_shared_boards(client_factory, db): ...
async def test_non_member_gets_404_for_board(client_factory, db): ...
async def test_editor_cannot_rename_or_delete_board(client_factory, db): ...  # 403
async def test_board_view_uses_two_queries(db):  # licznik zdarzeń "before_cursor_execute" == 2 po require_board
async def test_board_name_validation(client, db): ...  # "" i 81 znaków → 422
```
- [ ] **Step 2: Run** `uv run pytest tests/boards -v` → FAIL
- [ ] **Step 3: Zaimplementuj** permissions/service/routes/events, szablon listy boardów (karty z nazwą, rolą, liczbą otwartych pozycji; formularz „New board”)
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(boards): role permissions, board CRUD, default board on registration"`

### Task 8: Kategorie i pozycje — serwis i endpointy z izolacją

**Files:**
- Create: `backend/src/easytodo/content/{validation,service,routes}.py`, `frontend/templates/boards/{_board_content,_category,_item}.html` (minimalne — pełne UI w Task 9)
- Test: `backend/tests/content/test_categories.py`, `test_items.py`, `test_isolation.py`

**Interfaces:**
- Consumes: Task 7 `require_board`, `Action`, `BoardEvent`, `EventType`, `commit_and_publish`, `CategoryView`.
- Produces (`content/service.py`; każda funkcja przyjmuje `db, ..., user: User`; obiekty spoza boardów użytkownika ⇒ `NotFound`):
  - `async def load_category(db, category_id, user, action) -> tuple[Category, BoardRole]` i `async def load_item(db, item_id, user, action) -> tuple[Item, BoardRole]` — jeden JOIN z `board_members` (`NotFound("This category no longer exists.")` / `NotFound("This item no longer exists.")`).
  - `create_category(db, board_id, user, name) -> Category` (position = max+1; duplikat nazwy ⇒ `Conflict("A category with this name already exists.")`; event `CATEGORY_CHANGED`), `rename_category(db, category_id, user, name) -> Category`, `delete_category(db, category_id, user) -> None` (event `CATEGORY_DELETED`), `reorder_categories(db, board_id, user, ordered_ids: list[int]) -> None` (musi być dokładną permutacją id kategorii boardu, inaczej `ValidationFailed("Category order is out of date. Refresh the board.")`; event `CATEGORIES_REORDERED`), `get_category_view(db, category_id, user) -> tuple[CategoryView, BoardRole]`.
  - `create_item(db, category_id, user, name, description: str | None = None) -> Item`, `update_item(db, item_id, user, *, name: str, description: str | None) -> Item`, `toggle_item(db, item_id, user) -> Item` (ustawia/czyści `done_at`), `move_item(db, item_id, user, target_category_id) -> tuple[Item, int]` (zwraca też stare `category_id`; kategoria docelowa musi należeć do **tego samego** boardu, inaczej `NotFound("This category no longer exists.")`; event `CATEGORY_CHANGED` z oboma id), `delete_item(db, item_id, user) -> int` (zwraca `category_id`), `clear_done(db, category_id, user) -> int` (liczba usuniętych). Każda zmiana pozycji ⇒ event `CATEGORY_CHANGED` z jej kategorią.
  - `validation.py`: `clean_name(value: str, *, max_len: int, label: str) -> str` (strip, zwija białe znaki, 1..max_len, komunikat `f"{label} must be 1–{max_len} characters."`); limity: kategoria 120, pozycja 200, opis ≤ 2000 (`"Description must be at most 2000 characters."`).
  - Trasy (HTMX, odpowiedź = fragment HTML; EDIT_CONTENT dla zmian, VIEW dla GET):
    - `GET /boards/{id}/content` → `_board_content.html`
    - `POST /boards/{id}/categories` (`name`) → `_category.html` (status 201)
    - `GET /categories/{id}` → `_category.html`
    - `PATCH /categories/{id}` (`name`) → `_category.html`
    - `DELETE /categories/{id}` → 200, pusty body
    - `PUT /boards/{id}/categories/order` (`ids` powtarzane pole formularza) → 204
    - `POST /categories/{id}/items` (`name`, `description?`) → `_category.html`
    - `PATCH /items/{id}` (`name`, `description`) → `_category.html`
    - `POST /items/{id}/toggle` → `_category.html`
    - `POST /items/{id}/move` (`category_id`) → `_board_content.html`
    - `DELETE /items/{id}` → `_category.html`
    - `POST /categories/{id}/clear-done` → `_category.html`
  - Elementy DOM: `id="category-{id}"` z `data-category-id`, `id="item-{id}"` z `data-item-id`, kontener `id="board-content"` z `data-board-id`.

- [ ] **Step 1: Failing testy**

```python
async def test_items_sorted_open_first_then_alphabetically(db): ...
    # "banana"(done), "Apple", "cherry" → kolejność ["Apple", "cherry", "banana"]
async def test_duplicate_category_name_is_409_case_insensitive(editor_client, board): ...
async def test_reorder_requires_exact_permutation(editor_client, board): ...  # brak/obce id → 422
async def test_viewer_can_read_but_not_modify(viewer_client, board, item): ...  # GET 200, POST toggle 403
async def test_toggle_sets_and_clears_done_at(editor_client, item, db): ...
async def test_clear_done_removes_only_done_items(editor_client, category, db): ...
async def test_names_are_rendered_escaped(editor_client, board):            # Review Focus 1
    nasty = "O'Reilly <script>alert(1)</script> \"x\""
    r = await editor_client.post(f"/boards/{board.id}/categories", data={"name": nasty})
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    # to samo dla pozycji (nazwa i opis) oraz w GET /boards/{id}
async def test_action_on_deleted_item_returns_404_with_resync(editor_client, item, db):  # Review Focus 2
    await editor_client.delete(f"/items/{item.id}")
    r = await editor_client.post(f"/items/{item.id}/toggle", headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert json.loads(r.headers["hx-trigger"]) == {"toast": {"level": "error", "message": "This item no longer exists."}, "resync": True}
async def test_other_users_ids_are_404_everywhere(client_factory, db):    # Review Focus 4
    # użytkownik B wywołuje KAŻDĄ trasę z listy Interfaces na id boardu/kategorii/pozycji A → 404; dane A bez zmian
async def test_move_item_to_category_in_other_board_is_404(client_factory, db):  # Review Focus 4
    # A jest członkiem obu boardów X i Y: przeniesienie pozycji z X do kategorii w Y → 404 (tylko w obrębie boardu)
async def test_name_length_limits(editor_client, category): ... # 201 znaków → 422 "Item name must be 1–200 characters."
```
Fixtures `editor_client`, `viewer_client`, `board`, `category`, `item` dodaj do `tests/content/conftest.py` (owner + zalogowany klient z daną rolą).

- [ ] **Step 2: Run** `uv run pytest tests/content -v` → FAIL
- [ ] **Step 3: Zaimplementuj** validation/service/routes i minimalne fragmenty
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(content): categories and items CRUD with board-scoped authorization"`

### Task 9: Widok boardu — UI mobile-first, modal, drag-and-drop

**Files:**
- Modify: `frontend/templates/boards/{board,_board_content,_category,_item}.html`
- Create: `frontend/templates/boards/_item_modal.html`, `frontend/static/js/board.js`, `backend/tests/live_server.py`, `backend/tests/e2e/conftest.py`
- Test: `backend/tests/content/test_board_markup.py`, `backend/tests/e2e/test_board_ui.py`

**Interfaces:**
- Consumes: Task 8 trasy i id elementów DOM.
- Produces:
  - Układ: telefon (<640 px) — jedna kolumna kategorii, pole „Add item” na górze każdej kategorii, przyciski min. 44×44 px; tablet — 2 kolumny; desktop (≥1024 px) — CSS `columns` (masonry jak w v1) z 3+ kolumnami. Pozycje zrobione przekreślone i na dole kategorii.
  - Interakcje: checkbox = `hx-post="/items/{id}/toggle"`; dodawanie pozycji to formularz `hx-post` z resetem pola i fokusem po swapie; klik w nazwę otwiera modal Alpine (`x-data="itemModal"`, komponent zarejestrowany w `board.js` przez `Alpine.data` — build CSP nie wykonuje wyrażeń inline) z edycją nazwy/opisu, przeniesieniem do kategorii (select) i usunięciem; menu kategorii: zmiana nazwy, „Clear done”, usuń (potwierdzenie `hx-confirm`), widz nie widzi żadnych kontrolek edycji.
  - Dane do modala przekazywane wyłącznie przez atrybuty `data-*` (autoescape), **nigdy** przez interpolację w JS.
  - `board.js`: `initSortable(root)` — SortableJS na listach pozycji z `group: "items"` (przeciąganie między kategoriami, dotyk z `delay: 150, delayOnTouchOnly: true`), `onEnd` ⇒ `htmx.ajax("POST", "/items/{id}/move", {values: {category_id}, target: "#board-content"})`; Sortable na kontenerze kategorii z uchwytem `.category-handle` ⇒ `PUT /boards/{id}/categories/order`. Ponowna inicjalizacja na `htmx:afterSwap`.
  - `tests/live_server.py`: fixture `live_server` (uruchamia `uvicorn.Server` z `create_app(settings)` w osobnym wątku na wolnym porcie, `public_url=f"http://127.0.0.1:{port}"`, zwraca `LiveServer(url, settings)`); `tests/e2e/conftest.py`: `logged_in_page(browser, live_server, email) -> Page`.

- [ ] **Step 1: Failing testy**

```python
def test_templates_have_no_inline_handlers():                       # Review Focus 1 + CSP
    for path in Path("../frontend/templates").rglob("*.html"):
        text = path.read_text()
        assert not re.search(r"\son[a-z]+\s*=", text), path
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path

async def test_viewer_sees_no_edit_controls(viewer_client, board, item):
    html = (await viewer_client.get(f"/boards/{board.id}")).text
    assert 'hx-post="/items/' not in html and "Add item" not in html

async def test_fragment_and_full_page_render_same_category(editor_client, category): ...

# e2e (pytest -m e2e)
@pytest.mark.e2e
def test_add_and_toggle_item_on_phone(logged_in_page_mobile): ...   # viewport 375×812: dodaj "Milk", odhacz, element ma klasę line-through i jest ostatni
@pytest.mark.e2e
def test_drag_item_to_other_category(logged_in_page): ...           # drag_to → pozycja w nowej kategorii po przeładowaniu
@pytest.mark.e2e
def test_no_csp_violations(logged_in_page): ...                      # zbieraj page.on("console") – brak "Content Security Policy"
```
- [ ] **Step 2: Run** `uv run pytest tests/content/test_board_markup.py -v && uv run playwright install chromium && uv run pytest -m e2e -v` → FAIL
- [ ] **Step 3: Zaimplementuj** szablony, `board.js`, `live_server`; `make css`
- [ ] **Step 4: Run** `uv run pytest -v && uv run pytest -m e2e -v` → PASS
- [ ] **Step 5: CI** — dodaj do `ci.yml` job `e2e`: `make css`, `uv run playwright install --with-deps chromium`, `uv run pytest -m e2e`
- [ ] **Step 6: Commit** `git commit -m "feat(ui): mobile-first board view with item modal and touch drag-and-drop"`

### Task 10: Udostępnianie boardów — zaproszenia i członkowie

**Files:**
- Create: `backend/src/easytodo/mail/mailer.py`, `backend/src/easytodo/mail/templates/invite.txt`, `backend/src/easytodo/boards/{invites,invite_routes}.py`, `frontend/templates/boards/{members,invite}.html`
- Modify: `main.py` (`app.state.mailer` z `get_mailer(settings)`), `auth/routes.py` (`next` po rejestracji/logowaniu z linku zaproszenia)
- Test: `backend/tests/boards/test_invites.py`, `backend/tests/boards/test_members.py`, `backend/tests/mail/test_mailer.py`

**Interfaces:**
- Consumes: Task 7 `require_board`, `Action.MANAGE_MEMBERS`, `commit_and_publish`; Task 4 `new_token`, `hash_token`.
- Produces:
  - `mailer.py`: `class Mailer(Protocol): async def send(self, to: str, subject: str, body: str) -> None`; `ConsoleMailer` (loguje i dopisuje `SentMail(to, subject, body)` do `self.outbox: list[SentMail]`), `SmtpMailer(settings)` (aiosmtplib, `start_tls=settings.smtp_starttls`, timeout 10 s; błąd wysyłki logowany, nie przerywa żądania); `get_mailer(settings) -> Mailer`; `render_mail(name: str, **ctx) -> tuple[str, str]` (subject z pierwszej linii `Subject: …`).
  - `invites.py`: `INVITE_TTL = timedelta(days=7)`; `async def create_link_invite(db, board_id, user, role: BoardRole) -> tuple[str, BoardInvite]` (wielokrotnego użytku do wygaśnięcia/odwołania); `async def create_email_invite(db, board_id, user, email: str, role, mailer, settings) -> BoardInvite` (jednorazowe; e-mail z linkiem `{public_url}/invite/{token}`); `async def get_valid_invite(db, raw_token) -> BoardInvite` (wygasłe/odwołane/zużyte e-mailowe ⇒ `NotFound("This invite is no longer valid.")`); `async def accept_invite(db, raw_token, user) -> Board` (e-mailowe: `normalize_email(user.email) == invite.email`, inaczej `Forbidden("This invite was sent to a different email address.")`; już członek ⇒ bez zmian, bez obniżania roli; event `BOARD_UPDATED`); `async def revoke_invite(db, invite_id, user) -> None`.
  - Członkowie (`boards/service.py`): `async def list_members(db, board_id, user) -> list[MemberView]` (VIEW); `async def change_role(db, board_id, user, member_user_id, role: BoardRole) -> None` (MANAGE_MEMBERS; nie można zmienić roli ownera ani nadać `owner` ⇒ `Forbidden("The owner's role can't be changed.")`); `async def remove_member(db, board_id, user, member_user_id) -> None` (MANAGE_MEMBERS; event `MEMBER_REMOVED` z `user_id`); `async def leave_board(db, board_id, user) -> None` (owner ⇒ `Conflict("The owner can't leave. Delete the board instead.")`; event `MEMBER_REMOVED`).
  - Trasy: `GET /boards/{id}/members`, `POST /boards/{id}/invites` (`role`, `email?` — pusty email ⇒ link pokazywany raz z przyciskiem „Copy”), `DELETE /boards/{id}/invites/{invite_id}`, `PATCH /boards/{id}/members/{user_id}` (`role`), `DELETE /boards/{id}/members/{user_id}`, `POST /boards/{id}/leave`, `GET /invite/{token}` (anonim ⇒ 303 `/login?next=/invite/{token}`, strona logowania pokazuje link „Create an account”, który zachowuje `next`), `POST /invite/{token}/accept` ⇒ 303 `/boards/{id}`.

- [ ] **Step 1: Failing testy**

```python
async def test_link_invite_grants_role(client_factory, db): ...        # owner tworzy link editor; B akceptuje → member editor
async def test_only_owner_can_invite(editor_client, board): ...        # 403
async def test_expired_or_revoked_invite_is_invalid(client_factory, db): ...  # 404 "This invite is no longer valid."
async def test_email_invite_sends_mail_with_link(client, board, mail_outbox): ...
    # outbox[0].to == "friend@example.com" i body zawiera f"http://testserver/invite/"
async def test_email_invite_matches_case_insensitively(client_factory, db, mail_outbox):   # Review Focus 5
    # zaproszenie na " Friend@Example.com " akceptuje konto friend@example.com
async def test_email_invite_rejects_other_account(client_factory, db, mail_outbox): ...    # 403
async def test_email_invite_is_single_use(client_factory, db, mail_outbox): ...
async def test_accepting_when_already_member_keeps_role(client_factory, db): ...  # editor akceptuje link viewer → nadal editor
async def test_anonymous_invite_visit_redirects_to_login_with_next(client, invite_token): ...
async def test_removed_member_loses_access(client_factory, db): ...    # 404 na board po usunięciu
async def test_owner_cannot_leave_or_be_demoted(client_factory, db): ...
async def test_invite_token_stored_hashed(db, ...): ...                # surowy token ∉ board_invites.token_hash
```
- [ ] **Step 2: Run** `uv run pytest tests/boards tests/mail -v` → FAIL
- [ ] **Step 3: Zaimplementuj** mailer, invites, członków, trasy, szablony; fixture `mail_outbox` w `conftest.py` = `app.state.mailer.outbox`
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(sharing): link and email invites, member roles, leave board"`

### Task 11: Docker + docker-compose z automatycznymi migracjami

**Files:**
- Create: `deploy/Dockerfile`, `deploy/entrypoint.sh`, `docker-compose.yml`, `.env.example`, `.dockerignore`
- Modify: `.github/workflows/ci.yml` (job `docker`: `docker compose build` + smoke test)

**Interfaces:**
- Consumes: `create_app` (factory), `alembic upgrade head`.
- Produces:
  - `Dockerfile` multi-stage: (1) `css` — pobiera Tailwind standalone CLI w wersji przypiętej argumentem `TAILWIND_VERSION=4.1.x` i buduje `app.css`; (2) `runtime` — `python:3.12-slim`, `uv sync --frozen --no-dev`, kopiuje `backend/`, `frontend/` (z `app.css` ze stage 1), użytkownik bez roota `app` (uid 10001), `EXPOSE 8000`, `HEALTHCHECK` przez `python -c` na `/healthz`.
  - `entrypoint.sh`: `alembic upgrade head` (z retry 10× co 2 s, gdy baza jeszcze wstaje), potem `exec uvicorn --factory easytodo.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "$EASYTODO_FORWARDED_ALLOW_IPS" --ws-ping-interval 25 --ws-ping-timeout 20 --workers "${EASYTODO_WORKERS:-1}"`.
  - `docker-compose.yml` (w roocie): `db` (`postgres:17-alpine`, wolumen `pgdata`, healthcheck `pg_isready`, bez publikowania portu), `app` (build z `deploy/Dockerfile`, `env_file: .env`, `depends_on: db: condition: service_healthy`, `ports: ["${EASYTODO_PORT:-8000}:8000"]`, `restart: unless-stopped`), `caddy` dochodzi w Task 16 (profil `proxy`).
  - `.env.example`: wszystkie pola `Settings` z komentarzami + `POSTGRES_PASSWORD`; `EASYTODO_DATABASE_URL=postgresql+asyncpg://easytodo:${POSTGRES_PASSWORD}@db:5432/easytodo`.

- [ ] **Step 1: Napisz smoke test (najpierw ma nie przejść)**

Run: `cp .env.example .env && sed -i 's/^EASYTODO_SECRET_KEY=.*/EASYTODO_SECRET_KEY='"$(openssl rand -hex 32)"'/' .env && docker compose up -d --build --wait && curl -fsS localhost:8000/healthz`
Expected (przed implementacją): błąd — brak `docker-compose.yml`.

- [ ] **Step 2: Zaimplementuj** pliki z Interfaces
- [ ] **Step 3: Zweryfikuj**

Run: komenda ze Step 1, potem `docker compose exec app alembic current` i `docker compose restart app && docker compose logs app | grep -c "Running upgrade"`
Expected: `{"status":"ok","db":"ok"}`; `0001 (head)`; po restarcie migracje nie są powtarzane (Alembic nic nie robi). Rejestracja i dodanie pozycji przez przeglądarkę pod `http://<ip-w-LAN>:8000` działa z telefonu.

- [ ] **Step 4: Run** `docker compose down -v && cd backend && uv run pytest` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(deploy): docker image and compose with automatic migrations"`

---

## Faza 2 — Real-time (Priorytet 2)

### Task 12: Synchronizacja przez WebSocket

**Files:**
- Create: `backend/src/easytodo/realtime/{hub,listener,routes}.py`, `frontend/static/js/realtime.js`
- Modify: `realtime/events.py` (`commit_and_publish` z `pg_notify`), `main.py` (start/stop huba i listenera w lifespan), `auth/sessions.py` + `auth/routes.py` + `account_routes.py` (publikacja `SESSIONS_REVOKED` przy logout/revoke), `frontend/templates/boards/board.html` (wskaźnik połączenia, `data-board-id`)
- Test: `backend/tests/realtime/test_hub.py`, `test_publish.py`, `test_ws.py`, `backend/tests/e2e/test_realtime.py`

**Interfaces:**
- Consumes: Task 7 `BoardEvent`, `EventType`, `current_origin`, `require_board`; Task 5 `load_session`, `SESSION_COOKIE`; Task 9 `live_server`.
- Produces:
  - `commit_and_publish(db, *events)`: dla każdego zdarzenia `await db.execute(text("SELECT pg_notify('board_events', :p)"), {"p": event_with_origin.to_json()})` przed `await db.commit()` (origin = `current_origin.get()`); payload < 8000 B.
  - `listener.py`: `class EventListener: __init__(self, dsn: str, on_event: Callable[[BoardEvent], Awaitable[None]], on_reconnect: Callable[[], Awaitable[None]])`, `async def start()`, `async def stop()`; dedykowane połączenie `asyncpg.connect` (DSN z `database_url` bez `+asyncpg`) z `add_listener("board_events", …)`; zerwanie połączenia ⇒ ponawianie z backoffem 1→30 s, po odzyskaniu woła `on_reconnect` (zdarzenia mogły przepaść).
  - `hub.py`: `@dataclass class Subscriber: board_id: int; user_id: int; session_id: int; client_id: str | None; queue: asyncio.Queue[str]` (`maxsize=100`); `class Hub`: `def subscribe(self, sub: Subscriber) -> None`, `def unsubscribe(self, sub) -> None`, `async def dispatch(self, event: BoardEvent) -> None` — routing:
    - zdarzenia z `board_id` ⇒ subskrybenci boardu z wyjątkiem `client_id == event.origin`;
    - `MEMBER_REMOVED` ⇒ subskrybent o `user_id` dostaje zdarzenie i jest zamykany kodem 4403; pozostali dostają `board.updated`;
    - `BOARD_DELETED` ⇒ wszyscy na boardzie dostają zdarzenie i są zamykani kodem 4404;
    - `SESSIONS_REVOKED` ⇒ subskrybenci z `session_id in event.session_ids` zamykani kodem 4401;
    - pełna kolejka ⇒ zamknięcie kodem 1013 (klient się resynchronizuje).
    `async def broadcast_resync(self) -> None` (do wszystkich `{"type": "resync"}`).
  - `routes.py`: `WS /ws/boards/{board_id}?client_id=…` — przed `accept()` sprawdza: `Origin` == `settings.public_origin` (inaczej zamknięcie 4403), sesja z cookie ważna i nie `mfa_pending` (4401), członkostwo VIEW (4404). Wiadomości serwer→klient: JSON `{"type", "board_id", "category_ids"}`; klient nic nie wysyła (poza pingami protokołu).
  - `realtime.js`: `connectBoard(boardId)` — przy `open` **zawsze** `htmx.ajax("GET", "/boards/{id}/content", "#board-content")`; `category.changed` ⇒ dla każdego id: jeśli `#category-{id}` istnieje, `GET /categories/{id}` (outerHTML), w przeciwnym razie odśwież cały board; `category.deleted` ⇒ usuń `#category-{id}`; `categories.reordered` / `board.updated` / `resync` ⇒ odśwież cały board; `board.deleted` / `member.removed` ⇒ `location = "/boards"`; zamknięcie z kodem 4401 ⇒ `location = "/login"`; inne zamknięcia ⇒ reconnect z backoffem 1, 2, 4… max 30 s z losowym jitterem; `visibilitychange` (powrót karty) i `online` ⇒ natychmiastowy reconnect, jeśli socket nie jest otwarty. Nie odświeża fragmentu, w którym użytkownik ma otwarty modal edycji — wtedy odświeżenie po zamknięciu modala. Wskaźnik `#conn-status`: „Live” / „Reconnecting…”.

- [ ] **Step 1: Failing testy**

```python
async def test_dispatch_skips_origin_client_and_other_boards():
    hub = Hub(); a = sub(board_id=1, client_id="A"); b = sub(board_id=1, client_id="B"); c = sub(board_id=2)
    for s in (a, b, c): hub.subscribe(s)
    await hub.dispatch(BoardEvent(EventType.CATEGORY_CHANGED, board_id=1, category_ids=(7,), origin="A"))
    assert a.queue.empty() and c.queue.empty()
    assert json.loads(b.queue.get_nowait()) == {"type": "category.changed", "board_id": 1, "category_ids": [7]}
async def test_full_queue_disconnects_subscriber_with_1013(): ...
async def test_member_removed_closes_only_that_user_with_4403(): ...
async def test_sessions_revoked_closes_matching_sockets_with_4401(): ...

async def test_notify_is_sent_only_after_commit(db, listener_probe): ...      # rollback ⇒ brak zdarzenia w 1 s
async def test_listener_reconnect_triggers_resync(...): ...                   # pg_terminate_backend na połączeniu listenera ⇒ broadcast_resync wywołane

# z live_server + biblioteką websockets
async def test_change_on_device_a_reaches_device_b_within_1s(live_server): ...
    # ten sam użytkownik, 2 sesje; WS z client_id "B"; POST item z X-Client-Id "A" ⇒ B dostaje category.changed < 1 s; WS "A" nic
async def test_ws_rejects_anonymous_with_4401(live_server): ...
async def test_ws_rejects_non_member_with_4404(live_server): ...
async def test_ws_rejects_foreign_origin_with_4403(live_server): ...
async def test_removed_member_socket_is_closed(live_server): ...
async def test_logout_closes_that_sessions_socket(live_server): ...
async def test_events_cross_workers(live_server_two_workers): ...             # 2 instancje create_app na jednej bazie: zmiana w 1 ⇒ zdarzenie w 2

@pytest.mark.e2e
def test_second_browser_sees_new_item_within_2s(two_logged_in_pages): ...     # dwa konteksty, ten sam board
```
- [ ] **Step 2: Run** `uv run pytest tests/realtime -v` → FAIL
- [ ] **Step 3: Zaimplementuj** publish, listener, hub, endpoint WS, `realtime.js`; dołącz skrypt w `board.html`
- [ ] **Step 4: Run** `uv run pytest -v && uv run pytest -m e2e -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(realtime): board WebSocket sync via Postgres LISTEN/NOTIFY with resync on reconnect"`

---

## Faza 3 — 2FA, reset hasła, panel admina (Priorytet 3)

### Task 13: 2FA (TOTP) z kodami zapasowymi

**Files:**
- Create: `backend/src/easytodo/security/totp.py`, `backend/src/easytodo/auth/twofa.py`, `frontend/templates/account/{2fa_setup,2fa_recovery}.html`, `frontend/templates/auth/login_2fa.html`
- Modify: `auth/routes.py` (krok 2FA w logowaniu), `auth/deps.py` (`mfa_pending`), `account/index.html` (sekcja 2FA)
- Test: `backend/tests/auth/test_totp.py`, `backend/tests/auth/test_2fa_flow.py`

**Interfaces:**
- Consumes: Task 4 `encrypt`/`decrypt`, `hash_token`, `rate_limit`; Task 5 `create_session`, `revoke_session`.
- Produces:
  - `totp.py`: `ISSUER = "EasyToDo"`; `new_totp_secret() -> str` (`pyotp.random_base32()`); `provisioning_uri(secret: str, email: str) -> str`; `qr_svg(uri: str) -> str` (segno, inline SVG); `verify_totp(secret: str, code: str, last_step: int | None) -> int | None` — akceptuje okno ±1 krok (30 s), zwraca użyty krok czasowy; ten sam lub wcześniejszy krok niż `last_step` ⇒ `None` (ochrona przed powtórzeniem).
  - `twofa.py`: `async def begin_setup(db, user, settings) -> str` (generuje sekret, zapisuje zaszyfrowany w `totp_secret_enc` z `totp_enabled=False`, zwraca sekret); `async def confirm_setup(db, user, code, settings) -> list[str]` (włącza 2FA, generuje 10 kodów zapasowych w formacie `xxxx-xxxx` z alfabetu `abcdefghjkmnpqrstuvwxyz23456789`, zapisuje hashe sha256, zwraca surowe kody do jednorazowego pokazania; zły kod ⇒ `ValidationFailed("Invalid code. Try again.")`); `async def verify_second_factor(db, user, code, settings) -> bool` (TOTP lub nieużyty kod zapasowy, który zostaje oznaczony jako użyty); `async def disable_2fa(db, user, password, code, settings) -> None`; `async def regenerate_recovery_codes(db, user, password) -> list[str]`.
  - Przepływ logowania: hasło OK i `totp_enabled` ⇒ sesja z `mfa_pending=True` i 303 na `/login/2fa`; `POST /login/2fa` (`code`; limit 10/5 min na IP) poprawny ⇒ unieważnij sesję pending i utwórz nową pełną (nowy token) ⇒ 303 na `next` lub `/boards`; 5 błędnych kodów (`mfa_failures`) ⇒ sesja pending usunięta, 303 `/login` z komunikatem `"Too many invalid codes. Sign in again."`. Sesja pending ma dostęp tylko do `/login/2fa` i `/logout`.
  - Trasy konta: `GET /account/2fa/setup` (QR + sekret tekstowo), `POST /account/2fa/setup` (`code`) ⇒ strona z kodami zapasowymi, `POST /account/2fa/disable` (`password`, `code`), `POST /account/2fa/recovery-codes` (`password`).

- [ ] **Step 1: Failing testy**

```python
def test_verify_totp_rejects_replay():
    s = new_totp_secret(); code = pyotp.TOTP(s).now()
    step = verify_totp(s, code, None); assert step is not None
    assert verify_totp(s, code, step) is None
def test_verify_totp_accepts_previous_step(): ...   # kod z t-30 s przechodzi, z t-90 s nie (pyotp.TOTP.at)

async def test_enable_2fa_and_login_requires_code(client_factory, db): ...
    # setup → confirm z poprawnym kodem → 10 kodów; nowy klient: login hasłem → 303 /login/2fa; GET /boards → 303 /login/2fa
async def test_second_step_rotates_session(client, db): ...   # cookie po 2FA ≠ cookie pending; pending usunięty
async def test_recovery_code_works_once(client_factory, db): ...
async def test_five_bad_codes_kill_pending_session(client, db): ...
async def test_totp_secret_is_encrypted_at_rest(db, user_with_2fa): ...   # totp_secret_enc ≠ sekret i decrypt == sekret
async def test_disable_2fa_requires_password_and_code(client, db): ...
async def test_pending_session_cannot_open_websocket(live_server): ...    # 4401
```
- [ ] **Step 2: Run** `uv run pytest tests/auth -v` → FAIL
- [ ] **Step 3: Zaimplementuj** totp/twofa/trasy/szablony
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(auth): optional TOTP 2FA with recovery codes"`

### Task 14: Reset hasła e-mailem

**Files:**
- Create: `backend/src/easytodo/auth/reset.py`, `backend/src/easytodo/mail/templates/password_reset.txt`, `frontend/templates/auth/{forgot,reset}.html`
- Modify: `auth/routes.py`, `auth/login.html` (link „Forgot password?”)
- Test: `backend/tests/auth/test_password_reset.py`

**Interfaces:**
- Consumes: Task 10 `Mailer`, `render_mail`; Task 5 `normalize_email`, `revoke_all_sessions`; Task 4 `new_token`, `hash_token`, `validate_password`, `rate_limit`.
- Produces:
  - `RESET_TTL = timedelta(minutes=60)`; `async def request_password_reset(db, email: str, mailer, settings) -> None` — dla nieznanego lub wyłączonego konta nic nie robi; dla istniejącego unieważnia wcześniejsze tokeny użytkownika i wysyła link `{public_url}/password/reset/{token}`; `async def reset_password(db, raw_token: str, new_password: str) -> User` (nieważny/wygasły/użyty ⇒ `NotFound("This reset link is invalid or has expired.")`; ustawia hash, `used_at`, `must_change_password=False`, `failed_login_count=0`, `locked_until=None`; `revoke_all_sessions` + event `SESSIONS_REVOKED`). Reset **nie** wyłącza 2FA.
  - Trasy: `GET/POST /password/forgot` (limit 5/h na IP; zawsze ta sama odpowiedź: `"If an account exists for that email, we've sent a reset link."`), `GET/POST /password/reset/{token}` ⇒ 303 `/login` z toastem `"Password updated. Sign in with your new password."`.

- [ ] **Step 1: Failing testy**

```python
async def test_unknown_email_gets_same_response_and_no_mail(client, mail_outbox): ...
async def test_reset_flow_changes_password_and_revokes_sessions(client_factory, db, mail_outbox): ...
    # token z treści maila (regex /password/reset/(\S+)); stare hasło nie działa, nowe działa; poprzednia sesja wylogowana
async def test_reset_token_single_use_and_expires(client, db, mail_outbox): ...  # drugi raz 404; expires_at w przeszłości 404
async def test_new_request_invalidates_previous_token(client, db, mail_outbox): ...
async def test_reset_clears_lockout(client, db, mail_outbox): ...
async def test_reset_keeps_2fa_required(client, db, user_with_2fa, mail_outbox): ...  # po resecie login → /login/2fa
async def test_forgot_is_rate_limited(client): ...  # 6. żądanie w godzinie → 429
```
- [ ] **Step 2: Run** `uv run pytest tests/auth/test_password_reset.py -v` → FAIL
- [ ] **Step 3: Zaimplementuj** reset/trasy/szablony
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(auth): password reset via emailed single-use token"`

### Task 15: Panel administratora i CLI `create-admin`

**Files:**
- Create: `backend/src/easytodo/admin/{service,routes}.py`, `backend/src/easytodo/cli.py`, `frontend/templates/admin/{index,users,_user_row,user_created}.html`
- Modify: `base.html` (link „Admin” tylko dla `is_admin`)
- Test: `backend/tests/admin/test_admin.py`, `backend/tests/test_cli.py`

**Interfaces:**
- Consumes: Task 5 `require_user`, `hash_password`, `normalize_email`, `revoke_all_sessions`; Task 14 `request_password_reset`; Task 12 `commit_and_publish` (`SESSIONS_REVOKED`).
- Produces:
  - `async def require_admin(user = Depends(require_user)) -> User` (nie-admin ⇒ `NotFound()` — panel jest niewidoczny).
  - `service.py`: `async def get_stats(db) -> AdminStats` (`users_total`, `users_active_30d` (wg `last_login_at`), `users_with_2fa`, `boards_total`, `shared_boards` (>1 członek), `categories_total`, `items_total`, `items_open`); `async def list_users(db, *, q: str | None, page: int, per_page: int = 50) -> Page[UserRow]` (`@dataclass class Page(Generic[T]): items: list[T]; total: int; page: int; per_page: int`) (`UserRow`: email, is_admin, status, totp_enabled, created_at, last_login_at, owned_boards; filtr `email ILIKE %q%` z escapowaniem `%`/`_`); `async def create_user_with_temp_password(db, email, *, is_admin: bool = False) -> tuple[User, str]` (hasło tymczasowe `secrets.token_urlsafe(12)`, `must_change_password=True`; pokazywane raz na `user_created.html`); `async def delete_user(db, admin, user_id) -> None` (sam siebie ⇒ `Conflict("You can't delete your own account here.")`; boardy, których jest właścicielem, są usuwane kaskadowo — strona potwierdzenia pokazuje ich liczbę; publikuje `BOARD_DELETED` dla każdego); `async def set_status(db, admin, user_id, status: UserStatus) -> None` (wyłączenie ⇒ `revoke_all_sessions`); `async def reset_2fa(db, admin, user_id) -> None`; ostatniego admina nie można usunąć ani wyłączyć (`Conflict("At least one active admin is required.")`).
  - Trasy pod `/admin`: `GET /admin` (statystyki), `GET /admin/users?q=&page=`, `POST /admin/users` (`email`, `is_admin`), `DELETE /admin/users/{id}`, `POST /admin/users/{id}/send-reset`, `POST /admin/users/{id}/status` (`status`), `POST /admin/users/{id}/reset-2fa`.
  - `cli.py`: `def main(argv: list[str] | None = None) -> int` (argparse); `create-admin --email E [--password-stdin]` (tworzy admina albo nadaje `is_admin` istniejącemu kontu; bez `--password-stdin` pyta przez `getpass`); uruchamianie: `docker compose exec app python -m easytodo.cli create-admin --email you@example.com`.

- [ ] **Step 1: Failing testy**

```python
async def test_non_admin_gets_404_on_admin_pages(client, db): ...
async def test_stats_counts(admin_client, db): ...   # seed: 3 userów, 2 boardy (1 współdzielony), 4 pozycje (1 zrobiona) ⇒ dokładne liczby
async def test_user_search_escapes_wildcards(admin_client, db): ...  # q="%" nie zwraca wszystkich
async def test_created_user_must_change_password(admin_client, client_factory, db): ...
    # hasło z user_created.html; login nowym kontem ⇒ 303 /account/password
async def test_delete_user_removes_owned_boards_and_sessions(admin_client, db): ...
async def test_admin_cannot_delete_self_or_last_admin(admin_client, db): ...
async def test_send_reset_emails_user(admin_client, db, mail_outbox): ...
async def test_disable_user_kills_sessions(admin_client, client_factory, db): ...
async def test_admin_cannot_open_users_board(admin_client, db): ...   # 404 — brak obejścia izolacji
async def test_cli_create_admin_promotes_existing_user(db, monkeypatch): ...  # stdin "secret-password-1"; await asyncio.to_thread(main, ["create-admin", "--email", e, "--password-stdin"]) == 0 ⇒ is_admin True
```
- [ ] **Step 2: Run** `uv run pytest tests/admin tests/test_cli.py -v` → FAIL
- [ ] **Step 3: Zaimplementuj** serwis, trasy (tabela użytkowników na desktopie, karty na telefonie), CLI
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(admin): admin panel with stats and user management, create-admin CLI"`

---

## Faza 4 — Hosting, UX, migracja danych, dokumentacja (Priorytet 4)

### Task 16: Reverse proxy z HTTPS (Caddy)

**Files:**
- Create: `deploy/Caddyfile`, `docs/deployment.md`
- Modify: `docker-compose.yml` (serwis `caddy` w profilu `proxy`), `.env.example` (`EASYTODO_DOMAIN`, `ACME_EMAIL`)
- Test: `backend/tests/test_https_settings.py`

**Interfaces:**
- Produces:
  - `caddy` (`caddy:2-alpine`, `profiles: ["proxy"]`, porty `80:80`, `443:443`, `443:443/udp`, wolumeny `caddy_data`, `caddy_config`); `Caddyfile`: `{$EASYTODO_DOMAIN} { encode zstd gzip; reverse_proxy app:8000 }` + globalnie `email {$ACME_EMAIL}`. Przy profilu `proxy` port aplikacji publikowany tylko na `127.0.0.1` (`EASYTODO_BIND=127.0.0.1`), a `EASYTODO_FORWARDED_ALLOW_IPS` ustawione na `*` (aplikacja osiągalna wyłącznie przez sieć compose).
  - `docs/deployment.md`: (a) LAN bez TLS; (b) internet z własną domeną: przekierowanie portów 80/443 na routerze, `EASYTODO_PUBLIC_URL=https://<domena>`, `docker compose --profile proxy up -d`; (c) bez otwierania portów: Cloudflare Tunnel lub Tailscale Funnel wskazujące na `app:8000`; (d) checklista: silny `EASYTODO_SECRET_KEY`, `EASYTODO_REGISTRATION_OPEN=false` po założeniu kont, backup `pg_dump` (gotowa komenda), aktualizacje (`git pull && docker compose up -d --build`).

- [ ] **Step 1: Failing testy**

```python
async def test_https_public_url_enables_secure_cookie_and_hsts(https_client):   # settings public_url="https://testserver"
    r = await https_client.get("/login")
    assert "Secure" in r.headers["set-cookie"]
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
async def test_forwarded_proto_does_not_override_public_url_origin_check(https_client): ...
    # POST z Origin http://testserver przy public_url https ⇒ 403
```
- [ ] **Step 2: Run** `uv run pytest tests/test_https_settings.py -v` → FAIL (jeśli Task 4 już to pokrył — test przechodzi od razu; wtedy zostaw go jako regresję i przejdź dalej)
- [ ] **Step 3: Zaimplementuj** Caddyfile, profil compose, dokumentację
- [ ] **Step 4: Zweryfikuj lokalnie**

Run: `EASYTODO_DOMAIN=localhost EASYTODO_PUBLIC_URL=https://localhost docker compose --profile proxy up -d --build --wait && curl -kfsS https://localhost/healthz`, potem w przeglądarce `https://localhost` (zaakceptuj certyfikat wewnętrzny Caddy), zaloguj się w dwóch oknach na ten sam board.
Expected: `{"status":"ok","db":"ok"}`; w DevTools → Network połączenie `wss://localhost/ws/boards/<id>` ma status 101, a pozycja dodana w jednym oknie pojawia się w drugim; cookie `easytodo_session` ma flagę `Secure`.

- [ ] **Step 5: Commit** `git commit -m "feat(deploy): optional Caddy reverse proxy with automatic HTTPS and deployment guide"`

### Task 17: Dopracowanie UX i testy responsywności

**Files:**
- Modify: `frontend/templates/**`, `frontend/styles/input.css`, `frontend/static/js/{app,board,realtime}.js`
- Test: `backend/tests/e2e/test_responsive.py`, `backend/tests/e2e/test_resync.py`

**Interfaces:**
- Consumes: wszystko z Task 9 i 12.
- Produces (zakres polerowania — dokładnie te elementy):
  - Ciemny motyw wg `prefers-color-scheme`.
  - Puste stany: board bez kategorii („Add your first category”), kategoria bez pozycji.
  - Przycisk „Clear done (n)” widoczny tylko, gdy n > 0.
  - Optymistyczne odhaczanie: klasa `line-through` dodawana natychmiast na `htmx:beforeRequest`, cofana na `htmx:responseError`.
  - Wskaźnik połączenia w nagłówku (z Task 12), toasty z auto-zamknięciem po 4 s.
  - Na telefonie dolny pasek z „New category” i przełącznikiem boardów; `viewport-fit=cover` i `env(safe-area-inset-bottom)`.
  - `manifest.webmanifest` + ikony (instalacja na ekranie głównym; bez service workera/offline).
  - Dostępność: etykiety `aria-label` na przyciskach-ikonach, fokus wraca na pole „Add item” po dodaniu, modal zamykany `Esc` z pułapką fokusu.

- [ ] **Step 1: Failing testy e2e**

```python
@pytest.mark.e2e
@pytest.mark.parametrize("width", [360, 768, 1280])
def test_no_horizontal_scroll(logged_in_page_with_seed, width):
    page.set_viewport_size({"width": width, "height": 800})
    assert page.evaluate("document.scrollingElement.scrollWidth <= window.innerWidth")
@pytest.mark.e2e
def test_touch_targets_at_least_44px_on_phone(...): ...      # checkboxy, przyciski menu, „Add” — bounding_box ≥ 44×44
@pytest.mark.e2e
def test_desktop_shows_three_columns(...): ...               # 1280 px: ≥3 różne wartości x dla kart kategorii
@pytest.mark.e2e
def test_offline_device_resyncs_after_reconnect(two_logged_in_pages):   # Review Focus 3
    a, b = two_logged_in_pages
    b.context.set_offline(True); add_item(a, "Bread"); b.context.set_offline(False)
    b.dispatch_event("body", "visibilitychange")  # symulacja powrotu
    expect(b.get_by_text("Bread")).to_be_visible(timeout=5000)
@pytest.mark.e2e
def test_optimistic_toggle_reverts_on_error(...): ...        # route("**/toggle", abort) ⇒ klasa zdjęta, toast widoczny
@pytest.mark.e2e
def test_open_modal_is_not_clobbered_by_remote_update(...): ...  # B edytuje w modalu; A zmienia inną pozycję; modal B zostaje z wpisanym tekstem
```
- [ ] **Step 2: Run** `uv run pytest -m e2e -v` → FAIL
- [ ] **Step 3: Zaimplementuj** elementy z listy
- [ ] **Step 4: Run** `make css && uv run pytest -v && uv run pytest -m e2e -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(ui): responsive polish, dark mode, optimistic toggles, PWA manifest"`

### Task 18: Import danych z v1

**Files:**
- Create: `backend/src/easytodo/legacy_import.py`
- Modify: `backend/src/easytodo/cli.py` (podkomenda `import-legacy`)
- Test: `backend/tests/test_legacy_import.py`

**Interfaces:**
- Consumes: Task 3 modele; Task 7 `create_board`.
- Produces:
  - `@dataclass class ImportReport: categories: int; items: int; renamed_categories: list[str]; orphan_items: int`.
  - `async def import_legacy(db: AsyncSession, legacy: asyncpg.Connection, *, schema: str, owner: User, board_name: str = "Imported from v1") -> ImportReport` — czyta `{schema}.categories` (po `position, id`) i `{schema}.tasks` złączone przez `{schema}.task_assignment` (dla zduplikowanych przypisań wygrywa najnowsze `assigned_at`); tworzy **nowy** board ownera; zachowuje `name`, `description`, `is_done`, `created_at`, `updated_at` (`timestamp` bez strefy traktowany jako czas lokalny serwera v1 — parametr `--legacy-tz`, domyślnie `Europe/Warsaw`); zadania bez przypisania trafiają do kategorii „Uncategorized”; nazwy przycięte do limitów z Task 8 (obcięte trafiają do `renamed_categories`); wszystko w jednej transakcji (błąd ⇒ nic nie zapisane). `schema` walidowany regexem `^[a-z_][a-z0-9_]*$`, w SQL wstawiany jako identyfikator w cudzysłowie.
  - CLI: `python -m easytodo.cli import-legacy --legacy-dsn postgresql://user:pass@host:5432/todo_grocery --schema prod --owner-email you@example.com [--board-name …] [--legacy-tz …]`; drukuje raport.

- [ ] **Step 1: Failing testy** (fixture tworzy w bazie testowej schemat `legacy_t` z tabelami `categories`, `tasks`, `task_assignment` o kolumnach jak w `legacy/v1/ddl/3_3-ddl_prod.sql`, bez triggerów)

```python
async def test_imports_categories_in_order_with_items(db, legacy_conn, owner): ...
    # 2 kategorie (position 2,1), 3 zadania (1 done, 1 z opisem) ⇒ board z kategoriami w kolejności position, is_done/description zachowane
async def test_orphan_tasks_go_to_uncategorized(db, legacy_conn, owner): ...
async def test_import_is_atomic(db, legacy_conn, owner, monkeypatch): ...  # wyjątek w połowie ⇒ 0 nowych boardów
async def test_invalid_schema_name_rejected(db, legacy_conn, owner): ...   # "prod; drop table x" ⇒ ValueError
async def test_naive_timestamps_converted_from_legacy_tz(db, legacy_conn, owner): ...
```
- [ ] **Step 2: Run** `uv run pytest tests/test_legacy_import.py -v` → FAIL
- [ ] **Step 3: Zaimplementuj** `import_legacy` i podkomendę CLI
- [ ] **Step 4: Run** `uv run pytest -v` → PASS
- [ ] **Step 5: Commit** `git commit -m "feat(migration): import v1 categories and tasks into a v2 board"`

### Task 19: Porównanie wydajności, README, dokumentacja migracji, sprzątanie

**Files:**
- Modify: `README.md` (pełna przebudowa), `docs/performance.md`
- Create: `docs/migration-from-v1.md`
- Delete: `legacy/v1/` (kod zostaje w tagu `legacy-v1`)

**Interfaces:**
- Consumes: Task 2 `scripts/bench.py`, Task 18 CLI.
- Produces:
  - `docs/performance.md`: tabela v1 vs v2 (p50/p95 dla `view_board`, `add_item`, `toggle_item`) na tej samej maszynie i seedzie, plus czas od zmiany do zdarzenia WS u drugiego klienta (p95, 100 prób).
  - `README.md`: opis, funkcje, zrzut ekranu (telefon + desktop), szybki start (`cp .env.example .env`, wygenerowanie `EASYTODO_SECRET_KEY`, `docker compose up -d`, `create-admin`), konfiguracja (tabela wszystkich `EASYTODO_*` z domyślnymi wartościami), HTTPS (link do `docs/deployment.md`), SMTP, rozwój lokalny (`make test-db`, `uv sync`, `make css`, `make run`, `make test`, `make e2e`), migracje (`uv run alembic revision --autogenerate -m …`, zasada „każda zmiana modelu = migracja”, automatyczne `upgrade head` przy starcie kontenera), architektura w 5 zdaniach (z sekcji „Decyzje architektoniczne” tego planu), backup/restore.
  - `docs/migration-from-v1.md`: krok po kroku — backup bazy v1 (`pg_dump`), uruchomienie v2 obok (inna baza/kontener), założenie konta, `import-legacy` z DSN bazy v1, weryfikacja liczby pozycji (zapytanie `SELECT count(*) FROM <schema>.tasks` vs raport), wyłączenie v1; różnice: brak `/v1/*` JSON API, konfiguracja przez env zamiast `config.ini`, rola `setup.py`/`setup_docker.py` przejęta przez docker-compose.

- [ ] **Step 1: Zmierz v2**

Run: `docker compose up -d --build --wait && docker compose exec app python -m easytodo.cli create-admin --email bench@example.com --password-stdin <<< "bench-password-123" && uv run python ../scripts/bench.py --target v2 --base-url http://localhost:8000 --email bench@example.com --password bench-password-123`
Expected: każdy p95 v2 ≤ p95 v1 z `docs/performance.md`. Jeśli nie — **stop**: zdiagnozuj (`EXPLAIN ANALYZE` zapytań z `get_board_view`, profil `py-spy`) i popraw przed dalszymi krokami; nie łagodź kryterium.

- [ ] **Step 2: Zapisz wyniki** w `docs/performance.md`
- [ ] **Step 3: Przećwicz migrację** z bazy v1 z Task 2 (ponownie uruchom legacy compose z wolumenem) zgodnie z `docs/migration-from-v1.md`; liczby z raportu zgadzają się z `count(*)` w v1
- [ ] **Step 4: Napisz README** i dokument migracji; usuń `legacy/v1/` (`git rm -r legacy/v1`) i usuń linię o legacy z README
- [ ] **Step 5: Pełna weryfikacja**

Run: `cd backend && uv run ruff check && uv run alembic check && uv run pytest && uv run pytest -m e2e && cd .. && docker compose down -v && docker compose up -d --build --wait && curl -fsS localhost:8000/healthz`
Expected: wszystko zielone, `{"status":"ok","db":"ok"}`

- [ ] **Step 6: Commit** `git commit -m "docs: README, v1 migration guide and performance comparison; remove legacy code"`

---

## Mapowanie na kryteria akceptacji ze specyfikacji

| Kryterium | Task(i) |
|---|---|
| Rejestracja, logowanie, 2FA, reset hasła | 5, 6, 13, 14 |
| Własne boardy, izolacja danych | 7, 8 (`test_other_users_ids_are_404_everywhere`) |
| Udostępnianie z rolą (e-mail / link) | 10 |
| Zmiany w czasie rzeczywistym | 12, 17 |
| GUI responsywne | 9, 17 |
| Panel admina (lista, usuwanie, reset, konto z hasłem tymczasowym, statystyki) | 15 |
| Schemat zarządzany migracjami (start + CI) | 1, 3, 11 |
| Start przez docker-compose (+ opcjonalny reverse proxy z HTTPS) | 11, 16 |
| Wydajność nie gorsza | 2, 7 (2 zapytania na widok), 19 |
| README i dokumentacja migracji z v1 | 18, 19 |
| CSRF / XSS / SQL injection | 4 (CSRF + CSP), 8–9 (escaping, brak inline JS), ORM + parametryzowany SQL wszędzie, 18 (identyfikator schematu) |
| Sesje unieważnialne | 5, 6, 12 (`sessions.revoked`), 14, 15 |
