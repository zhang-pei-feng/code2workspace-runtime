# Benchmark Case Node Guidance

- Prefer the concrete execution context already prepared by the registration node.
- Read the latest registration/readiness artifact first and reuse its recommended launch command when available.
- If an execution-contract artifact exists for this case, follow it verbatim before trying any alternate command.
- If register artifacts already name the shared dataset, resolved inputs, expected outputs, and helper command, do not keep searching for alternatives.
- Prefer the deterministic helper command shape:
  `python3 .code2workspace/skills/orchestration/benchmark-workflow-orchestrator/scripts/benchmark_workflow.py run-repo-native --repo <repo> --run-dir <run_dir>`
- Execute one concrete case path first.
- Do not spend time probing generic tool metadata once a runnable launch command is already known.
- After the command exits, inspect only the expected outputs declared by the case manifest or result manifest.
- Do not read full FASTA, BAM, VCF, or large log files after completion.
- Use only lightweight checks such as file existence, file size, directory listing, and log tail when needed.
- Write or preserve a small result-manifest artifact that records exact output paths, exit code, and lightweight file checks.
- As soon as those artifacts or a concrete failure log exist, return the structured JSON result immediately.
