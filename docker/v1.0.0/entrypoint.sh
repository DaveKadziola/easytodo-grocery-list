#!/bin/sh

# config.ini
cat > /main/config.ini <<EOF
[database]
host = ${DB_HOST}
port = ${DB_PORT}
dbname = ${DB_NAME}
user = ${DB_USER}
password = ${DB_PASSWORD}

[schema]
name = ${DB_SCHEMA}

[host]
name = ${APP_HOST_NAME}
port = ${APP_HOST_PORT}
EOF

# socketio.json
cat > /main/app/static/js/socketio.json <<EOF
{
  "host": {
    "name": "${SOCKETIO_HOST}",
    "port": "${SOCKETIO_PORT}"
  }
}
EOF

# start app
exec python3 run.py
