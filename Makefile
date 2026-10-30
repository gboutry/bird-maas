.PHONY: sync lint format test native pack schemas
sync:
	uv sync --locked --python 3.12
lint:
	uv run --locked ruff check shared/src charms tests scripts
	uv run --locked ruff format --check shared/src charms tests scripts
format:
	uv run ruff check --fix shared/src charms tests scripts
	uv run ruff format shared/src charms tests scripts
test:
	uv run --locked pytest --cov=maas_bird --cov-report=term-missing tests/unit
native:
	uv run --locked pytest tests/native
pack:
	uv run --locked python scripts/pack.py
schemas:
	uv run --locked python scripts/export_schemas.py
