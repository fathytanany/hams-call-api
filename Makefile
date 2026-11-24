.PHONY: run test lint check db demo

db:
	docker compose up -d --wait db

run: db
	uv run uvicorn callapi.main:app --reload --port 8000

test: db
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

check: lint test

# Needs `docker compose up` running: the API dials the compose test app.
demo:
	uv run python scripts/demo.py --calls $(or $(CALLS),20)
