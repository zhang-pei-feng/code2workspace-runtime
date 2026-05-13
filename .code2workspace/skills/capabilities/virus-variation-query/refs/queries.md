# SQL 查询模板集

`virus_variation` 风险库和数据治理智能体 `covid_data` 库的常用 SQL 查询模板。

## 新冠 ncov_amino_acid_risk

```sql
-- 精准位点
SELECT gene, aminoacid_site, CONCAT(ref_aminoacid,aminoacid_site,aminoacid) mut,
       ace2, antibody, aminoacid_substitution
FROM ncov_amino_acid_risk
WHERE gene='{GENE}' AND aminoacid_site={SITE};

-- 位点范围
WHERE gene='{GENE}' AND aminoacid_site BETWEEN {S} AND {E}

-- 高抗体逃逸（antibody>=2）
WHERE antibody>=2 ORDER BY antibody DESC, ace2 DESC LIMIT 50;

-- 高传播风险（易发生+易逃逸）
WHERE antibody>=2 AND aminoacid_substitution>=2 ORDER BY antibody DESC;

-- 基因统计
SELECT gene, COUNT(*) total,
  SUM(antibody>=2) escape2plus, SUM(antibody=3) escape3,
  SUM(ace2>=2) ace2_impact
FROM ncov_amino_acid_risk GROUP BY gene ORDER BY escape2plus DESC;
```

---

## 数据治理库 covid_data

优先用配置驱动入口执行，避免泄露 MySQL 密码：

```bash
cd "$DATA_GOVERNANCE_AGENT_ROOT" && PYTHONPATH=src .venv/bin/python - <<'PY'
from data_governance_mvp.local_db import execute_sql
print(execute_sql("""SELECT COUNT(*) FROM ncov_lineage_mutations;""", batch=True))
PY
```

### 谱系 Spike/RBD 突变

```sql
SELECT lineage, raw_site, site_type, gene, position_start, position_end,
       ref_residues, alt_residues, definition_name, definition_url
FROM ncov_lineage_mutations
WHERE lineage='{LINEAGE}'
  AND (gene IN ('S','SPIKE') OR LOWER(raw_site) LIKE 's:%' OR LOWER(raw_site) LIKE 'spike:%')
  AND position_start BETWEEN 319 AND 541
ORDER BY position_start LIMIT 100;
```

### 谱系所有氨基酸突变

```sql
SELECT lineage, raw_site, site_type, gene, position_start, position_end,
       ref_residues, alt_residues
FROM ncov_lineage_mutations
WHERE lineage='{LINEAGE}' AND site_type IN ('amino_acid','amino_acid_deletion')
ORDER BY gene, position_start LIMIT 200;
```

### 数据治理库命中后查风险库

把 `ncov_lineage_mutations.position_start` 命中的 Spike 位点代入 `virus_variation.ncov_amino_acid_risk`：

```sql
SELECT gene, aminoacid_site,
       CONCAT(ref_aminoacid, aminoacid_site, aminoacid) mut,
       ace2, antibody, aminoacid_substitution
FROM ncov_amino_acid_risk
WHERE gene='S' AND aminoacid_site IN ({SITES})
ORDER BY aminoacid_site, antibody DESC, ace2 DESC LIMIT 100;
```

### 谱系目录和导入状态

```sql
-- Pango lineage catalog
SELECT lineage, who_name, earliest_date, designated_count, assigned_count, description
FROM ncov_lineage_catalog
WHERE lineage='{LINEAGE}' OR who_name='{WHO_NAME}'
ORDER BY lineage LIMIT 50;

-- 最近导入运行
SELECT run_id, source_id, query_term, status, fetched_records, stored_records,
       started_at, completed_at
FROM ncov_source_harvest_runs
ORDER BY started_at DESC LIMIT 20;
```

---

## 流感 flu_variation

