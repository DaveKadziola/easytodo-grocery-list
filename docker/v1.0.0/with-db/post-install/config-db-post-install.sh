#!/bin/sh
set -e

cp /tmp/pg_hba.conf "$PGDATA/pg_hba.conf"
chmod 600 "$PGDATA/pg_hba.conf"
chown postgres:postgres "$PGDATA/pg_hba.conf"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';
EOSQL