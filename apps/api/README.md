# jp-adopt-api

See [docs/runbooks/spike-local-dev.md](../../docs/runbooks/spike-local-dev.md) for the full stack. Summarized:

- `uv run uvicorn jp_adopt_api.main:app --reload --host 0.0.0.0 --port 8000`
- `uv run alembic upgrade head` (set `DATABASE_URL` / use `.env` from repo root)
- Export OpenAPI: `uv run python -m jp_adopt_api.scripts.export_openapi` → `apps/api/openapi.json` (drives `packages/contracts`).

Run from repo root or this directory with `uv` (see root runbook).

```bash
cd apps/api
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn jp_adopt_api.main:app --reload --host 0.0.0.0 --port 8000
```

OpenAPI: `http://localhost:8000/openapi.json` (export for contracts: `uv run python -m jp_adopt_api.scripts.export_openapi`).
