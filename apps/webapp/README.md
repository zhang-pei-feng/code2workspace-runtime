# Web Workbench

`apps/webapp` now contains:

- a Starlette backend under `apps/webapp/api.py`
- a vendored `langchain-ai/agent-chat-ui` Next.js frontend under
  `apps/webapp/frontend/`

## Current scope

- browser chat UI backed by the shared local LangGraph server
- same-origin `/langgraph/*` proxy route exposed by the Python backend
- default assistant ID `agent`
- static frontend export served by the backend for `/`

## Runtime model

- On backend startup, `apps/webapp` starts one shared local LangGraph server
  using the existing `libs/cli` server bridge.
- The backend proxies `agent-chat-ui` requests from `/langgraph/*` to that
  shared server, so the browser does not need to know the dynamic upstream
  port.
- The vendored frontend defaults to `/langgraph` and `assistant_id=agent`, so
  the setup form is skipped for the local-development baseline.
- The older thread-first `/api/*` routes are still present, but the current
  frontend no longer uses the custom `Sessions / Workspace / Runs / Maintenance`
  dashboard.

## Run locally

Backend:

```bash
cd /mnt/data1/zhangpf/code2workspace
uv run --project libs/cli python -m uvicorn apps.webapp.api:app --app-dir . --reload
```

Frontend dev server:

```bash
cd /mnt/data1/zhangpf/code2workspace/apps/webapp/frontend
npm install
npm run dev
```

The Next.js dev server runs independently; the backend still exposes the
LangGraph proxy and static export entrypoint on its own port.

## Build frontend for backend serving

```bash
cd /mnt/data1/zhangpf/code2workspace/apps/webapp/frontend
npm run build
```

When `frontend/out/` exists, the backend serves the exported app for non-`/api/*`
and non-`/langgraph/*` routes.

## Test frontend

```bash
cd /mnt/data1/zhangpf/code2workspace/apps/webapp/frontend
npm run verify
npm run test:e2e
```

Notes:

- chat submissions now use `messages + updates + values` streaming through the
  backend `/langgraph/*` proxy instead of `values`-only mode
- the Playwright smoke suite reuses a local browser executable before trying
  any CDN-managed browser install
- browser lookup order is:
  - `PLAYWRIGHT_EXECUTABLE_PATH`
  - cached Chromium under `~/.cache/ms-playwright/chromium-1208/...`
  - cached headless shell under `~/.cache/ms-playwright/chromium_headless_shell-1208/...`
  - older cached Chromium under `~/.cache/ms-playwright/chromium-1181/...`
- if none of those paths exist, `npm run test:e2e` fails early and tells you
  to set `PLAYWRIGHT_EXECUTABLE_PATH`

## Current limitations

- the current frontend is chat-first; the earlier custom workspace/run
  dashboard is gone
- auth is intentionally absent in the local-development baseline
- the shared LangGraph server is local-development oriented and not production
  hardened

## Suggested test prompts

For realistic user-facing web tests, use the curated prompt bank in
`docs/overview/web-test-question-bank.md`.
It groups prompts into:

- high-success quick checks
- more realistic cross-source analysis questions
- long-form report prompts
- higher-cost demo-only tasks that should not be part of first-pass smoke tests
