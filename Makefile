.PHONY: run test lint check

run:
	uv run uvicorn callapi.main:app --reload --port 8000

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

check: lint test
