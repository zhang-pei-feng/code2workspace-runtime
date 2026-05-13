---
name: benchmark-workflow-orchestrator
description: Orchestrate local workflow reuse, batch benchmark execution, result collection, and benchmark summarization for requests about workflow comparison, benchmark runs, multi-workflow reuse, template filling, and result download. Use when the user asks to compare multiple workflows, benchmark assembly tools, reuse existing local workflows on the same input, or obtain benchmark summaries without calling legacy ACP or DeepAgents agents.
---

# Benchmark Workflow Orchestrator

Use this skill when the user wants a benchmark or multi-workflow reuse flow,
especially for local workflow and result directories.

## Always do

1. Work entirely inside the current `code2workspace` repository.
2. Never call old runtime entrypoints under `/mnt/data1/zhangpf/superagent/...`.
3. Keep workflow execution local; do not assume any remote workflow service.
4. Save outputs under `results/skills/benchmark-workflow-orchestrator/...`.
5. Before expanding scope with extra reruns or extra tools, write or update a
   user-facing benchmark report summarizing completed cases, artifacts, shared
   datasets, and blockers.

## Entry points

- Skill front-door:
  `python3 skills/orchestration/benchmark-workflow-orchestrator/scripts/benchmark_workflow.py --help`

## Workflow

1. If the user asks for benchmarking, workflow comparison, or reusing multiple
   workflows on the same input, first inspect the local run directory, inputs,
   and deterministic helper scripts already present in the repository.
2. If the environment is not ready, do not fake execution. Produce a structured
   plan/result stub and clearly say what is missing.
3. For local summary-only tasks, run
   `python3 skills/orchestration/benchmark-workflow-orchestrator/scripts/benchmark_workflow.py summarize ...`
4. When the result directory already exists, summarize real result files instead
   of inventing benchmark metrics.
5. When some cases succeed and others are still pending, update the report with
   the partial benchmark state before continuing.

## Output rules

- Mention the output directory first.
- If the run is only partially executable, state exactly which prerequisites are
  missing.
- If real local execution happened, surface real case directories, workflow
  names, commands, and artifact paths only.
- Never claim benchmark completion without real artifact paths.
