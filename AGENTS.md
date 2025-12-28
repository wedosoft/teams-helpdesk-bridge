# Repository Guidelines

## Project Structure & Module Organization
The backend lives in `app/` (FastAPI). Key areas: `app/adapters/` for platform integrations (Freshdesk/Zendesk/Salesforce/Freshchat), `app/teams/` for bot handlers, `app/admin/` for admin/OAuth flows, `app/core/` for routing/orchestration, and `app/static/` for the Teams tab HTML. Operational docs and runbooks are under `docs/`, Supabase schema migrations are in `supabase/migrations/`, and Teams app packages/assets live in `teams-app/`. Root-level infra files include `Dockerfile`, `requirements.txt`, and `fly.toml`.

## Build, Test, and Development Commands
- `python3 -m venv venv` and `source venv/bin/activate` to set up a local venv.
- `python3 -m pip install -r requirements.txt` to install dependencies.
- `uvicorn app.main:app --host 0.0.0.0 --port 8000` to run the API locally.
- `fly deploy` to deploy to Fly.io; `fly logs -a teams-helpdesk-bridge` for runtime logs.
- `docker build -t teams-helpdesk-bridge .` and `docker run -p 3978:3978 teams-helpdesk-bridge` for containerized runs (port 3978).

## Coding Style & Naming Conventions
Use 4-space indentation and PEP 8-friendly formatting with type hints and short docstrings, matching existing modules. Keep FastAPI routers in `routes.py` within each adapter/admin/teams package, and isolate external API logic in `client.py`-style helpers. Use `snake_case` for functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.

## Testing Guidelines
There is no automated test suite configured in this repo (no `tests/` directory or test runner in `requirements.txt`). Validate changes by running the server and exercising endpoints; the curl-based runbook in `docs/posco/posco-poc-runbook.md` is the quickest reference. If you add tests, introduce a `tests/` folder with `test_*.py` naming and document the runner and command in this file.

## Commit & Pull Request Guidelines
Recent commits use a Conventional Commits-style prefix (e.g., `feat:`, `fix:`, `refactor:`) with a short, imperative summary. Keep commits focused and update `docs/` or `.env.example` when behavior or configuration changes. PRs should include a brief summary, relevant runbook/issue links, and screenshots for Teams tab or manifest asset changes in `app/static/` or `teams-app/`.

## Security & Configuration Tips
Never commit secrets; use `.env`/`.env.local` for local values and keep `.env.example` current. `ENCRYPTION_KEY` is required for tenant config encryption; changing it requires re-saving tenant settings. Supabase uses `SUPABASE_URL` and `SUPABASE_SECRET_KEY`, and tenant configs are stored encrypted in Supabase.
