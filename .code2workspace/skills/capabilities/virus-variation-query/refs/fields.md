# 数据库字段语义手册

`virus_variation` 风险库和数据治理智能体 `covid_data` 库的字段定义和含义说明。

---

## ncov_amino_acid_risk（新冠）

参考序列：MN908947

| 字段 | 类型 | 说明 |
|------|------|------|
| gene | VARCHAR(32) | 基因名，10个：ORF1ab(134824) S(24187) N(7961) ORF3a(5225) M(4218) ORF7a(2299) ORF8(2299) E(1425) ORF6(1159) ORF10(722) |
| aminoacid_site | INT | 氨基酸位点编号（基于参考序列） |
| ref_aminoacid | CHAR(1) | 参考序列氨基酸（单字母） |
| aminoacid | CHAR(1) | 变异后氨基酸（单字母） |
| ace2 | TINYINT | **ACE2受体结合亲和力影响**：0=无影响；2=中度增强；3=高度增强；NULL=未评估 |
| antibody | TINYINT | **抗体结合效率降低程度**：0=无影响；1=轻度降低；2=中度降低；3=高度降低（高度逃逸）；NULL=未评估 |
| aminoacid_substitution | TINYINT | **碱基替换发生难度**：0=最难（多碱基替换）；1=较难；2=中等；3=最易（单碱基替换即可）；NULL=未评估 |

**索引**：(gene, aminoacid_site)，(gene, aminoacid_site, ref_aminoacid, aminoacid)

**高风险组合**：`antibody>=2 AND aminoacid_substitution>=2`（既易发生又难被抗体识别）

---

## flu_variation（流感）

| 字段 | 类型 | 说明 |
|------|------|------|
| reference | VARCHAR(32) | **型别+片段**，格式见下方枚举 |
| gene | VARCHAR(32) | 基因名：HA NA PB2 PB1 NP PA NS1 M1 等 |
| ref_aminoacid | CHAR(1) | 参考序列氨基酸（单字母） |
| ref_aminoacid_site | INT | 参考序列氨基酸位点 |
| aminoacid | CHAR(1) | 变异后氨基酸（单字母） |
| anti_risk | TINYINT | **抗体结合效率降低程度**：0=无影响；1=轻度；2=中度；3=高度降低；NULL=未评估 |
| receptor_risk_23 | TINYINT | **α-2,3受体结合亲和力**（禽类受体）：0=无影响→3=高度增强；NULL=未评估 |
| receptor_risk_26 | TINYINT | **α-2,6受体结合亲和力**（人类受体）：0=无影响→3=高度增强；NULL=未评估 |
| matrix_risk | TINYINT | **碱基替换发生难度**：0=最易→3=最难；NULL=未评估 |

**reference 枚举**：

| 前缀 | 说明 | 常用值 |
|------|------|--------|
| `A-seg1~8` | 甲型8个基因片段 | A-seg4=HA, A-seg6=NA |
| `A-seg4_H1~H16` | HA亚型细分 | H1(H1N1), H3(H3N2), H5(H5N1), H7, H9 |
| `A-seg6_N1~N9` | NA亚型细分 | N1(H1N1), N2(H3N2) |
| `B-seg1~8` | 乙型8个基因片段 | — |
| `C-seg1~7` | 丙型7个基因片段 | — |

**高风险组合**：`receptor_risk_26>=2`（人类受体适应）或 `anti_risk>=2 AND matrix_risk>=2`

---

## mpxv_variation（猴痘）

参考序列：NC_063383.1，179个基因（OPG系列）

| 字段 | 类型 | 说明 |
|------|------|------|
| gene | VARCHAR(32) | 基因名，OPG001~OPG210等；高变基因：OPG210(35740) OPG105(24454) OPG151(22136) |
| ref_aminoacid | CHAR(1) | 参考序列氨基酸（单字母） |
| ref_aminoacid_site | INT | 参考序列氨基酸位点 |
| aminoacid | CHAR(1) | 变异后氨基酸（单字母） |
| anti_risk | TINYINT | **抗体结合效率降低程度**：0=无影响；2=中度降低；3=高度降低；NULL=未评估（占绝大多数） |
| matrix_risk | TINYINT | **碱基替换发生难度**：0=最难→3=最易；NULL=未评估 |

**索引**：(gene, ref_aminoacid_site)，(gene, ref_aminoacid_site, ref_aminoacid, aminoacid)

**注意**：anti_risk 非 NULL 记录极少（<0.2%），查询时应先确认是否有非NULL数据。

---

## covid_data（数据治理智能体本地库）

数据治理智能体仓库：`${DATA_GOVERNANCE_AGENT_ROOT}`

配置来源：`home/.data_governance_agent/config.toml` 的 `[mysql]` 段。优先通过数据治理智能体的 `execute_sql` 入口读取配置，不要把 MySQL 密码复制到输出里。

