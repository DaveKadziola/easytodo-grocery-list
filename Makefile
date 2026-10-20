.PHONY: test-db test e2e css run lint

# Tailwind standalone CLI (no Node.js), fetched once into ./bin (git-ignored) for this platform.
TAILWIND_VERSION := v4.1.13
TAILWIND_OS := $(if $(filter Darwin,$(shell uname -s)),macos,linux)
TAILWIND_ARCH := $(if $(filter arm64 aarch64,$(shell uname -m)),arm64,x64)
TAILWIND_URL := https://github.com/tailwindlabs/tailwindcss/releases/download/$(TAILWIND_VERSION)/tailwindcss-$(TAILWIND_OS)-$(TAILWIND_ARCH)

test-db:
	docker compose -f compose.test.yml up -d --wait

test: test-db
	cd backend && uv run pytest

e2e: test-db
	cd backend && uv run pytest -m e2e

bin/tailwindcss:
	mkdir -p bin
	curl -fsSL -o $@.download $(TAILWIND_URL)
	chmod +x $@.download
	mv $@.download $@

css: bin/tailwindcss
	./bin/tailwindcss -i frontend/styles/input.css -o frontend/static/css/app.css --minify

run:
	cd backend && uv run uvicorn --factory easytodo.main:create_app --reload

lint:
	cd backend && uv run ruff check
