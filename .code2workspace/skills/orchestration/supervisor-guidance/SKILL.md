---
name: supervisor-guidance
description: Runtime-owned guidance assets for supervisor worker nodes. Stores task experience and execution strategy as prompt fragments instead of hardcoding them in Python logic.
---

# Supervisor Guidance

This skill package is not invoked manually by workers.

It exists so the supervisor runtime can load node guidance from versioned
project assets instead of embedding task-specific execution strategy in code.

Current reference assets live under:

- `references/nodes/register.md`
- `references/nodes/benchmark_case.md`
- `references/nodes/inspect.md`
- `references/nodes/init_report.md`
- `references/nodes/compose_report.md`
- `references/nodes/analyze_task.md`
- `references/nodes/execute_task.md`

Rule:

- keep these files focused on task experience and execution strategy
- keep capability-to-tool mappings in code
- avoid baking family-specific business logic into the runtime when a guidance
  asset can express it
