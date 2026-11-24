.PHONY: run test lint check db

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
