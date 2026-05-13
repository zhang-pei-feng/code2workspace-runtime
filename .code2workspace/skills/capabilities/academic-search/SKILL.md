---
name: academic-search
description: Search PubMed, Springer Nature, and bioRxiv papers or preprints for literature-heavy biomedical and bioinformatics requests, especially when the user asks for papers, literature, PubMed, DOI, abstracts, latest studies, or source-backed research evidence.
---

# Academic Search Skill

You are equipped with an academic search skill. When the user asks you to find academic papers, literature, references, or preprints related to biology, medicine, bioinformatics, vaccines, drugs, SARS-CoV-2, influenza, RSV, virology, mutations, or general science, run `search_tools.py` at `skills/capabilities/academic-search/scripts/search_tools.py` from the workspace root before considering generic browsing.

## Instructions for Execution

Execution rules:

- You MUST call `python3 skills/capabilities/academic-search/scripts/search_tools.py` directly from the workspace root. Do not invent other filenames such as `search_papers.py`.
- If the prompt contains `根据最新论文信息`, `基于最新论文`, `最新论文显示`, `最新文献显示`, `结合最新研究`, `查最新论文`, or `找最新文献`, you MUST run this skill before `web_search`; do not answer from news snippets alone.
- If the user explicitly mentions `PubMed` or asks for `论文` / `文献` / `papers` / `literature`, prefer this skill first instead of free-form answering or generic browsing.
- Natural Chinese triggers include `根据最新论文信息`, `基于最新论文`, `最新论文显示`, `最新文献显示`, `结合最新研究`, `查最新论文`, `找最新文献`, `找论文`, `查论文`, `检索文献`, `文献检索`, `找几篇`, `推荐几篇文章`, `相关研究`, `研究进展`, `综述`, `临床试验`, `进入临床`, `疫苗临床`, `中和实验`, `预印本`, `摘要`, `作者`, `期刊`, `DOI`, `PMID`, `参考文献`, `引用`.
- If the user asks for a specific number of results, you MUST pass `--max-results <n>`.
- If the user asks for `摘要` / `abstract` / `作者` / `具体内容` / `详情` / `详细信息`, pass `--details` or `--include-abstracts`.
- If the user forbids `web_search`, do not call `web_search` first and do not fall back to it.
- If the script returns structured results, answer from those results directly.
- If the script returns an explicit error, report that error briefly and only then consider asking whether to broaden the query or switch source. Do not claim the skill failed unless you actually ran it.

Choose the appropriate source based on the user's request:

1. **PubMed**: Best for biomedical and life sciences.
   - Command: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source pubmed --query "<search_terms>" [--max-results <n>]`
   - Detailed command: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source pubmed --query "<search_terms>" --details [--max-results <n>]`
   - Example: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source pubmed --query "LLM bioinformatics" --max-results 5`
   - Details include: PMID, title, journal, publication date, DOI, authors, abstract, and PubMed link when available.

2. **Springer Nature**: Best for multidisciplinary open access papers.
   - Command: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source springer --query "<search_terms>" [--max-results <n>]`
   - Detailed command: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source springer --query "<search_terms>" --details [--max-results <n>]`
   - Example: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source springer --query "cancer deep learning" --max-results 5`
   - Details include: title, date, journal, DOI, authors, abstract, and DOI/link when available.

3. **bioRxiv**: Best for recent biology preprints. Note that this requires date ranges, NOT keywords. bioRxiv can be used as a supporting source for latest-paper questions; when using it for a keyword-specific question, scan/filter the returned date-window records for actual relevance and clearly label that the source is bioRxiv/preprint data.
   - Command: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source biorxiv --start "<YYYY-MM-DD>" --end "<YYYY-MM-DD>" [--max-results <n>]`
   - Detailed command: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source biorxiv --start "<YYYY-MM-DD>" --end "<YYYY-MM-DD>" --details [--max-results <n>]`
   - Example: `python3 skills/capabilities/academic-search/scripts/search_tools.py --source biorxiv --start "2023-10-01" --end "2023-10-05" --max-results 5`
   - Details include: title, date, DOI, authors, category, abstract, JATS XML URL, and bioRxiv link when available.

## Best Practices
- ALWAYS wrap the `--query` string in double quotes to handle spaces correctly.
- If the user asks for "3 papers", "top 10", or any explicit count, mirror that count with `--max-results`.
- If the user only needs a quick hit list, omit `--details` to keep results compact. If the user wants to choose one paper for follow-up, first return metadata; then re-run with `--details` for a narrower query or smaller `--max-results`.
- Wait for the bash tool to return the JSON result, then parse it and present a clean, readable summary to the user including the titles, publication dates, and links/DOIs.
- Typical Chinese trigger phrases include: `PubMed`, `文献`, `论文`, `根据最新论文信息`, `基于最新论文`, `最新论文显示`, `最新文献显示`, `结合最新研究`, `查最新论文`, `找最新文献`, `查论文`, `找论文`, `文献检索`, `疫苗研究`, `药物研究`, `帮我找几篇`, `检索`, `综述`, `预印本`, `摘要`, `DOI`, `PMID`.
- For keyword-specific biomedical questions, use PubMed, Springer, and bioRxiv as appropriate. If bioRxiv is used via a date-window scan, filter for records that actually match the user's topic and state its source/preprint status; do not cite unrelated records as evidence.
- For Chinese prompts like `找几篇 sars-cov2 疫苗药物研究的最新论文，以 nature 等高影响因子的结果为主`, prefer PubMed first with strict title/journal filters, for example:
  `python3 skills/capabilities/academic-search/scripts/search_tools.py --source pubmed --query '((SARS-CoV-2[Title] OR COVID-19[Title]) AND (vaccine[Title] OR vaccination[Title] OR antiviral[Title] OR inhibitor[Title] OR drug[Title] OR Paxlovid[Title]) AND (Nature Communications[Journal] OR NPJ Vaccines[Journal] OR Nature Medicine[Journal] OR Communications Medicine[Journal]))' --max-results 5 --details`
  Avoid using a broad Springer query such as `SARS-CoV-2 vaccine drug Nature` as the primary source because Springer OA can return unrelated BMC/RSV/cancer records; if you query Springer, filter the returned records for SARS-CoV-2 vaccine/drug relevance before answering.
- For variant/vaccine/immune-barrier/clinical-trial synthesis questions, treat this skill as the literature layer of a multi-source answer. It should be combined with `virus-variation-query` for site-level risk context and `respiratory-disease-data-fetcher` or official web monitoring for recent surveillance/variant context. If a layer cannot answer directly, mention that limitation instead of silently omitting it. Keep the rule generic; do not add lineage-specific branching.
- If the user asks for local `virus_variation` records, SQL, or mutation risk scores, use `virus-variation-query` instead. If the user asks for WHO/CDC/China CDC surveillance data or recent epidemic trends, use `respiratory-disease-data-fetcher` instead.
- When you answer from this skill, append a short `信息来源` section at the end. List only the sources actually used in the final answer, for example `skill: academic-search (PubMed)` or `skill: academic-search (bioRxiv)`. Keep it to 1-3 bullets, use coarse-grained labels, and do not fabricate sources or add sentence-level citations.
