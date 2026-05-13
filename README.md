# code2workspace-runtime

Minimal runtime export for `code2workspace`.

This repository keeps the code needed to run the supervisor-first
Code2Workspace agent and the lightweight web API backend. It intentionally does
not include thesis experiment outputs, harness runs, benchmark result
directories, generated workspaces, virtual environments, or local databases.

## Included

- `libs/code2workspace` - core LangGraph/LangChain runtime and orchestration
- `libs/cli` - interactive CLI and non-interactive runner
- `apps/webapp` - small API backend that reuses the CLI runtime path
- `.code2workspace/skills` - project skills and supervisor guidance used by the
  runtime
- `backend/config/agent_models.json` - project-local model configuration
  entrypoint
- `docs/overview` - current runtime architecture and status notes

## Not Included

- `experiments/`
- `results/`
- `workspace/`
- `.workspaces/`
- `orchestration_runs/`
- local virtual environments, caches, screenshots, and SQLite run indexes

## Run

Copy `.env.example` to `.env` and fill the provider settings you want to use.

```bash
uv run --project libs/cli code2workspace
```

Single non-interactive task:

```bash
uv run --project libs/cli code2workspace -n "Reply with OK only." -q
```

Web API backend:

```bash
uv run --project libs/cli python -m uvicorn apps.webapp.api:app --app-dir . --reload
```

