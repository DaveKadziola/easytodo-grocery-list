.PHONY: test-db test e2e css run lint

# Tailwind standalone CLI (no Node.js), fetched once into ./bin (git-ignored) for this platform.
# The version and the linux sha256 pins are also in deploy/Dockerfile (backend/tests/test_deploy_files.py checks that they match).
TAILWIND_VERSION := v4.1.13
TAILWIND_OS := $(if $(filter Darwin,$(shell uname -s)),macos,linux)
TAILWIND_ARCH := $(if $(filter arm64 aarch64,$(shell uname -m)),arm64,x64)
TAILWIND_BIN := bin/tailwindcss-$(TAILWIND_VERSION)
TAILWIND_URL := https://github.com/tailwindlabs/tailwindcss/releases/download/$(TAILWIND_VERSION)/tailwindcss-$(TAILWIND_OS)-$(TAILWIND_ARCH)
# sha256 of each release asset (the release's sha256sums.txt); a download that differs is deleted.
TAILWIND_SHA256_linux-x64 := b9ed9f8f640d3323711f9f68608aa266dff3adbc42e867c38ea2d009b973be11
TAILWIND_SHA256_linux-arm64 := c90529475a398adbf3315898721c0f9fe85f434a2b3ea3eafada68867641819a
TAILWIND_SHA256_macos-x64 := c3b230bdbfaa46c94cad8db44da1f82773f10bac54f56fa196c8977d819c09e4
TAILWIND_SHA256_macos-arm64 := c47681e9948db20026a913a4aca4ee0269b4c0d4ef3f71343cb891dfdc1e97c9
TAILWIND_SHA256 := $(TAILWIND_SHA256_$(TAILWIND_OS)-$(TAILWIND_ARCH))
SHA256_CHECK := $(if $(filter Darwin,$(shell uname -s)),shasum -a 256 -c,sha256sum -c)

test-db:
	docker compose -f compose.test.yml up -d --wait

test: test-db css
	cd backend && uv run pytest

e2e: test-db css
	cd backend && uv run pytest -m e2e

$(TAILWIND_BIN):
	mkdir -p bin
	curl -fsSL -o $@.download $(TAILWIND_URL)
	echo "$(TAILWIND_SHA256)  $@.download" | $(SHA256_CHECK) || { rm -f $@.download; exit 1; }
	chmod +x $@.download
	mv $@.download $@

css: $(TAILWIND_BIN)
	./$(TAILWIND_BIN) -i frontend/styles/input.css -o frontend/static/css/app.css --minify

run:
	cd backend && uv run uvicorn --factory easytodo.main:create_app --reload

lint:
	cd backend && uv run ruff check
