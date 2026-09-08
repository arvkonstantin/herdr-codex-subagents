.PHONY: setup lint test check

setup:
	uv sync --extra dev

lint:
	uv run ruff check .

test:
	uv run pytest --cov --cov-report=term-missing

check: lint test
