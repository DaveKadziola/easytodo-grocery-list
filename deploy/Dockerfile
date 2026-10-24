# EasyToDo image. The build context is the repository root (see docker-compose.yml).
# Needs Docker 25 or newer (BuildKit 0.12+): ADD --checksum, RUN --mount.
#
# Third-party binaries are downloaded by BuildKit itself and verified against a pinned sha256,
# so a swapped release asset fails the build instead of running. Bumping a version means
# bumping its hashes (the release's sha256sums.txt / *.sha256 files) and, for Tailwind, the
# Makefile pins as well (backend/tests/test_deploy_files.py checks that they match). Supported
# platforms: linux/amd64 and linux/arm64.
ARG TAILWIND_VERSION=4.1.13
ARG TAILWIND_SHA256_AMD64=b9ed9f8f640d3323711f9f68608aa266dff3adbc42e867c38ea2d009b973be11
ARG TAILWIND_SHA256_ARM64=c90529475a398adbf3315898721c0f9fe85f434a2b3ea3eafada68867641819a
ARG UV_VERSION=0.11.32
ARG UV_SHA256_AMD64=aab924fd522efd06f1c5f3b93a243864fc453132c94b2dc49f1371b528a4b967
ARG UV_SHA256_ARM64=4d4fa08d95b06642e5800df6a22bd71455f23f988269e18da2847971d8c0bf31
ARG TARGETARCH

FROM scratch AS tools-amd64
ARG TAILWIND_VERSION
ARG TAILWIND_SHA256_AMD64
ARG UV_VERSION
ARG UV_SHA256_AMD64
ADD --checksum=sha256:${TAILWIND_SHA256_AMD64} --chmod=755 \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64 \
    /tailwindcss
ADD --checksum=sha256:${UV_SHA256_AMD64} \
    https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz \
    /uv.tar.gz

FROM scratch AS tools-arm64
ARG TAILWIND_VERSION
ARG TAILWIND_SHA256_ARM64
ARG UV_VERSION
ARG UV_SHA256_ARM64
ADD --checksum=sha256:${TAILWIND_SHA256_ARM64} --chmod=755 \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-arm64 \
    /tailwindcss
ADD --checksum=sha256:${UV_SHA256_ARM64} \
    https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz \
    /uv.tar.gz

FROM tools-${TARGETARCH} AS tools

# ---- Stage 1: Tailwind CSS (standalone CLI, no Node.js) -> frontend/static/css/app.css ----
FROM python:3.12-slim AS css
WORKDIR /build
COPY --from=tools /tailwindcss /usr/local/bin/tailwindcss
COPY frontend/ frontend/
RUN tailwindcss -i frontend/styles/input.css -o frontend/static/css/app.css --minify

# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/backend/.venv/bin:$PATH"

RUN --mount=type=bind,from=tools,source=/uv.tar.gz,target=/tmp/uv.tar.gz \
    tar -xzf /tmp/uv.tar.gz --strip-components=1 --wildcards -C /usr/local/bin '*/uv'

# backend/ and frontend/ stay siblings under /app: easytodo.web resolves FRONTEND_DIR relative to
# the (editable) source tree, so uv sync must install the project from /app/backend.
WORKDIR /app/backend
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
COPY frontend/ /app/frontend/
COPY --from=css /build/frontend/static/css/app.css /app/frontend/static/css/app.css
COPY --chmod=755 deploy/entrypoint.sh /app/entrypoint.sh

# The files attached to items (EASYTODO_ATTACHMENTS_DIR, relative to /app/backend): a volume in
# docker-compose.yml, which starts out with this directory's owner, so that the app can write there.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/backend/data/attachments \
    && chown app:app /app/backend/data/attachments
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
ENTRYPOINT ["/app/entrypoint.sh"]
