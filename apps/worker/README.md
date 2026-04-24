# jp-adopt-worker

ARQ worker that drains the transactional outbox and POSTs signed payloads to `INTEGRATION_WEBHOOK_URL`.

```bash
cd apps/worker
uv sync --extra dev
uv run jp-adopt-worker
# or: uv run arq jp_adopt_worker.worker_settings.ArqWorkerSettings
```

Requires Postgres (migrated), Redis, and env vars from root `.env.example`.
