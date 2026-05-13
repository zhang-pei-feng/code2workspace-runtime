---
name: respiratory-disease-data-fetcher
description: 获取少量固定官方来源的数据，包括 WHO COVID-19 Cases、WHO Variants、U.S. CDC Trends、中国疾控全国新型冠状病毒感染疫情情况、WHO Africa Weekly Bulletin。仅当用户明确要这些固定来源或快速查看 WHO/CDC/中国疾控等少量源时使用本 skill；如果用户要查的呼吸道病原、COVID/新冠、流感、RSV、变异株、毒株谱系、病毒监测、周报、月报、疾控/卫生部门通报、原始网页/PDF、跨地区监测信息可能存在于整理后的来源表中，应优先改用 `respiratory-disease-wide-monitor` 筛选并抓取相关来源。不适合本地 virus_variation 变异风险 SQL 查询或纯论文检索作为唯一来源。若用户要求生成正式报告，可先获取数据，再结合 `multi-source-report` 或当前 supervisor 报告流程整合。
---

# 概述
该技能旨在获取多个来源的实时数据，涉及新冠疫情和其他呼吸道疾病的趋势和统计数据。数据来源包括 WHO、美国 CDC、中国 CDC 以及 WHO 非洲地区每周的疫情通报。

# 自然语言触发
仅在用户明确要少量固定源时使用本 skill，典型说法：

- “近期全球新冠/流感/RSV 流行情况怎么样”
- “查一下 WHO/CDC/中国疾控 最新疫情监测数据”
- “美国 COVID 检测阳性率、急诊或死亡趋势”
- “中国疾控最近一个月的新冠疫情情况”
- “WHO 最新变异株 XFG/LP.8.1/NB.1.8.1 风险信息”
- “呼吸道疾病/呼吸系统病原 本周或近期趋势”

如果用户问的是本地 `virus_variation` 表、SQL、位点风险、抗体逃逸、受体结合，用 `virus-variation-query`；如果用户问找论文、PubMed、DOI、摘要，用 `academic-search`。

如果用户要查的信息可能在整理后的来源表中，例如呼吸道病原、COVID/新冠、流感、RSV、变异株、毒株谱系、病毒监测、周报、月报、疾控/卫生部门通报、原始网页/PDF、跨地区监测，优先改用 `respiratory-disease-wide-monitor` 先筛选并抓取相关来源；这不要求用户明确说“全量”“广域”或“大量来源”。

如果用户问的是“某个变异株/谱系是否可能突破疫苗或既往免疫屏障”“针对某个变异株是否有新疫苗或临床试验”“变异株风险研判”等综合问题，应把本 skill 作为官方/网页监测层，与 `academic-search` 的论文/预印本/临床试验证据和 `virus-variation-query` 的本地位点风险证据结合使用。若官方监测源没有直接覆盖该变异株或临床试验，不要编造；说明“官方监测未直接给出该点”，再用其他来源补充。

# 数据源
1. **WHO 新冠疫情数据**  
   数据来源于 WHO 新冠疫情仪表板，包含各国报告的新冠病例数。  
   [数据来源](https://data.who.int/dashboards/covid19/cases)

2. **美国 CDC 新冠疫情趋势**  
   美国新冠死亡病例、急诊科就诊情况和检测阳性率的趋势。  
   [数据来源](https://covid.cdc.gov/covid-data-tracker/#trends_select_testpositivity_00)

3. **中国 CDC 新冠疫情数据**  
   中国的 COVID-19 及其他呼吸道疾病的数据，包括全国的病例报告、医院监测和检测数据。  
   [数据来源](https://www.chinacdc.cn/jksj/xgbdyq/)

4. **WHO 非洲地区疫情周报**  
   WHO 每周发布的非洲大陆各类疾病暴发情况报告，包括新冠疫情和其他传染病。  
   [数据来源](https://www.afro.who.int/health-topics/disease-outbreaks/outbreaks-and-other-emergencies-updates?utm_source=chatgpt.com)

5. **WHO 新冠变异株数据**  
   数据来源于 WHO 新冠变异株仪表板，提供了 WHO 关注的变异株（VOI 和 VUM）详细信息，包括风险评估报告链接。  
   [数据来源](https://data.who.int/dashboards/covid19/variants)

# 使用方法
当需要获取最新的疫情数据时，可以使用该技能查询：
1. 从 **WHO** 获取全球新冠疫情数据。
2. 从 **美国 CDC** 获取新冠趋势数据。
3. 从 **中国 CDC** 获取新冠及其他传染病数据。
4. 从 **WHO 非洲地区** 获取每周的疫情暴发更新。
5. 从 **WHO** 获取新冠变异株的数据，包含 VOC（关注变异株）和 VUM（需要关注的变异株）。

## 示例提示
- "全球最新的新冠疫情情况是什么？"
- "美国的新冠死亡趋势如何？"
- "中国的疫情情况是什么？"
- "获取 WHO 非洲地区的最新疫情周报。"
- "获取 WHO 新冠变异株的详细数据。"

## 输出示例
- WHO 新冠疫情数据（全球、国家级别的疫情数据）
- 美国 CDC 新冠趋势（死亡率、急诊科就诊数据、检测阳性率）
- 中国 CDC 每月疫情数据（确诊病例、重症病例、变异株监测）
- WHO 非洲地区每周疫情周报（PDF下载并提取的报告内容）
- WHO 新冠变异株数据（包含 VOI 和 VUM 毒株的风险评估）

## 来源标注
- 当你基于该 skill 的结果回答用户时，在答案末尾增加一个简短的“信息来源”小节。
- 只列出本轮最终实际采用的来源，例如 `skill: respiratory-disease-data-fetcher（China CDC）`、`skill: respiratory-disease-data-fetcher（U.S. CDC Trends）`、`skill: respiratory-disease-data-fetcher（WHO）`。
- 保持粗粒度，1-3 条即可；不要编造来源，也不要做逐句脚注。