```sql
-- 精准位点（需指定reference）
SELECT reference, ref_aminoacid_site, CONCAT(ref_aminoacid,ref_aminoacid_site,aminoacid) mut,
       anti_risk, receptor_risk_23, receptor_risk_26, matrix_risk
FROM flu_variation
WHERE reference='{REF}' AND gene='{GENE}' AND ref_aminoacid_site={SITE};

-- 位点范围
WHERE reference='{REF}' AND gene='{GENE}'
  AND ref_aminoacid_site BETWEEN {S} AND {E}

-- 高抗原性风险
WHERE reference='{REF}' AND anti_risk>=2
  ORDER BY anti_risk DESC, receptor_risk_26 DESC LIMIT 50;

-- 人类受体适应高风险（α-2,6）
WHERE receptor_risk_26>=2 ORDER BY receptor_risk_26 DESC, anti_risk DESC LIMIT 50;

-- 跨亚型对比同一位点
SELECT reference, CONCAT(ref_aminoacid,ref_aminoacid_site,aminoacid) mut,
       anti_risk, receptor_risk_26
FROM flu_variation
WHERE gene='HA' AND ref_aminoacid_site={SITE} AND reference REGEXP '^A-seg4_H'
ORDER BY reference;
```

**常用 reference 速查**：
- H3N2-HA → `A-seg4_H3`；H1N1-HA → `A-seg4_H1`；H5N1-HA → `A-seg4_H5`
- H3N2-NA → `A-seg6_N2`；H1N1-NA → `A-seg6_N1`

---

## 猴痘 mpxv_variation

```sql
-- 精准位点
SELECT gene, ref_aminoacid_site, CONCAT(ref_aminoacid,ref_aminoacid_site,aminoacid) mut,
       anti_risk, matrix_risk
FROM mpxv_variation
WHERE gene='{GENE}' AND ref_aminoacid_site={SITE};

-- 位点范围
WHERE gene='{GENE}' AND ref_aminoacid_site BETWEEN {S} AND {E}

-- 高抗体逃逸（anti_risk非NULL，先确认数据量）
SELECT COUNT(*) FROM mpxv_variation WHERE anti_risk IS NOT NULL;
-- 再查高风险
WHERE anti_risk>=2 ORDER BY anti_risk DESC, matrix_risk DESC LIMIT 50;

-- 综合高风险
WHERE anti_risk>=2 AND matrix_risk>=2 ORDER BY anti_risk DESC;

-- 基因统计 Top20
SELECT gene, COUNT(*) total,
  SUM(anti_risk IS NOT NULL) has_risk_score,
  SUM(anti_risk>=2) escape2plus,
  SUM(matrix_risk>=2) easy_substitution
FROM mpxv_variation GROUP BY gene
ORDER BY escape2plus DESC LIMIT 20;
```

---

## 三库对比

```sql
-- 数据量总览
SELECT 'ncov' v, COUNT(*) n FROM ncov_amino_acid_risk UNION ALL
SELECT 'flu',  COUNT(*) FROM flu_variation UNION ALL
SELECT 'mpxv', COUNT(*) FROM mpxv_variation;

-- 高风险比例对比
SELECT 'ncov' v, COUNT(*) total,
  SUM(antibody>=2) high_risk,
  ROUND(100*SUM(antibody>=2)/COUNT(*),2) pct
FROM ncov_amino_acid_risk UNION ALL
SELECT 'flu', COUNT(*), SUM(anti_risk>=2),
  ROUND(100*SUM(anti_risk>=2)/COUNT(*),2)
FROM flu_variation UNION ALL
SELECT 'mpxv', COUNT(*), SUM(anti_risk>=2),
  ROUND(100*SUM(anti_risk>=2)/COUNT(*),2)
FROM mpxv_variation;
```

---

## 导出 CSV

```bash
mysql -u agent_virus -p'VirusAgent@2026!' virus_variation --batch --silent \
  -e "SELECT gene,aminoacid_site,ref_aminoacid,aminoacid,ace2,antibody
      FROM ncov_amino_acid_risk WHERE antibody>=2" \
  | sed 's/\t/,/g' > /tmp/ncov_escape.csv
```