### ncov_source_harvest_runs

记录每次本地同步/导入运行。

| 字段 | 类型 | 说明 |
|------|------|------|
| run_id | VARCHAR | 稳定运行 ID |
| source_id | VARCHAR | 数据源，例如 `ncbi_virus`, `sra`, `pango_lineages`, `epietl` |
| query_term | VARCHAR | 上游查询词或逻辑入口 |
| fetched_records | BIGINT | 本次抓取记录数 |
| stored_records | BIGINT | 本次落库规范化记录数 |
| status | VARCHAR | `running`, `completed`, `failed` 等 |
| started_at / completed_at | DATETIME | 运行开始/结束时间 |

### ncov_source_records

NCBI Virus / SRA 等序列类来源的规范化记录。

| 字段 | 类型 | 说明 |
|------|------|------|
| source_id | VARCHAR | 数据源 |
| accession | VARCHAR | 来源记录 accession |
| sample_name | TEXT | 样本名或 run title |
| organism | VARCHAR | 规范化物种名 |
| host | VARCHAR | 宿主 |
| collection_date | VARCHAR | 采样日期 |
| country / region | VARCHAR | 国家/地区 |
| lineage | VARCHAR | 来源记录里可得的谱系字段 |
| sequence_length | BIGINT | 序列长度或碱基数 |
| source_url | TEXT | 来源记录 URL |

### ncov_lineage_catalog

Pango lineage catalog。

| 字段 | 类型 | 说明 |
|------|------|------|
| lineage | VARCHAR | Pango 谱系名，主键 |
| detail_url | VARCHAR | lineage 详情页 |
| most_common_countries | TEXT | 发布源中的常见国家摘要 |
| earliest_date | VARCHAR | 发布源中的最早日期 |
| designated_count / assigned_count | INT | 发布源中的计数 |
| description | TEXT | 描述 |
| who_name | VARCHAR | WHO 命名标签，如可得 |
| source_page | VARCHAR | 来源页 |

### ncov_lineage_mutations

Pango constellation 定义解析出的谱系突变表。用于回答“某谱系有哪些突变”，但不包含 ACE2/antibody 风险评分。

| 字段 | 类型 | 说明 |
|------|------|------|
| lineage | VARCHAR | 谱系名 |
| definition_name | VARCHAR | 上游 constellation 定义文件 |
| label | VARCHAR | 定义内标签 |
| who_name | VARCHAR | WHO 标签，如可得 |
| phe_label | VARCHAR | PHE 标签，如可得 |
| raw_site | VARCHAR | 原始位点字符串，例如 `S:E484K`, `spike:Q493R`, `nuc:C25000T` |
| site_type | VARCHAR | `amino_acid`, `nucleotide`, `amino_acid_deletion`, `unparsed` 等 |
| gene | VARCHAR | 解析后的基因；Spike 可能是 `S` 或 `SPIKE` |
| position_start / position_end | INT | 解析后起止坐标 |
| ref_residues / alt_residues | VARCHAR | 参考/替代残基 |
| definition_url | VARCHAR | 上游定义 URL |

**Spike/RBD 查询注意**：不要只查 `gene='S'`。推荐条件：

```sql
(gene IN ('S','SPIKE') OR LOWER(raw_site) LIKE 's:%' OR LOWER(raw_site) LIKE 'spike:%')
AND position_start BETWEEN 319 AND 541
```

### ncov_epietl_reports / ncov_epietl_risk_events / ncov_epietl_channels

EpiETL 监测报告、AI 抽取风险事件和 source channel 元数据。

常用字段：

- reports: `title`, `country`, `pathogen`, `epi_week`, `source_org`, `source_url`, `published_at`, `report_date`, `summary`
- risk_events: `title`, `severity`, `category`, `pathogen`, `country`, `epi_week`, `source_url`, `period_start`, `period_end`, `summary`
- channels: `channel_id`, `name`, `organization`, `country_or_region`, `source_type`, `status`, `last_sync`

---

## 评分一览

| 字段 | 0 | 1 | 2 | 3 | NULL |
|------|---|---|---|---|------|
| ace2 | 无影响 | — | 中度增强ACE2结合 | 高度增强 | 未评估 |
| antibody / anti_risk | 无影响 | 轻度降低抗体结合 | 中度降低 | 高度降低（高逃逸） | 未评估 |
| receptor_risk_23 | 无影响 | 轻度 | 中度增强禽类受体结合 | 高度增强 | 未评估 |
| receptor_risk_26 | 无影响 | 轻度 | 中度增强人类受体结合 | 高度增强 | 未评估 |
| aminoacid_substitution / matrix_risk | 最难发生 | 较难 | 中等 | 最易（单碱基）| 未评估 |
