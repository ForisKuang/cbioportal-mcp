#!/usr/bin/env bash
# Verify that the precomputed frequency views (sql/6-precomputed-mutation-frequency.sql)
# return exactly what their *_live twins (sql/4-mutation-frequency-views.sql) return,
# for every loaded preference × a sample of genes / alterations / studies.
#
# Read-only: runs SELECTs only. Run it after scripts/apply_sql.sh (or after the
# daily clone CronJob) against the same database. Exits non-zero on any mismatch.
#
# Required env (same as apply_sql.sh; any user with SELECT on the DB works):
#   CLICKHOUSE_HOST
#   CLICKHOUSE_DATABASE
#   CLICKHOUSE_ADMIN_USER / CLICKHOUSE_ADMIN_PASSWORD
#     (or CLICKHOUSE_USER / CLICKHOUSE_PASSWORD — read-only is enough, but the
#      user needs CREATE TEMPORARY TABLE for IN-subqueries on some deployments)
#
# Optional env:
#   CLICKHOUSE_PORT     default 9440
#   CLICKHOUSE_SECURE   default true
#   PARITY_GENES        default "TP53 KRAS PIK3CA EGFR MYC CDKN2A ERBB2 TTN"
#   PARITY_STUDIES      default: 3 studies from precomputed_frequency_studies
#
# Note: live views recompute from raw tables, so this takes a while on big
# cohorts (e.g. all_studies_non_redundant). That cost is the point of the precompute.

set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
CH_USER="${CLICKHOUSE_ADMIN_USER:-${CLICKHOUSE_USER:-}}"
CH_PASSWORD="${CLICKHOUSE_ADMIN_PASSWORD:-${CLICKHOUSE_PASSWORD:-}}"
: "${CH_USER:?CLICKHOUSE_ADMIN_USER or CLICKHOUSE_USER is required}"
CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-9440}"
CLICKHOUSE_SECURE="${CLICKHOUSE_SECURE:-true}"
PARITY_GENES="${PARITY_GENES:-TP53 KRAS PIK3CA EGFR MYC CDKN2A ERBB2 TTN}"

command -v clickhouse-client >/dev/null 2>&1 || {
    echo "error: clickhouse-client not on PATH" >&2
    exit 1
}

secure_flag=()
if [[ "$CLICKHOUSE_SECURE" == "true" || "$CLICKHOUSE_SECURE" == "1" ]]; then
    secure_flag+=(--secure)
fi

ch() {
    clickhouse-client \
        --host "$CLICKHOUSE_HOST" --port "$CLICKHOUSE_PORT" "${secure_flag[@]}" \
        --user "$CH_USER" --password "$CH_PASSWORD" \
        --database "$CLICKHOUSE_DATABASE" \
        --query "$1"
}

# Identifiers are interpolated into SQL; only allow the characters they use.
valid() { [[ "$1" =~ ^[A-Za-z0-9_.-]+$ ]]; }

failures=0
checks=0

compare() {
    local pre="$1" live="$2"
    checks=$((checks + 1))
    local diff
    diff=$(ch "SELECT
        (SELECT groupArray(tuple(*)) FROM (SELECT * FROM $pre ORDER BY ALL))
     != (SELECT groupArray(tuple(*)) FROM (SELECT * FROM $live ORDER BY ALL))")
    if [[ "$diff" != "0" ]]; then
        echo "MISMATCH  $pre"
        failures=$((failures + 1))
    else
        echo "ok        $pre"
    fi
}

preferences=$(ch "SELECT DISTINCT preference_name FROM cancer_study_query_preferences ORDER BY 1")
studies="${PARITY_STUDIES:-$(ch "SELECT cancer_study_identifier FROM precomputed_frequency_studies ORDER BY cityHash64(cancer_study_identifier) LIMIT 3")}"

echo "Target: $CH_USER@$CLICKHOUSE_HOST:$CLICKHOUSE_PORT/$CLICKHOUSE_DATABASE"
echo "Precompute built at: $(ch "SELECT max(built_at) FROM precomputed_frequency_studies")"
echo

for pref in $preferences; do
    valid "$pref" || { echo "skip invalid preference name: $pref"; continue; }
    compare "top_mutated_genes_in_cohort(preference='$pref', top_n=50)" \
            "top_mutated_genes_in_cohort_live(preference='$pref', top_n=50)"
    for gene in $PARITY_GENES; do
        valid "$gene" || continue
        compare "gene_mutation_frequency_by_cancer_type(preference='$pref', gene='$gene')" \
                "gene_mutation_frequency_by_cancer_type_live(preference='$pref', gene='$gene')"
        for alt in amplification deep_deletion structural_variant; do
            compare "gene_alteration_frequency_by_cancer_type(preference='$pref', gene='$gene', alteration='$alt')" \
                    "gene_alteration_frequency_by_cancer_type_live(preference='$pref', gene='$gene', alteration='$alt')"
        done
    done
done

study_array=""
for study in $studies; do
    valid "$study" || continue
    study_array+="${study_array:+,}'$study'"
    for gene in $PARITY_GENES; do
        valid "$gene" || continue
        compare "gene_mutation_frequency_in_study(study='$study', gene='$gene')" \
                "gene_mutation_frequency_in_study_live(study='$study', gene='$gene')"
    done
done
if [[ -n "$study_array" ]]; then
    for gene in $PARITY_GENES; do
        valid "$gene" || continue
        compare "gene_mutation_frequency_in_studies(studies=[$study_array], gene='$gene')" \
                "gene_mutation_frequency_in_studies_live(studies=[$study_array], gene='$gene')"
    done
fi

echo
echo "$checks checks, $failures mismatches"
[[ "$failures" -eq 0 ]]
