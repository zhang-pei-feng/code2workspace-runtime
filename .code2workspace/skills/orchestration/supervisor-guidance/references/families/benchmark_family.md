# Benchmark Family Guidance

- Treat the benchmark task as a staged workflow: first inspect benchmark assets and choose a runnable shared-dataset/tool subset, then fan out per-tool execution, then summarize.
- Registration may spend meaningful time inspecting benchmark assets under the user-provided benchmark path so later execution nodes start with concrete dataset and WDL/input knowledge.
- Prefer explicit shared-dataset/tool mappings and explicit metric plan artifacts before launching tool runs, but if no checked-in catalog exists, derive the subset from the benchmark directory structure itself.
- If the benchmark directory already contains datasets, WDL, or inputs, use those concrete assets rather than staying at a purely abstract planning level.
- Keep early-node search bounded to the benchmark root, staged case directories, and dataset/input/WDL files needed for the immediate next step.
- For user tasks that ask you to choose a shared dataset and multiple tools, optimize for one coherent runnable subset rather than trying to certify every benchmark case in the directory.
- The register node is allowed to decide the tool subset for the later execution graph; it should return that subset explicitly instead of assuming the graph already knows it.
- Execution nodes should inspect the run directory and consume the latest registration/readiness artifacts before choosing commands.
- Prefer explicit execution-contract artifacts over free-form reconstruction of commands in later execution nodes.
