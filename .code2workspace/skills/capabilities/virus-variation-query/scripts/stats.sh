#!/usr/bin/env bash
# 基因维度统计汇总
# 用法: ./stats.sh [ncov|flu|mpxv|all]
set -euo pipefail
DB="virus_variation"
MYSQL=(mysql -u agent_virus -p'VirusAgent@2026!' "$DB")
V="${1:-all}"

ncov() {
  echo "=== NCOV 基因统计 ==="
  "${MYSQL[@]}" -e "
    SELECT gene,
      COUNT(*) total,
      COUNT(DISTINCT aminoacid_site) sites,
      SUM(antibody>=2) escape_2plus,
      SUM(antibody=3)  escape_3,
      SUM(ace2>=2)     ace2_impact,
      SUM(antibody>=2 AND aminoacid_substitution>=2) high_combined
    FROM ncov_amino_acid_risk
    GROUP BY gene ORDER BY escape_2plus DESC;"
}
flu() {
  echo "=== FLU 型别×基因统计 (Top 20) ==="
  "${MYSQL[@]}" -e "
    SELECT reference, gene,
      COUNT(*) total,
      COUNT(DISTINCT ref_aminoacid_site) sites,
      SUM(anti_risk>=2)        escape_2plus,
      SUM(receptor_risk_26>=2) rcpt26_risk,
      SUM(receptor_risk_23>=2) rcpt23_risk
    FROM flu_variation
    GROUP BY reference, gene
    ORDER BY escape_2plus DESC LIMIT 20;"
}
mpxv() {
  echo "=== MPXV 基因统计 (Top 20) ==="
  "${MYSQL[@]}" -e "
    SELECT gene,
      COUNT(*) total,
      COUNT(DISTINCT ref_aminoacid_site) sites,
      SUM(anti_risk IS NOT NULL)          has_score,
      SUM(anti_risk>=2)                   escape_2plus,
      SUM(anti_risk>=2 AND matrix_risk>=2) high_combined
    FROM mpxv_variation
    GROUP BY gene
    ORDER BY escape_2plus DESC LIMIT 20;"
}

case "$V" in
  ncov) ncov ;;
  flu)  flu  ;;
  mpxv) mpxv ;;
  all)  ncov; echo; flu; echo; mpxv ;;
  *) echo "用法: $0 [ncov|flu|mpxv|all]"; exit 1 ;;
esac
