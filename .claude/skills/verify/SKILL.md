---
name: verify
description: Build/launch/drive recipe for runtime-verifying backend changes in this repo against the live local Docker stack.
---

# Verifying backend changes at runtime

A full deployment of this platform runs locally in Docker: `atom_postgres`
(pgvector, host port **17433**, user `atom_user`), `atom_redis` (host port
**16380**), `atom_backend` (prebuilt image on 18000 — does NOT have
working-tree changes). The postgres and redis passwords come from your local
`.env` / `POSTGRES_PASSWORD` + `REDIS_PASSWORD` environment (compose refuses
to start without them) — read them from there; they are never in the tree.
Verify working-tree code by booting a second backend against an isolated
scratch DB on the same postgres.

## Recipe

1. **Venv**: `python3 -m venv <scratch>/testenv && pip install -r backend/requirements.txt`
   (no torch — installs in a few minutes; Python 3.12 matches the image).
2. **Scratch DB** (never write to the live `atom_cm_db`):
   ```
   docker exec atom_postgres psql -U atom_user -d atom_cm_db -c "CREATE DATABASE verify_scratch OWNER atom_user;"
   docker exec atom_postgres psql -U atom_user -d verify_scratch -c "CREATE EXTENSION IF NOT EXISTS vector;"
   ```
3. **Migrate**: from `backend/`, `python -m alembic upgrade head` with
   `DATABASE_URL=postgresql://atom_user:${POSTGRES_PASSWORD}@127.0.0.1:17433/verify_scratch`
   and any `SECRET_KEY`.
4. **Admin auth**: seed a user (`User(username=…, email=…, password_hash=hash_password(…), role=UserRole.ADMIN)`)
   and mint a token with `create_access_token(user_id, amr="pwd+mfa")` —
   admin routes REQUIRE the `pwd+mfa` amr claim (`require_admin`, deps.py).
   Pass it as `Authorization: Bearer`.
5. **Launch**:
   ```
   SECRET_KEY=… DATABASE_URL=…verify_scratch REDIS_URL=redis://:${REDIS_PASSWORD}@127.0.0.1:16380/5 \
   AUTO_INGEST_KNOWLEDGE_BASE_ON_STARTUP=false EXCEL_ENGINE_ENABLED=false LLM_PROVIDER=ollama \
   python -m uvicorn app.main:app --host 127.0.0.1 --port 18999
   ```
   Gotcha: with the excel engine ENABLED, startup mkdirs `/app/...` (container
   path) and dies on bare metal — disable it unless you fixed that path.
6. **Drive**: REST under `http://127.0.0.1:18999/api/...`
   (e.g. `/api/admin/code-indexing/repos`, `/api/jobs/{id}`).

## Useful tricks

- **Hang a GitLab fetch** (to hold an indexing job in RUNNING): point
  `GITLAB_URL` at a local tarpit — a socket that accepts and never responds.
  python-gitlab has no timeout, so the job hangs until you kill the tarpit
  (then it fails with connection-reset, exercising the fail path).
- **Pre-fix vs post-fix contrast**: `git worktree add <scratch>/pristine HEAD`,
  run the same probe with `PYTHONPATH=<worktree>/backend` vs the real backend.
- **Engine renderer without an LLM**: build a `WorkbookPlan`/`SheetSpec`
  (pydantic, `app.excel_testcase_engine.schemas.workbook_plan`) and call
  `excel_writer.render(plan, path)` — the exact `node_render` call — then
  `load_workbook` the output.
- Tests need only `SECRET_KEY` and `DATABASE_URL` env vars (any values) to
  import settings: `SECRET_KEY=t DATABASE_URL=postgresql://x:x@localhost/x python -m pytest tests/... -q`.

## Not reachable in this environment

Anything needing the AiNxt/Anthropic LLM gateway or a full agentic run
(celery worker + workspace clone + LLM): the tool-loop retry paths in
`core/llm.py`, the budget guard firing mid-run, `drive_run` phases. Verify
those by log-watching a real deployment after rollout.
