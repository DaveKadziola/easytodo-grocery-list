.PHONY: test-db test e2e css run lint

test-db:
	docker compose -f compose.test.yml up -d --wait

test: test-db
	cd backend && uv run pytest

e2e: test-db
	cd backend && uv run pytest -m e2e

css:
	./bin/tailwindcss -i frontend/styles/input.css -o frontend/static/css/app.css --minify

run:
	cd backend && uv run uvicorn --factory easytodo.main:create_app --reload

lint:
	cd backend && uv run ruff check
