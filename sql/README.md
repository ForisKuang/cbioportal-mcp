# LLM-prep SQL

The daily clone CronJob (see `knowledgesystems-k8s-deployment`) executes every
`*.sql` file in this directory **in numeric order** against the freshly-cloned
LLM database. The files re-shape and annotate the schema so the cBioPortal
MCP agent can reason about it.

## Files

| Order | File | Scope |
|-------|------|-------|
| 0 | `0-cleanup-for-llm.sql` | Drop columns that mislead the agent (e.g. `sample.sample_type`). |
| 1 | `1-add-column-comments.sql` | Attach human-readable `COMMENT`s so the agent can self-introspect column meaning. |
| 2 | `2-add-oncotree-fields.sql` | Add OncoTree fields to `type_of_cancer` (auto-generated). |
| 3 | `3-add-cancer-study-query-preferences.sql` | Creates `cancer_study_query_preferences` table + pattern-detected preferences (currently `pan_cancer_tcga`). |
| 4 | `4-mutation-frequency-views.sql` | The two coverage building-block views (`mutation_panel_gene_coverage`, `mutation_wes_coverage`) and the **live** (recompute-on-read) frequency recipes, suffixed `_live` (`gene_mutation_frequency_by_cancer_type_live`, `top_mutated_genes_in_cohort_live`, ...). These are the reference definitions of every frequency number. Handles the "WES is not in `gene_panel`" trap that produces >100% frequencies. See `cbioportal://mutation-frequency-guide`. |
| 5 | `5-gene-expression-views.sql` | Gene-expression / copy-number-value / methylation views, backed by `genetic_alteration_derived`. Currently `gene_pair_coexpression(study, gene_a, gene_b, profile_type)` for Spearman correlation between two genes. See `cbioportal://gene-expression-guide`. |
| 6 | `6-precomputed-mutation-frequency.sql` | Rebuilds the per-study `precomputed_*` aggregate tables (altered / profiled sample counts per study × CANCER_TYPE × gene, plus per-study top-N inputs) and defines the agent-facing frequency views (`gene_mutation_frequency_by_cancer_type`, `gene_mutation_frequency_in_study(ies)`, `gene_alteration_frequency_by_cancer_type`, `top_mutated_genes_in_cohort`) over them. Studies missing from the `precomputed_frequency_studies` manifest fall back to the live recipe inside the same view. See "Refreshing the precomputed aggregates" below. |

Everything under `sql/` directly is **portable** — works against any cBioPortal deployment. Deployment-specific SQL lives under `sql/portal-specific/<portal-name>/`:

| Path | Scope |
|------|-------|
| `sql/portal-specific/public-portal/0-preferences.sql` | Public cBioPortal (`cbioportal.org`). Loads `all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes` preferences. All INSERTs gated on `cancer_study` existence, so on other deployments this is a no-op rather than an error. |

`apply_sql.sh` and the daily clone cron apply the portable files first (in numeric order), then iterate every subdirectory of `portal-specific/`. A deployer image can ship multiple subdirs if it needs to, but typically only contains the one for that portal.

## Applying these files manually

For a one-off apply outside the daily clone CronJob (e.g. you just edited
`sql/5-*.sql` and want to test against a prepped database without re-cloning
the data), use `scripts/apply_sql.sh`:

```bash
export CLICKHOUSE_HOST=...
export CLICKHOUSE_DATABASE=cbioportal_public_librechat_blue   # your prepped DB
export CLICKHOUSE_ADMIN_USER=librechat_admin                   # NOT the MCP SELECT-only user
export CLICKHOUSE_ADMIN_PASSWORD=...
./scripts/apply_sql.sh
```

Requires the `clickhouse-client` binary on `PATH`. The script uses dedicated
`CLICKHOUSE_ADMIN_USER` / `CLICKHOUSE_ADMIN_PASSWORD` env vars instead of the
MCP server's `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD`, so the SELECT-only
runtime user never sees DDL credentials.

## Refreshing the precomputed aggregates

**When:** every time `6-precomputed-mutation-frequency.sql` is applied — i.e.
automatically by the daily clone CronJob right after it clones the data,
and by `scripts/apply_sql.sh` for manual applies. There is no separate
refresh job. If the underlying data is ever changed *without* re-running
the SQL files (e.g. a study is loaded by hand), re-run `apply_sql.sh`, or
just that one file:

```bash
clickhouse-client ... --multiquery --queries-file sql/6-precomputed-mutation-frequency.sql
```

Until then the views stay correct: studies not in `precomputed_frequency_studies`
are computed live. A study whose data *changed* in place (same id) would be
served stale, so always re-apply after an in-place reload.

**How:** each `precomputed_*` table is built into `<name>__staging` with an
`INSERT … SELECT`, then swapped in with `EXCHANGE TABLES` and the old copy
dropped. Re-running is idempotent (each run rebuilds from scratch), readers
never see a half-built table, and a failed run leaves the previous tables in
place. The manifest table is swapped last. `EXCHANGE TABLES` needs an
Atomic / Replicated / Shared database engine (the default everywhere,
including ClickHouse Cloud).

**Why not a refreshable materialized view (`REFRESH EVERY …`):** the data in
these databases only changes when the clone CronJob re-creates them, and that
job already runs the SQL files immediately afterwards. A timer-based refresh
would either recompute unchanged data on a schedule or lag the clone by up to
one interval (serving aggregates from the previous clone); tying the rebuild
to the apply step makes it exactly as fresh as the data with no extra moving
parts. It also keeps the build in plain `INSERT … SELECT`, which
`clickhouse local` can execute for the parity tests.

**Why per study:** `sample_unique_id` is study-prefixed, so per-study distinct
counts add up exactly across any set of studies. One set of tables serves
every preference — including portal-specific preferences, which are inserted
*after* file 6 runs — and every ad-hoc study list.

**Verifying:** `scripts/check_precomputed_parity.sh` (read-only) compares the
precomputed views against their `*_live` twins for every loaded preference
and a sample of genes / studies, and exits non-zero on any difference.
`tests/test_precomputed_parity.py` runs the same comparison exhaustively on
synthetic data with `clickhouse local` (skipped if `clickhouse` isn't
installed). If you change a counting rule, change it in both file 4
(`*_live`) and file 6, then run both.

## Deploying for a non-public portal

Two options for adding your own preferences:

1. **Add `sql/portal-specific/<your-deployment-name>/0-preferences.sql`** alongside
   the existing `public-portal/` subdir. `apply_sql.sh` and the cron will
   iterate every subdir of `portal-specific/`, so your file gets picked up
   automatically. Leaving `public-portal/` in place is harmless — its INSERTs
   are existence-gated and produce zero rows on databases without the
   cBioPortal-public studies.

2. **Or remove the `public-portal/` subdir entirely** from your image / mount
   if you'd rather not even apply its (no-op) INSERTs.

## Conventions for portal-specific preference files

- Live under `sql/portal-specific/<portal-name>/`. The portal-name is a
  free-form slug — pick something that identifies your deployment.
- Numeric prefix within each subdir so order is intrinsic. Start at `0-`.
- Every `INSERT INTO cancer_study_query_preferences` must gate on
  `WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM cancer_study)`
  (or equivalent) so the file is harmless on databases that don't have your
  studies.
- One `preference_name` per query-intent, lower_snake_case. Document its
  purpose in the `notes` column so the agent can self-explain to users.
- The agent discovers available preferences via
  `SELECT DISTINCT preference_name FROM cancer_study_query_preferences` —
  no hardcoded list anywhere outside SQL.
