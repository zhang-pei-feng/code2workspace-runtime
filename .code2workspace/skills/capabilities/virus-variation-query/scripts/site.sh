#!/usr/bin/env bash
# 精准位点/范围查询
# 用法:
#   ./site.sh ncov  <gene> <site> [end_site]
#   ./site.sh flu   <reference> <gene> <site> [end_site]
#   ./site.sh mpxv  <gene> <site> [end_site]
# 示例:
#   ./site.sh ncov S 484 501
#   ./site.sh flu A-seg4_H3 HA 226
#   ./site.sh mpxv OPG210 100 200
set -euo pipefail
DB="virus_variation"
MYSQL=(mysql -u agent_virus -p'VirusAgent@2026!' "$DB")
V="${1:-}"; shift || true

case "$V" in
ncov)
  GENE="${1:?gene}"; S="${2:?site}"; E="${3:-$S}"
  echo "=== NCOV | $GENE | site $S~$E ==="
  "${MYSQL[@]}" -e "
    SELECT gene, aminoacid_site,
           CONCAT(ref_aminoacid,aminoacid_site,aminoacid) mut,
           ace2, antibody, aminoacid_substitution
    FROM ncov_amino_acid_risk
    WHERE gene='$GENE' AND aminoacid_site BETWEEN $S AND $E
    ORDER BY aminoacid_site, antibody DESC;" ;;
flu)
  REF="${1:?reference}"; GENE="${2:?gene}"; S="${3:?site}"; E="${4:-$S}"
  echo "=== FLU | $REF | $GENE | site $S~$E ==="
  "${MYSQL[@]}" -e "
    SELECT reference, ref_aminoacid_site,
           CONCAT(ref_aminoacid,ref_aminoacid_site,aminoacid) mut,
           anti_risk, receptor_risk_23, receptor_risk_26, matrix_risk
    FROM flu_variation
    WHERE reference='$REF' AND gene='$GENE'
      AND ref_aminoacid_site BETWEEN $S AND $E
    ORDER BY ref_aminoacid_site, anti_risk DESC;" ;;
mpxv)
  GENE="${1:?gene}"; S="${2:?site}"; E="${3:-$S}"
  echo "=== MPXV | $GENE | site $S~$E ==="
  "${MYSQL[@]}" -e "
    SELECT gene, ref_aminoacid_site,
           CONCAT(ref_aminoacid,ref_aminoacid_site,aminoacid) mut,
           anti_risk, matrix_risk
    FROM mpxv_variation
    WHERE gene='$GENE' AND ref_aminoacid_site BETWEEN $S AND $E
    ORDER BY ref_aminoacid_site, anti_risk DESC;" ;;
*)
  echo "用法: $0 ncov <gene> <site> [end]"
  echo "      $0 flu <ref> <gene> <site> [end]"
  echo "      $0 mpxv <gene> <site> [end]"
  exit 1 ;;
esac
