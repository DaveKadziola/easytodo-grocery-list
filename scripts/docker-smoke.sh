#!/bin/sh
# Smoke test of a running docker compose stack (CI runs it after `docker compose up -d --wait`).
# From the repository root:
#
#     sh scripts/docker-smoke.sh              # the app at http://localhost:8000
#     BASE_URL=http://localhost:8001 sh scripts/docker-smoke.sh
#
# BASE_URL must be the app's EASYTODO_PUBLIC_URL (the CSRF check compares the Origin header).
# It checks that the app answers, that the migrations ran once and are at head, and that a file
# attached to a task is served back unchanged, also after the app container is recreated (the
# `attachments` volume keeps it). It needs a stack you can throw away: it creates the account
# smoke@example.com and a board "smoke <pid>".
#
# A step that fails prints `smoke: <step> failed` to stderr and exits with 1; the end is
# `smoke: ok`.
set -eu

BASE_URL=${BASE_URL:-http://localhost:8000}
EMAIL=smoke@example.com
PASSWORD=smoke-password-123
ATTACHMENTS_DIR=/app/backend/data/attachments

step=start
WORK=$(mktemp -d)
JAR=$WORK/cookies
CSRF=
cleanup() {
    status=$?
    rm -rf "$WORK"
    if [ "$status" -ne 0 ]; then
        echo "smoke: $step failed" >&2
        exit 1
    fi
}
trap cleanup EXIT

# The request of a signed-in browser: its cookies, the Origin and the CSRF token of the page.
api() {
    curl -fsS -b "$JAR" -c "$JAR" -H "Origin: $BASE_URL" -H "X-CSRF-Token: $CSRF" "$@"
}

# The first number after "<prefix>" in what is read from standard input; fails if there is none.
number_after() {
    found=$(grep -o "$1[0-9][0-9]*" | head -n 1 | grep -o '[0-9][0-9]*')
    test -n "$found"
    echo "$found"
}

# The migrations that ran, counted in the app's log.
migrations_run() {
    docker compose logs app | grep -c "Running upgrade" || true
}

step="health check"
curl -fsS "$BASE_URL/healthz" >/dev/null
step="sign-in page"
curl -fsS -o /dev/null "$BASE_URL/login"
step="stylesheet"
curl -fsS -o /dev/null "$BASE_URL/static/css/app.css"

step="database at head"
docker compose exec -T app alembic current | grep -F "(head)" >/dev/null
step="migrations ran"
before=$(migrations_run)
test "$before" -ge 1
step="restart"
docker compose restart app
docker compose up -d --wait
step="migrations not repeated after a restart"
test "$(migrations_run)" -eq "$before"

step="attachments volume writable"
docker compose exec -T app test -w "$ATTACHMENTS_DIR"

step="create account"
printf '%s\n' "$PASSWORD" |
    docker compose exec -T app python -m easytodo.cli create-admin --email "$EMAIL" --password-stdin

step="sign in"
curl -fsS -c "$JAR" -o /dev/null "$BASE_URL/login"
csrf_cookie() { awk '$6 == "easytodo_csrf" { print $7 }' "$JAR"; }
CSRF=$(csrf_cookie)
test -n "$CSRF"
login_status=$(curl -sS -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' -H "Origin: $BASE_URL" \
    --data-urlencode "email=$EMAIL" --data-urlencode "password=$PASSWORD" \
    --data-urlencode "csrf_token=$CSRF" "$BASE_URL/login")
test "$login_status" -eq 303
CSRF=$(csrf_cookie)  # what the browser sends from now on
test -n "$CSRF"

step="create board"
board_url=$(api -o /dev/null -w '%{redirect_url}' --data-urlencode "name=smoke $$" "$BASE_URL/boards")
board_id=$(echo "$board_url" | number_after '/boards/')
step="add category"
category_id=$(api --data-urlencode "name=Dairy" "$BASE_URL/boards/$board_id/categories" |
    number_after 'id="category-')
step="add task"
item_id=$(api --data-urlencode "name=Milk" "$BASE_URL/categories/$category_id/items" |
    number_after 'id="item-')

step="upload attachment"
printf 'EasyToDo smoke test %s\n' "$$" >"$WORK/note.txt"
attachment_id=$(api -F "file=@$WORK/note.txt;type=text/plain" "$BASE_URL/items/$item_id/attachments" |
    number_after 'data-attachment-id="')
step="download attachment"
api -o "$WORK/downloaded.txt" "$BASE_URL/attachments/$attachment_id/file"
cmp "$WORK/note.txt" "$WORK/downloaded.txt"

step="recreate the app container"
docker compose up -d --force-recreate --wait app
step="attachment after recreating the app container"
api -o "$WORK/again.txt" "$BASE_URL/attachments/$attachment_id/file"
cmp "$WORK/note.txt" "$WORK/again.txt"

step="finish"
echo "smoke: ok"
