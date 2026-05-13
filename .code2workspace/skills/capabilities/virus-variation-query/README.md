# Virus Variation Query Skill

这是一个用于查询本地病毒变异相关数据库的 OpenClaw 技能。

## 📁 目录结构

```
virus-variation-query/
├── SKILL.md          # 主技能文件 (带 YAML front matter)
├── README.md         # 本文档
├── scripts/          # 辅助脚本
│   ├── site.sh       # 特定位点查询
│   ├── risk.sh       # 高风险突变筛选
│   └── stats.sh      # 统计汇总
└── refs/            # 参考文档
    ├── fields.md     # 数据库字段说明
    └── queries.md    # SQL 查询模板
```

## 🔧 功能特性

- **新冠病毒** (SARS-CoV-2): Spike蛋白抗体逃逸、ACE2受体结合分析
- **流感病毒** (Influenza): HA/NA蛋白抗原性、受体结合风险评估  
- **猴痘病毒** (Mpox): 膜蛋白变异风险分析
- **数据治理库** (`covid_data`): Pango lineage catalog、谱系突变、NCBI/SRA 导入记录、EpiETL 报告/风险事件

## 🎯 激活条件

用户询问以下关键词时自动激活：
- 病毒突变、基因变异
- 位点风险、抗体逃逸
- 受体结合、型别对比
- 谱系突变、Pango lineage、导入状态

## 📊 数据规模

| 病毒 | 记录数 | 主要基因 |
|------|--------|----------|
| 新冠 | ~196k | S, N, ORF1ab |
| 流感 | ~524k | HA, NA |
| 猴痘 | ~1052k | OPG210, OPG105 |

数据治理智能体 `covid_data` 当前主要表：

| 表 | 记录数 | 用途 |
|----|--------|------|
| `ncov_source_records` | ~20k | NCBI Virus / SRA 规范化记录 |
| `ncov_lineage_catalog` | ~75 | Pango 谱系目录 |
| `ncov_lineage_mutations` | ~625 | Pango constellation 谱系突变 |
| `ncov_epietl_reports` | ~200 | EpiETL 监测报告 |
| `ncov_epietl_risk_events` | ~188 | EpiETL 风险事件 |

## 🚀 使用方法

技能被激活后，可以：
1. 查询特定位点的变异风险
2. 筛选高风险突变位点
3. 获取基因层面统计数据
4. 查询本地谱系突变，再回查位点风险
5. 检查数据治理智能体本地导入状态
6. 执行自定义 SQL 查询

所有查询都是只读的，确保数据安全。

---

**版本**: OpenClaw Standard Format v1.0  
**更新**: 2026-03-20
