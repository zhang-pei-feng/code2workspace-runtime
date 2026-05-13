# Init Report Node Guidance

- This node may spend time producing a robust initialization scaffold, but it must stay inside initialization scope only.
- Required minimum outputs for this node are:
  - a request note that restates the user task
  - a report contract artifact
  - lane scaffolding with explicit evidence expectations
  - the directory skeleton needed by downstream report nodes
- Do not do first-pass research in this node.
- Do not draft the final report body in this node.
- As soon as the minimum initialization artifacts above exist, stop and return a structured JSON result immediately.
- If some initialization artifact still cannot be created, return `partial` with exact blocker details instead of continuing to think.
