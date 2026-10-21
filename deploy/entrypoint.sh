#!/bin/sh
# Apply database migrations, then run the app. The database may still be starting, so the
# migration is retried (10 attempts, 2 s apart) before giving up.
set -eu

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
