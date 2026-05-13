# Local Data Lane Guidance

- Focus on structured local data, APIs, registries, or databases relevant to the report topic.
- Minimum output is:
  - one lane brief
  - concrete data extracts or an explicit null-result statement
  - data quality caveats
  - uncertainty note
- If no suitable local dataset is available, say so explicitly and return `partial` rather than continuing to search indefinitely.
- As soon as the minimum lane brief exists, stop and return structured JSON.
