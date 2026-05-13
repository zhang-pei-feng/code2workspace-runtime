# Development guidelines for code2workspace

`code2workspace` is the implementation base for a graduation project about converting GitHub repositories into runnable workspaces with an agent built on LangGraph and LangChain.

## Repository structure

```txt
code2workspace/
├── apps/
│   └── webapp/          # minimal web API backend (frontend removed)
├── experiments/
│   ├── harness/         # harness-practice scaffolding
│   └── oneshot/         # generic one-shot repo-task runner
├── libs/
│   ├── code2workspace/  # SDK runtime
│   └── cli/         # terminal UI and non-interactive runner
└── README.md
```

## Working rules

- Read `docs/overview/session-handoff.md` first for the shortest handoff.
- Read `docs/overview/current-status.md` for the latest repository status, verified setup, and recent decisions.
- Read `docs/overview/roadmap.md` for the active implementation target.
- Read `docs/research/thesis-log.md` when work should stay aligned with thesis traceability.
- Keep changes focused on `libs/code2workspace` and `libs/cli`.
- Keep new work aligned with the current repo roadmap; the earlier checked-in web frontend has been removed unless the plan is intentionally changed again.
- Preserve current CLI and non-interactive behavior unless the task explicitly changes it.
- Basic interactive TUI startup is working again; when changing terminal behavior, still verify both the interactive TUI path and the non-interactive runner path explicitly.
- Prefer small, testable changes over broad refactors.
- Add or update tests when changing behavior.
- Avoid re-introducing removed modules such as examples, ACP, evals, REPL, or partner packages unless the project plan explicitly requires them.

## Useful commands

```bash
uv run --project libs/cli code2workspace
uv run --project libs/cli code2workspace -n "Reply with OK only." -q
uv run --project libs/cli --group test pytest
uv run --project libs/cli python -m uvicorn apps.webapp.api:app --app-dir . --reload
```
