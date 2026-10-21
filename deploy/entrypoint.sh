#!/bin/sh
# Check the settings, apply database migrations, then run the app. The database may still be
# starting, so the migration is retried (10 attempts, 2 s apart) before giving up; a setting that
# is missing or wrong is not something a retry fixes, so it is reported once, by name.
set -eu

if ! python - <<'PY'
import sys

from pydantic import ValidationError

from easytodo.config import Settings

try:
    Settings()
except ValidationError as exc:
    for error in exc.errors(include_url=False, include_input=False):  # no secret in the log
        name = "EASYTODO_" + "_".join(str(part) for part in error["loc"]).upper()
        print(f"entrypoint: {name}: {error['msg']}", file=sys.stderr)
    sys.exit(1)
PY
then
    echo "entrypoint: the settings in .env are not valid (see above); fix them, then run" \
        "docker compose up -d again" >&2
    exit 1
fi

attempt=1
until alembic upgrade head; do
    if [ "$attempt" -ge 10 ]; then
        echo "entrypoint: alembic upgrade head failed after $attempt attempts" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    echo "entrypoint: migration failed, retrying in 2 s (attempt $attempt/10)" >&2
    sleep 2
done

exec uvicorn --factory easytodo.main:create_app --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips "${EASYTODO_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    --ws-ping-interval 25 --ws-ping-timeout 20 \
    --workers "${EASYTODO_WORKERS:-1}"
