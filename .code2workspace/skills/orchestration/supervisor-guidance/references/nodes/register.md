# Register Node Guidance

- This node is registration-first: confirm benchmark assets, identify a shared dataset, choose multiple compatible tools/operators, and separate light missing assets from hard blockers.
- If `selected_tools` already exists in node metadata, treat it as a strong hint. If it is missing or empty, you must choose the tool subset yourself from the benchmark assets.
- If the user task names a concrete benchmark directory, inspect that directory directly before concluding that staged assets are missing.
- Produce a concrete readiness report rather than speculative execution.
- Scope the readiness judgment to the subset you actually selected for this task.
- Do not downgrade the node result because unrelated benchmark cases in the same root are broken or incomplete.
- Recommended bounded workflow for this node:
  - identify the benchmark root path from the original task
  - choose one shared dataset and compatible tools from the benchmark catalog, staged case manifests, or directly from the benchmark directory's WDL/input/case layout
  - run the deterministic helper:
    `python3 .code2workspace/skills/orchestration/benchmark-workflow-orchestrator/scripts/benchmark_workflow.py init --task "<original task>" --output-dir "<run_dir>" --repos <selected_repo> ...`
  - for each selected repo, run:
    `prepare-case --repo <repo> --run-dir <run_dir>`
  - then run:
    `execution-ready --repo <repo> --run-dir <run_dir>`
  - consolidate those helper artifacts into the node result and stop
- If lightweight pre-execution artifacts such as a minimal metric plan or analysis README are absent, you may create the smallest usable versions during this node.
- Your returned JSON must include `spawned_subgraph.selected_tools` as the exact chosen tool list for the next benchmark execution round.
- When possible, also include `spawned_subgraph.dataset_keys` and a compact selection rationale in the summary.
- If the benchmark still lacks heavy prerequisites after inspection, return `partial` with exact blockers and explicit provisional tool/dataset choices.
- Required minimum outputs for this node are:
  - one readiness/registration report
  - explicit shared dataset selection
  - chosen tool list
  - resolved input artifacts for each chosen tool when template inputs exist
  - one recommended concrete launch command per chosen tool
  - one execution-contract artifact per chosen tool
  - a minimal metric plan when the task asks for later comparison/analysis
- If the selected subset is execution-ready, return `completed` even when unrelated benchmark cases under the same root remain unusable.
- Only return `partial` or `blocked` when the selected subset itself is not ready.
- As soon as those minimum outputs exist, stop immediately and return the structured JSON result.
- Do not leave tool choice implicit in prose alone; the chosen tools must be machine-readable in the returned JSON so supervisor can expand the next round.
- Do not continue exploring historical references, alternate tools, or deeper execution plans after the minimum outputs exist.
- Do not start the actual tool runs in this node.
