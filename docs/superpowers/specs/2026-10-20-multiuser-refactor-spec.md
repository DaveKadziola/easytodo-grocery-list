# EasyToDo & Grocery — specyfikacja przebudowy (wersja od Dave'a, 2026-10-20)

> Kopia wymagań przekazanych przy zleceniu planu. Plan implementacji:
> `docs/superpowers/plans/2026-10-20-easytodo-v2-multiuser.md`.

## 1. Cel nadrzędny

Przekształcić istniejącą aplikację Python (Flask + HTML, PostgreSQL) w nowoczesną, bezpieczną, wieloużytkownikową aplikację do zarządzania listami zakupów i prostymi todo, z:

* izolacją danych per użytkownik/board,
* logowaniem + 2FA,
* real-time synchronizacją,
* responsywnym GUI (mobile-first),
* panelem administratora,
* możliwością hostowania lokalnie z dostępem przez internet.

Aplikacja ma pozostać równie szybka jak obecnie (lub szybsza), a UX ma być wyraźnie lepszy.

## 2. Obecny stan (punkt wyjścia)

* Aplikacja napisana w Pythonie, uruchamiana standalone lub w kontenerze.
* Stack: Flask + HTML (prawdopodobnie szablony), PostgreSQL.
* Funkcjonalność: tworzenie kategorii + list zadań/zakupów.
* Brak izolacji użytkowników – wszyscy widzą te same dane.
* Brak logowania, brak real-time, brak admina, brak migracji schematu.

## 3. Docelowy stan (Definition of Done)

### 3.1 Architektura i stack

* Backend: Python (FastAPI lub Flask, preferowany FastAPI ze względu na async + WebSocket).
* Frontend: React (lub Next.js) – tylko jeśli przynosi realną wartość UX i nie pogarsza wydajności. Alternatywa: nowoczesny HTMX + Alpine.js / Tailwind, jeśli React okaże się overkilliem. Decyzja ma być uzasadniona w planie.
* Baza: PostgreSQL z pełnym wsparciem migracji (Alembic lub równoważne).
* Real-time: WebSocket (lub Server-Sent Events) – zmiana na jednym urządzeniu pojawia się natychmiast na innych (w obrębie tego samego boardu).
* Konteneryzacja: Docker + docker-compose (app + postgres + opcjonalnie reverse proxy).
* Aplikacja musi działać zarówno lokalnie, jak i być wystawiona na internet (z HTTPS).

### 3.2 Modele danych (do przebudowy)

* User – konto użytkownika (email, hasło hash, 2FA secret, status, role).
* Board – przestrzeń robocza (właściciel, nazwa, lista członków z rolami: owner / editor / viewer).
* Category – należy do Board.
* Task / ShoppingItem – należy do Category (lub bezpośrednio do Board).
* Wsparcie dla udostępniania boardów (invite po emailu lub linku).
* Pełne migracje schematu (Alembic) – każda zmiana modelu = nowa migracja.

### 3.3 Funkcjonalność użytkownika

1. Rejestracja konta (email + hasło).
2. Logowanie + opcjonalne 2FA (TOTP – Google Authenticator / Authy).
3. Reset hasła (email z tokenem).
4. Tworzenie / zarządzanie własnymi boardami.
5. Zapraszanie innych użytkowników do boardu (różne role).
6. W boardzie: kategorie + listy zadań/zakupów (CRUD).
7. Real-time synchronizacja – zmiany widoczne natychmiast na wszystkich urządzeniach zalogowanych do tego samego boardu.
8. Responsywne GUI – doskonale działa na telefonie, tablecie i desktopie.
9. Szybkość: czas odpowiedzi porównywalny lub lepszy niż obecna aplikacja.

### 3.4 Panel administratora

* Dostęp tylko dla użytkowników z rolą admin.
* Funkcje:
  * Lista wszystkich użytkowników.
  * Usuwanie konta.
  * Wysłanie resetu hasła.
  * Tworzenie nowego konta z domyślnym hasłem (i wymuszeniem zmiany przy pierwszym logowaniu).
  * Podstawowe statystyki (liczba użytkowników, boardów itp.).

### 3.5 Bezpieczeństwo i hosting

* Hasła hashowane (bcrypt / argon2).
* 2FA (TOTP).
* Ochrona przed CSRF, XSS, SQL injection.
* Możliwość bezpiecznego wystawienia lokalnej instancji na internet (np. przez reverse proxy + certyfikat).
* Sesje / JWT – bezpieczne, z możliwością unieważnienia.

### 3.6 Jakość kodu i utrzymanie

* Czysta struktura projektu (backend / frontend / shared).
* Testy jednostkowe i integracyjne kluczowych ścieżek (auth, board sharing, real-time).
* Dokumentacja uruchomienia (README + docker-compose).
* Migracje bazy automatycznie uruchamiane przy starcie / w CI.

## 4. Ograniczenia i priorytety

* Priorytet 1: Izolacja danych + logowanie + podstawowy CRUD boardów/kategorii/zadań.
* Priorytet 2: Real-time synchronizacja.
* Priorytet 3: 2FA + reset hasła + panel admina.
* Priorytet 4: Polerowanie UX / responsywność / ewentualna migracja na React.
* Nie wolno pogarszać wydajności względem obecnej wersji.
* Aplikacja ma pozostać hostowalna lokalnie (self-hosted first).

## 5. Sposób pracy

1. Najpierw przeanalizuj istniejący kod i zaproponuj plan migracji (architektura + decyzja React vs. lżejszy frontend).
2. Rozbij pracę na niezależne, testowalne etapy (sub-agenty / taski).
3. Każdy etap kończy się działającym, committowalnym stanem.
4. Po każdym większym etapie uruchamiaj testy i sprawdzaj, czy nic się nie zepsuło.
5. Na koniec dostarcz pełną dokumentację uruchomienia i migracji z starej wersji (jeśli dane mają być przeniesione).

## 6. Kryteria akceptacji (checklist)

* Użytkownik może się zarejestrować, zalogować, włączyć 2FA i zresetować hasło.
* Każdy użytkownik ma własne boardy; dane są izolowane.
* Board można udostępnić innemu użytkownikowi z określoną rolą.
* Zmiany w liście zadań pojawiają się w czasie rzeczywistym na innych urządzeniach.
* GUI jest w pełni responsywne (mobile + desktop).
* Istnieje panel admina z wymaganymi funkcjami.
* Schemat bazy jest zarządzany migracjami.
* Aplikacja startuje przez docker-compose.
* Wydajność nie jest gorsza niż obecna wersja.
* README zawiera jasne instrukcje instalacji i konfiguracji.
