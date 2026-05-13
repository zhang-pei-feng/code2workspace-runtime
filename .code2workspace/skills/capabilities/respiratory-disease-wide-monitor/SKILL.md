---
name: respiratory-disease-wide-monitor
description: "呼吸道病原官方网页/报告来源索引与抓取 skill。不要用于 EpiETL/source catalog/source type/structured XLS、CSV、JSON 数据源目录问题；这类问题即使涉及 COVID、流感、RSV，也应使用 `epietl-api`。本 skill 读取 `skills/capabilities/respiratory-disease-wide-monitor/整理后的数据源表.xlsx` 中整理的 50+ WHO、CDC、中国疾控、省级疾控、港澳台、亚洲、北美、南美、欧洲、澳洲、非洲、GISAID、ECDC、CoV-Spectrum、Nextstrain、EVEscape、CoV-AbDab 等呼吸道疾病/新冠/流感/RSV/变异株数据源；只要用户要查的呼吸道病原、COVID、新冠、流感、RSV、变异株、毒株谱系、病毒监测、周报、月报、疾控/卫生部门通报、原始网页/PDF、跨地区监测信息可能存在于这张来源表里，就应优先使用本 skill 先筛选相关来源并抓取符合需求的数据源信息，不要求用户明确说“大量来源/全量扫描”。也适用于“监测五十多个数据源/广域数据源/全量数据源/整理后的数据源表/更多数据源/多数据源监测/全球呼吸道病原监测/网页数据监测/下探一级网页或 PDF/从数据源表抓取”等显式说法。不要删除或替代原 `respiratory-disease-data-fetcher`；当用户明确只要旧 skill 覆盖的少量固定源快速结果时可继续用旧 skill，否则相关数据可能在来源表中时先用本 skill。"
metadata: { "openclaw": { "emoji": "🌐", "requires": { "bins": ["python3"] } } }
---

# Respiratory Disease Wide Monitor

这个 skill 是呼吸道病原官方网页、报告和数据看板的来源索引与抓取入口，覆盖 `整理后的数据源表.xlsx` 里的 50+ 数据源。不要删除或替代现有 `respiratory-disease-data-fetcher`。

## 何时使用

优先用于：只要用户要查的信息可能存在于数据源表中，就先用本 skill 筛选并抓取相关来源；不要求用户明确要求“全量”“广域”或“大量数据源”。

- “监测五十多个数据源”
- “按整理后的数据源表抓取”
- “全球/广域/多数据源呼吸道病原监测”
- “查某个呼吸道病原、变异株或毒株谱系的官方监测更新”
- “查看 COVID/新冠、流感、RSV 的疾控/卫生部门原始网页或报告”
- “尽量全面看一下全球各地呼吸道病原最新监测”
- “多国/多地区/多机构的 COVID、流感、RSV 趋势扫描”
- “汇总各国疾控或卫生部门最新周报/月报”
- “检查近期国际上关于新冠变异株的监测更新”
- “不要只看 WHO/CDC/中国疾控，尽量多看一些地区来源”
- “新冠、流感、RSV、变异株的网页数据监测”
- “每个数据源尽量下探一级网页或 PDF”
- “比原来的 5 个数据源更全”

如果用户明确只要少量 WHO/CDC/中国疾控固定源的快速结果，可继续使用 `respiratory-disease-data-fetcher`。否则，只要相关信息可能在本表中，优先使用本 skill 从表中筛选来源后抓取。

如果用户问的是 EpiETL/source catalog/source channel/source type、Dashboard/Report Collection/News/Data 分类，或结构化 XLS/CSV/JSON 数据源地址，不要用本 skill 抢路由；改用 `epietl-api`。

## 数据源表

默认表格：

```text
skills/capabilities/respiratory-disease-wide-monitor/整理后的数据源表.xlsx
```

字段包括：`数据源名称`、`数据源类别`、`数据维护方`、`URL`、`病原类型`、`描述`。脚本会跳过“站点名称”分组行，只抓取有 URL 的实际数据源。

## 首选命令

全量抓取，根页面 + 每个源下探 1 个最相关的网页或 PDF：

```bash
python3 skills/capabilities/respiratory-disease-wide-monitor/scripts/fetch_sources.py \
  --format markdown \
  --child-limit 1 \
  --workers 8 \
  --timeout 12 \
  --output tmp/respiratory-wide-monitor.md
```

调试或快速试跑：

```bash
python3 skills/capabilities/respiratory-disease-wide-monitor/scripts/fetch_sources.py \
  --limit 5 \
  --child-limit 1 \
  --format json
```

按关键词/意图筛选。`--query` 会先做意图扩展和来源打分，例如 `XFG`、`JN.1`、`NB.1.8.1` 这类谱系名会自动落到变异株相关来源，而不是要求这些字符必须出现在表格字段里：

```bash
python3 skills/capabilities/respiratory-disease-wide-monitor/scripts/fetch_sources.py \
  --query "variant" \
  --child-limit 1 \
  --format markdown
```

按病原筛选：

```bash
python3 skills/capabilities/respiratory-disease-wide-monitor/scripts/fetch_sources.py \
  --pathogen "新冠" \
  --child-limit 1 \
  --format markdown
```

## 使用规则

1. 默认不改动数据源表；只读取 `skills/capabilities/respiratory-disease-wide-monitor/整理后的数据源表.xlsx`。
2. 查询/抓取数据时不要编辑 `scripts/fetch_sources.py`、`scripts/get_latest_monitoring_data.py` 或 skill 文件；直接运行现有脚本。只有用户明确要求修改 skill 代码时才可以编辑。
3. 不要尝试替换不存在的占位文本，例如 `# TODO: Fetch sources`；这类 edit 失败不应阻断查询。
4. 默认尽量下探一级：从根页面抽取高相关链接，优先报告、周报、月报、监测、PDF、variant、COVID、influenza、RSV 等链接。
5. 对动态仪表板、登录墙、反爬、PDF 文本不可提取等情况，不要编造内容；记录 HTTP 状态、标题、描述、可访问性和限制。
6. 结果较多时先给摘要：总数据源数、可访问数、失败数、下探成功数、关键数据源发现，再附输出文件路径。
7. 如果用户需要正式报告，可把本 skill 输出作为证据源，再交给 `multi-source-report` 或当前 supervisor 报告流程整合成正式报告。
8. 不要把本 skill 用于本地 `virus_variation` SQL 查询；本地变异风险库仍用 `virus-variation-query`。

## 输出说明

脚本支持：

- `--format json`：结构化结果，适合后处理。
- `--format markdown`：适合直接总结或作为报告素材。
- `--output <path>`：把完整结果写入文件，终端只打印路径。

回答用户时建议说明：

- 数据源表路径
- 抓取了多少个源
- 根页面可访问数量
- 下探页面/PDF 数量
- 失败源和失败原因概览
- 关键发现或后续可深入的来源
