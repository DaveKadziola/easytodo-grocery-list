FROM python:3.12-slim
WORKDIR /main

RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt .
COPY ./run.py .
COPY ./app/ ./app/
COPY ./docs/ ./docs/

RUN pip install --no-cache-dir -r requirements.txt

COPY /docker/v1.0.0/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]