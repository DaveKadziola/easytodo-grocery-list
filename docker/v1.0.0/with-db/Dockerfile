# Build stage for app dependencies to reduce image size and avoid store temp files
FROM python:3.12-alpine as builder

WORKDIR /build

RUN apk add --no-cache \
    postgresql-dev \
    build-base \
    python3-dev

RUN python -m venv /opt/easytodo
ENV PATH="/opt/easytodo/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# DB setup at first container run
FROM postgres:17-alpine
COPY ./pg_hba.conf /tmp/pg_hba.conf
COPY ./copy-config.sh /docker-entrypoint-initdb.d/10-copy-config.sh

RUN chmod +x /docker-entrypoint-initdb.d/*.sh && \
    chown -R postgres:postgres /docker-entrypoint-initdb.d/


# "Main" stage to collect app sources & config & run
FROM python:3.12-alpine
WORKDIR /main

RUN apk add --no-cache \
    postgresql-libs \
    libstdc++

COPY --from=builder /opt/easytodo /opt/easytodo
ENV PATH="/opt/easytodo/bin:$PATH"

COPY ./run.py .
COPY ./app/ ./app/
COPY ./docs/ ./docs/
COPY ./docker/v1.0.0/entrypoint.sh /entrypoint.sh

RUN addgroup -S todogrocery && \
    adduser -S -G todogrocery todogrocery && \
    chown -R todogrocery:todogrocery /main && \
    chmod +x /entrypoint.sh

USER todogrocery

ENTRYPOINT ["/entrypoint.sh"]
