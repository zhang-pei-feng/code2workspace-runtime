---
name: epietl-api
description: Query the EpiETL Epidemic Intelligence Data API for global disease surveillance reports, AI-extracted pathogen risk events, data source channels, and curated respiratory/COVID data sources. Use this before respiratory-disease-wide-monitor for source catalog/source URL/source type/structured XLS CSV JSON data source questions, even when the topic is COVID/flu/RSV. Use when users ask for EpiETL, epidemic intelligence, global disease surveillance reports, pathogen risk events, disease outbreak reports by country/source/pathogen/severity, public health channel listings, or source catalogs/source URLs/source types covering Dashboard, Report Collection/报告集合, News, and structured Data sources. Covered examples include WHO COVID-19 dashboards/CSV/hospitalization-ICU data/variant report collection/global risk assessment, China CDC/中国疾控 report collections, Hong Kong CHP/香港卫生防护中心 COVID/flu reports and XLS data including flux_data.xlsx/covidx_data.xlsx, and Taiwan CDC/台湾疾控 COVID report collections.
---

# EpiETL API

Use this skill to query EpiETL at `https://epietl.com` for disease surveillance reports, pathogen risk events, channel lists, API health checks, and curated source catalogs.

## Natural Language Triggers

Use this skill when users ask for:

- EpiETL / epidemic intelligence / pathogen risk events / disease outbreak reports.
- Public health source channels, data source catalogs, source types, source URLs, or whether a source is a dashboard, report collection, news item, or structured data table.
- Reports or events filtered by country, source, pathogen, disease, severity, or channel.
- WHO COVID-19 dashboards, WHO COVID CSV data, WHO COVID hospitalization/ICU data, WHO COVID variants/report collections, or WHO COVID global risk assessment.
- China CDC / 中国疾控 report collections / 报告集合 for national infectious disease surveillance, COVID situation, notifiable diseases, public health events, influenza, or acute respiratory infectious disease sentinel monitoring.
- Hong Kong CHP / 香港卫生防护中心 / 香港疾控 COVID/influenza surveillance reports or structured XLS data such as `flux_data.xlsx` and `covidx_data.xlsx`.
- Taiwan CDC / 台湾疾控 COVID report collections / 报告集合.

Do not use this skill as a replacement for local `virus_variation` SQL queries or pure literature search. If the user asks for a finished written report, gather EpiETL evidence if relevant, then route report writing through `multi-source-report` or the current supervisor report flow.

## Source Types

- `Dashboard`: stable source URL; data is primarily presented as charts.
- `Report Collection`: usually updated by week or reporting period; data is primarily presented as reports.
- `News`: source URL is not fixed; content is primarily report/news style.
- `Data`: relatively structured tables such as Excel, CSV, or JSON.

## Curated Source Coverage

WHO COVID-19:

- Dashboard:
  - `https://data.who.int/dashboards/covid19/summary`
  - `https://data.who.int/dashboards/covid19/circulation`
  - `https://data.who.int/dashboards/covid19/cases`
  - `https://data.who.int/dashboards/covid19/deaths`
  - `https://data.who.int/dashboards/covid19/hospitalizations`
- Report Collection:
  - `https://data.who.int/dashboards/covid19/variants`
- Data:
  - `https://srhdpeuwpubsa.blob.core.windows.net/whdh/COVID/WHO-COVID-19-global-data.csv`
  - `https://srhdpeuwpubsa.blob.core.windows.net/whdh/COVID/WHO-COVID-19-global-table-data.csv`
  - `https://srhdpeuwpubsa.blob.core.windows.net/whdh/COVID/WHO-COVID-19-global-hosp-icu-data.csv`
- News:
  - `https://www.who.int/publications/m/item/covid-19-global-risk-assessment--version-9`

CN-CDC:

- Report Collection:
  - `https://www.chinacdc.cn/jksj/jksj01/`
  - `https://www.chinacdc.cn/jksj/xgbdyq/`
  - `https://www.chinacdc.cn/jksj/jksj02/`
  - `https://www.chinacdc.cn/jksj/jksj03/`
  - `https://www.chinacdc.cn/jksj/jksj04_14249/`
  - `https://www.chinacdc.cn/jksj/jksj04_14275/`

HK-CDC / Hong Kong CHP:

- Report Collection:
  - `https://www.chp.gov.hk/sc/resources/29/100148.html`
- Data:
  - `https://www.chp.gov.hk/files/xls/flux_data.xlsx`
  - `https://www.chp.gov.hk/files/xls/covidx_data.xlsx`

TW-CDC:

- Report Collection:
  - `https://www.cdc.gov.tw/Category/MPage/iclxC6BjjFmtM1oT54EVuw`

## Authentication

- Do not hard-code API keys into skill files or responses.
- Use `EPIETL_API_KEY` for authenticated report search:

```bash
export EPIETL_API_KEY="..."
```

- `/api/reports` requires `Authorization: Bearer <key>`.
- `/api/risk/events`, `/api/channels`, and `/api/health` are public.

## Quick Start

Run the helper script from this skill:

```bash
python3 skills/capabilities/epietl-api/scripts/epietl_api.py health
python3 skills/capabilities/epietl-api/scripts/epietl_api.py channels --limit 20
python3 skills/capabilities/epietl-api/scripts/epietl_api.py events --severity critical --pathogen cholera --limit 10
EPIETL_API_KEY="$EPIETL_API_KEY" python3 skills/capabilities/epietl-api/scripts/epietl_api.py reports --country China --limit 5
```

Use `--param key=value` for query parameters not exposed as first-class flags:

```bash
python3 skills/capabilities/epietl-api/scripts/epietl_api.py events --param pathogen=cholera --param severity=critical --limit 10
```

## Workflow

1. Clarify the user's target if needed: reports vs risk events vs channels.
2. Prefer `events` for AI-extracted pathogen risk events. This is public.
3. Use `reports` for surveillance report search. Ensure `EPIETL_API_KEY` is set before calling it.
4. Keep `limit` at or below 200 and use `offset` for pagination.
5. Summarize returned items with dates, country/location, pathogen/disease, severity, source/channel, and source URLs when available.
6. If the API returns an error, report the HTTP status and response summary rather than inventing results.

## API Reference

Read `references/api.md` when you need endpoint details, parameters, citation text, or direct `curl` examples.

## Attribution

When using EpiETL data in a final answer, include a concise source note:

```text
信息来源：EpiETL (https://epietl.com), developed and maintained by Greater Bay Area Center for Bioinformation (GBACB). Original surveillance data belongs to the respective public health agencies listed in the source records.
```
