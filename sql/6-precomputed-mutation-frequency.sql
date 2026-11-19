-- ============================================================================
-- Precomputed mutation / alteration frequency aggregates
-- ============================================================================
-- The counting rules here must
-- match the *_live views in sql/4-mutation-frequency-views.sql exactly;
-- scripts/check_precomputed_parity.sh (and tests/test_precomputed_parity.py
-- against synthetic data) verify that they do.
--
-- WHY: the agent-facing frequency views used to recompute event ×
-- clinical × gene-panel-coverage joins with DISTINCT counts on every
-- call. The data only changes when the daily clone CronJob re-clones the
-- database, and that same job applies every sql/*.sql file right after the
-- clone. So the aggregates are rebuilt here, once per clone, by
-- idempotent INSERT … SELECT into a staging table followed by
-- EXCHANGE TABLES (atomic swap). See sql/README.md "Refreshing the
-- precomputed aggregates" for why this is used instead of a refreshable
-- materialized view.
--
-- SHAPE: everything is aggregated PER STUDY (not per preference).
-- `sample_unique_id` is study-prefixed, so a sample belongs to exactly one
-- study and COUNT(DISTINCT sample) over a set of studies equals the SUM
-- of the per-study counts. That makes one set of per-study tables serve
-- every preference, every ad-hoc study list, and preference rows inserted
-- after this file runs (sql/portal-specific/* is applied after sql/6-*).
--
-- WES / named-panel overlap: some samples carry both a WES row and a
-- named-panel row for the same alteration_type. The per-cancer-type
-- recipe counts DISTINCT samples over (panel ∪ WES), so the panel table
-- below EXCLUDES samples that also have a WES row for that alteration
-- type; WES + panel then add up exactly. The top-N recipe (like the
-- backend) adds WES and panel counts without de-duplication, so its
-- panel table does NOT exclude them.
--
-- FALLBACK: `precomputed_frequency_studies` lists the studies the
-- aggregates were built from. The agent-facing views read the
-- precomputed tables for those studies and run the live recipe for any
-- cohort study that is missing from it (e.g. a study loaded after the
-- last rebuild). With a fresh rebuild that live branch reads nothing.
--
-- Tables (all rebuilt on every apply):
--   precomputed_frequency_studies
--       (cancer_study_identifier, built_at) — coverage manifest.
--   precomputed_alteration_altered_by_cancer_type
--       (alteration, cancer_study_identifier, cancer_type, hugo_gene_symbol,
--        altered_samples)
--   precomputed_alteration_panel_profiled_by_cancer_type
--       (alteration_type, cancer_study_identifier, cancer_type,
--        hugo_gene_symbol, profiled_samples) — named panels, WES samples excluded
--   precomputed_alteration_wes_profiled_by_cancer_type
--       (alteration_type, cancer_study_identifier, cancer_type, profiled_samples)
--   precomputed_mutation_altered_by_study
--       (cancer_study_identifier, hugo_gene_symbol, altered_samples,
--        total_mutation_events) — no CANCER_TYPE join (top-N recipe)
--   precomputed_mutation_panel_profiled_by_study
--       (cancer_study_identifier, hugo_gene_symbol, profiled_samples)
--   precomputed_mutation_wes_profiled_by_study
--       (cancer_study_identifier, profiled_samples)
--
-- Agent-facing views (re)defined at the bottom of this file, same names,
-- parameters and output columns as before:
--   gene_mutation_frequency_by_cancer_type(preference, gene)
--   gene_mutation_frequency_in_study(study, gene)
--   gene_mutation_frequency_in_studies(studies, gene)
--   gene_alteration_frequency_by_cancer_type(preference, gene, alteration)
--   top_mutated_genes_in_cohort(preference, top_n)
-- ============================================================================

-- ============================================================================
-- Build: every table follows the same idempotent pattern
--   1. DROP + CREATE <name>__staging
--   2. INSERT INTO <name>__staging SELECT …
--   3. CREATE TABLE IF NOT EXISTS <name> AS <name>__staging   (first run only)
--   4. EXCHANGE TABLES <name>__staging AND <name>             (atomic swap)
--   5. DROP the staging table (now holding the previous build)
-- Re-running the file rebuilds every table from scratch; a failed run
-- leaves the previously published tables untouched (only __staging is
-- half-built). Requires an Atomic / Replicated / Shared database engine
-- for EXCHANGE TABLES (the default engine on every supported ClickHouse
-- version and on ClickHouse Cloud).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- precomputed_alteration_altered_by_cancer_type
-- ----------------------------------------------------------------------------
-- Numerator for the per-cancer-type recipes. One row per
-- (alteration, study, CANCER_TYPE, gene) with ≥ 1 altered sample.
-- alteration token ↔ event filter is the same mapping as
-- gene_alteration_frequency_by_cancer_type_live; the four filters are
-- disjoint, so every event counts toward at most one token.
DROP TABLE IF EXISTS precomputed_alteration_altered_by_cancer_type__staging;

CREATE TABLE precomputed_alteration_altered_by_cancer_type__staging (
    alteration LowCardinality(String)              COMMENT 'mutation | amplification | deep_deletion | structural_variant',
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    cancer_type String                             COMMENT 'Per-sample CANCER_TYPE from clinical_data_derived',
    hugo_gene_symbol String                        COMMENT 'HUGO gene symbol',
    altered_samples UInt64                         COMMENT 'Distinct samples with >= 1 on-panel event of this alteration kind. Additive across studies.'
)
ENGINE = MergeTree
ORDER BY (alteration, hugo_gene_symbol, cancer_study_identifier, cancer_type)
COMMENT 'Precomputed per-study altered-sample counts by CANCER_TYPE. Backs gene_*_frequency_* views; prefer those views. Rebuilt on every data refresh.';

-- One INSERT per alteration kind (the four event filters are disjoint) to
-- bound peak memory: each pass de-duplicates only its own events.
INSERT INTO precomputed_alteration_altered_by_cancer_type__staging
SELECT 'mutation' AS alteration, e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol, count() AS altered_samples
FROM (
    SELECT cancer_study_identifier, hugo_gene_symbol, sample_unique_id
    FROM genomic_event_derived
    WHERE variant_type = 'mutation' AND mutation_status != 'UNCALLED'
      AND off_panel = 0
    GROUP BY cancer_study_identifier, hugo_gene_symbol, sample_unique_id
) e
JOIN (
    SELECT cancer_study_identifier, sample_unique_id, attribute_value AS cancer_type
    FROM clinical_data_derived
    WHERE attribute_name = 'CANCER_TYPE'
) sct ON e.cancer_study_identifier = sct.cancer_study_identifier
     AND e.sample_unique_id = sct.sample_unique_id
GROUP BY e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol;

INSERT INTO precomputed_alteration_altered_by_cancer_type__staging
SELECT 'amplification' AS alteration, e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol, count() AS altered_samples
FROM (
    SELECT cancer_study_identifier, hugo_gene_symbol, sample_unique_id
    FROM genomic_event_derived
    WHERE variant_type = 'cna' AND cna_alteration = 2
      AND off_panel = 0
    GROUP BY cancer_study_identifier, hugo_gene_symbol, sample_unique_id
) e
JOIN (
    SELECT cancer_study_identifier, sample_unique_id, attribute_value AS cancer_type
    FROM clinical_data_derived
    WHERE attribute_name = 'CANCER_TYPE'
) sct ON e.cancer_study_identifier = sct.cancer_study_identifier
     AND e.sample_unique_id = sct.sample_unique_id
GROUP BY e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol;

INSERT INTO precomputed_alteration_altered_by_cancer_type__staging
SELECT 'deep_deletion' AS alteration, e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol, count() AS altered_samples
FROM (
    SELECT cancer_study_identifier, hugo_gene_symbol, sample_unique_id
    FROM genomic_event_derived
    WHERE variant_type = 'cna' AND cna_alteration = -2
      AND off_panel = 0
    GROUP BY cancer_study_identifier, hugo_gene_symbol, sample_unique_id
) e
JOIN (
    SELECT cancer_study_identifier, sample_unique_id, attribute_value AS cancer_type
    FROM clinical_data_derived
    WHERE attribute_name = 'CANCER_TYPE'
) sct ON e.cancer_study_identifier = sct.cancer_study_identifier
     AND e.sample_unique_id = sct.sample_unique_id
GROUP BY e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol;

INSERT INTO precomputed_alteration_altered_by_cancer_type__staging
SELECT 'structural_variant' AS alteration, e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol, count() AS altered_samples
FROM (
    SELECT cancer_study_identifier, hugo_gene_symbol, sample_unique_id
    FROM genomic_event_derived
    WHERE variant_type = 'structural_variant'
      AND off_panel = 0
    GROUP BY cancer_study_identifier, hugo_gene_symbol, sample_unique_id
) e
JOIN (
    SELECT cancer_study_identifier, sample_unique_id, attribute_value AS cancer_type
    FROM clinical_data_derived
    WHERE attribute_name = 'CANCER_TYPE'
) sct ON e.cancer_study_identifier = sct.cancer_study_identifier
     AND e.sample_unique_id = sct.sample_unique_id
GROUP BY e.cancer_study_identifier, sct.cancer_type, e.hugo_gene_symbol;

CREATE TABLE IF NOT EXISTS precomputed_alteration_altered_by_cancer_type AS precomputed_alteration_altered_by_cancer_type__staging;
EXCHANGE TABLES precomputed_alteration_altered_by_cancer_type__staging AND precomputed_alteration_altered_by_cancer_type;
DROP TABLE IF EXISTS precomputed_alteration_altered_by_cancer_type__staging;

-- ----------------------------------------------------------------------------
-- precomputed_alteration_panel_profiled_by_cancer_type
-- ----------------------------------------------------------------------------
-- Named-panel half of the per-cancer-type denominator. Samples that ALSO
-- have a WES row for the same alteration_type are excluded here (they are
-- counted once, in the WES table), so panel + WES = DISTINCT(panel ∪ WES).
--
-- Computed via each sample's SET of panels rather than expanding
-- sample × gene directly: samples are first counted per distinct panel
-- set, then each panel set is expanded to the DISTINCT genes its panels
-- list. Exact (a gene listed by two of a sample's panels counts once) and
-- far smaller than a sample × gene expansion for large panel cohorts.
DROP TABLE IF EXISTS precomputed_alteration_panel_profiled_by_cancer_type__staging;

CREATE TABLE precomputed_alteration_panel_profiled_by_cancer_type__staging (
    alteration_type LowCardinality(String)         COMMENT 'MUTATION_EXTENDED | COPY_NUMBER_ALTERATION | STRUCTURAL_VARIANT',
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    cancer_type String                             COMMENT 'Per-sample CANCER_TYPE from clinical_data_derived',
    hugo_gene_symbol String                        COMMENT 'HUGO gene symbol listed by the sample''s named gene panel(s)',
    profiled_samples UInt64                        COMMENT 'Distinct non-WES samples on a named panel listing this gene. Add the WES table for the full denominator.'
)
ENGINE = MergeTree
ORDER BY (alteration_type, hugo_gene_symbol, cancer_study_identifier, cancer_type)
COMMENT 'Precomputed named-panel half of the profiled denominator (WES samples excluded). Backs gene_*_frequency_* views; prefer those views.';

INSERT INTO precomputed_alteration_panel_profiled_by_cancer_type__staging
WITH sample_panels AS (
    SELECT alteration_type,
           cancer_study_identifier,
           sample_unique_id,
           arraySort(groupUniqArray(gene_panel_id)) AS panels
    FROM sample_to_gene_panel_derived
    WHERE alteration_type IN ('MUTATION_EXTENDED', 'COPY_NUMBER_ALTERATION', 'STRUCTURAL_VARIANT')
    GROUP BY alteration_type, cancer_study_identifier, sample_unique_id
    HAVING NOT has(panels, 'WES')
),
panel_set_counts AS (
    SELECT sp.alteration_type, sp.cancer_study_identifier, sct.cancer_type, sp.panels, count() AS n
    FROM sample_panels sp
    JOIN (
        SELECT cancer_study_identifier, sample_unique_id, attribute_value AS cancer_type
        FROM clinical_data_derived
        WHERE attribute_name = 'CANCER_TYPE'
    ) sct ON sp.cancer_study_identifier = sct.cancer_study_identifier
         AND sp.sample_unique_id = sct.sample_unique_id
    GROUP BY sp.alteration_type, sp.cancer_study_identifier, sct.cancer_type, sp.panels
),
panel_genes AS (
    SELECT DISTINCT gp.stable_id AS gene_panel_id, g.hugo_gene_symbol
    FROM gene_panel gp
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
),
panel_set_genes AS (
    SELECT DISTINCT psc.alteration_type, psc.cancer_study_identifier, psc.cancer_type,
           psc.panels, psc.n, pg.hugo_gene_symbol
    FROM panel_set_counts psc
    ARRAY JOIN psc.panels AS panel_id
    JOIN panel_genes pg ON panel_id = pg.gene_panel_id
)
SELECT alteration_type, cancer_study_identifier, cancer_type, hugo_gene_symbol, sum(n) AS profiled_samples
FROM panel_set_genes
GROUP BY alteration_type, cancer_study_identifier, cancer_type, hugo_gene_symbol;

CREATE TABLE IF NOT EXISTS precomputed_alteration_panel_profiled_by_cancer_type AS precomputed_alteration_panel_profiled_by_cancer_type__staging;
EXCHANGE TABLES precomputed_alteration_panel_profiled_by_cancer_type__staging AND precomputed_alteration_panel_profiled_by_cancer_type;
DROP TABLE IF EXISTS precomputed_alteration_panel_profiled_by_cancer_type__staging;

-- ----------------------------------------------------------------------------
-- precomputed_alteration_wes_profiled_by_cancer_type
-- ----------------------------------------------------------------------------
-- WES half of the per-cancer-type denominator: WES samples are profiled
-- for every gene, so this is one number per (alteration_type, study,
-- CANCER_TYPE), added to every gene's panel count.
DROP TABLE IF EXISTS precomputed_alteration_wes_profiled_by_cancer_type__staging;

CREATE TABLE precomputed_alteration_wes_profiled_by_cancer_type__staging (
    alteration_type LowCardinality(String)         COMMENT 'MUTATION_EXTENDED | COPY_NUMBER_ALTERATION | STRUCTURAL_VARIANT',
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    cancer_type String                             COMMENT 'Per-sample CANCER_TYPE from clinical_data_derived',
    profiled_samples UInt64                        COMMENT 'Distinct WES samples (profiled for every gene). Applies to all genes.'
)
ENGINE = MergeTree
ORDER BY (alteration_type, cancer_study_identifier, cancer_type)
COMMENT 'Precomputed WES half of the profiled denominator (applies to every gene). Backs gene_*_frequency_* views; prefer those views.';

INSERT INTO precomputed_alteration_wes_profiled_by_cancer_type__staging
SELECT w.alteration_type, w.cancer_study_identifier, sct.cancer_type, count() AS profiled_samples
FROM (
    SELECT DISTINCT alteration_type, cancer_study_identifier, sample_unique_id
    FROM sample_to_gene_panel_derived
    WHERE gene_panel_id = 'WES'
      AND alteration_type IN ('MUTATION_EXTENDED', 'COPY_NUMBER_ALTERATION', 'STRUCTURAL_VARIANT')
) w
JOIN (
    SELECT cancer_study_identifier, sample_unique_id, attribute_value AS cancer_type
    FROM clinical_data_derived
    WHERE attribute_name = 'CANCER_TYPE'
) sct ON w.cancer_study_identifier = sct.cancer_study_identifier
     AND w.sample_unique_id = sct.sample_unique_id
GROUP BY w.alteration_type, w.cancer_study_identifier, sct.cancer_type;

CREATE TABLE IF NOT EXISTS precomputed_alteration_wes_profiled_by_cancer_type AS precomputed_alteration_wes_profiled_by_cancer_type__staging;
EXCHANGE TABLES precomputed_alteration_wes_profiled_by_cancer_type__staging AND precomputed_alteration_wes_profiled_by_cancer_type;
DROP TABLE IF EXISTS precomputed_alteration_wes_profiled_by_cancer_type__staging;

-- ----------------------------------------------------------------------------
-- precomputed_mutation_altered_by_study  (top-N recipe numerator)
-- ----------------------------------------------------------------------------
-- No CANCER_TYPE join: the top-N recipe counts every cohort sample,
-- including samples without a CANCER_TYPE attribute.
DROP TABLE IF EXISTS precomputed_mutation_altered_by_study__staging;

CREATE TABLE precomputed_mutation_altered_by_study__staging (
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    hugo_gene_symbol String                        COMMENT 'HUGO gene symbol',
    altered_samples UInt64                         COMMENT 'Distinct samples with >= 1 on-panel, non-UNCALLED mutation. Additive across studies.',
    total_mutation_events UInt64                   COMMENT 'Number of on-panel, non-UNCALLED mutation events. Additive across studies.'
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, hugo_gene_symbol)
COMMENT 'Precomputed per-study mutated-sample counts per gene. Backs top_mutated_genes_in_cohort; prefer that view.';

INSERT INTO precomputed_mutation_altered_by_study__staging
SELECT cancer_study_identifier,
       hugo_gene_symbol,
       COUNT(DISTINCT sample_unique_id) AS altered_samples,
       count() AS total_mutation_events
FROM genomic_event_derived
WHERE variant_type = 'mutation'
  AND mutation_status != 'UNCALLED'
  AND off_panel = 0
GROUP BY cancer_study_identifier, hugo_gene_symbol;

CREATE TABLE IF NOT EXISTS precomputed_mutation_altered_by_study AS precomputed_mutation_altered_by_study__staging;
EXCHANGE TABLES precomputed_mutation_altered_by_study__staging AND precomputed_mutation_altered_by_study;
DROP TABLE IF EXISTS precomputed_mutation_altered_by_study__staging;

-- ----------------------------------------------------------------------------
-- precomputed_mutation_panel_profiled_by_study  (top-N denominator, panel part)
-- ----------------------------------------------------------------------------
-- Same panel-set expansion as above, MUTATION_EXTENDED only, no
-- CANCER_TYPE join and WITHOUT excluding WES samples (the top-N recipe
-- adds WES + panel counts, matching the backend).
DROP TABLE IF EXISTS precomputed_mutation_panel_profiled_by_study__staging;

CREATE TABLE precomputed_mutation_panel_profiled_by_study__staging (
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    hugo_gene_symbol String                        COMMENT 'HUGO gene symbol listed by the sample''s named gene panel(s)',
    profiled_samples UInt64                        COMMENT 'Distinct samples on a named mutation panel listing this gene.'
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, hugo_gene_symbol)
COMMENT 'Precomputed per-study named-panel mutation coverage per gene. Backs top_mutated_genes_in_cohort; prefer that view.';

INSERT INTO precomputed_mutation_panel_profiled_by_study__staging
WITH sample_panels AS (
    SELECT cancer_study_identifier,
           sample_unique_id,
           arraySort(groupUniqArray(gene_panel_id)) AS panels
    FROM sample_to_gene_panel_derived
    WHERE alteration_type = 'MUTATION_EXTENDED'
    GROUP BY cancer_study_identifier, sample_unique_id
),
panel_set_counts AS (
    SELECT cancer_study_identifier, panels, count() AS n
    FROM sample_panels
    GROUP BY cancer_study_identifier, panels
),
panel_genes AS (
    SELECT DISTINCT gp.stable_id AS gene_panel_id, g.hugo_gene_symbol
    FROM gene_panel gp
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
),
panel_set_genes AS (
    SELECT DISTINCT psc.cancer_study_identifier, psc.panels, psc.n, pg.hugo_gene_symbol
    FROM panel_set_counts psc
    ARRAY JOIN psc.panels AS panel_id
    JOIN panel_genes pg ON panel_id = pg.gene_panel_id
)
SELECT cancer_study_identifier, hugo_gene_symbol, sum(n) AS profiled_samples
FROM panel_set_genes
GROUP BY cancer_study_identifier, hugo_gene_symbol;

CREATE TABLE IF NOT EXISTS precomputed_mutation_panel_profiled_by_study AS precomputed_mutation_panel_profiled_by_study__staging;
EXCHANGE TABLES precomputed_mutation_panel_profiled_by_study__staging AND precomputed_mutation_panel_profiled_by_study;
DROP TABLE IF EXISTS precomputed_mutation_panel_profiled_by_study__staging;

-- ----------------------------------------------------------------------------
-- precomputed_mutation_wes_profiled_by_study  (top-N denominator, WES part)
-- ----------------------------------------------------------------------------
DROP TABLE IF EXISTS precomputed_mutation_wes_profiled_by_study__staging;

CREATE TABLE precomputed_mutation_wes_profiled_by_study__staging (
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    profiled_samples UInt64                        COMMENT 'Distinct WES mutation-profiled samples (profiled for every gene).'
)
ENGINE = MergeTree
ORDER BY cancer_study_identifier
COMMENT 'Precomputed per-study WES mutation coverage. Backs top_mutated_genes_in_cohort; prefer that view.';

INSERT INTO precomputed_mutation_wes_profiled_by_study__staging
SELECT cancer_study_identifier, COUNT(DISTINCT sample_unique_id) AS profiled_samples
FROM sample_to_gene_panel_derived
WHERE alteration_type = 'MUTATION_EXTENDED'
  AND gene_panel_id = 'WES'
GROUP BY cancer_study_identifier;

CREATE TABLE IF NOT EXISTS precomputed_mutation_wes_profiled_by_study AS precomputed_mutation_wes_profiled_by_study__staging;
EXCHANGE TABLES precomputed_mutation_wes_profiled_by_study__staging AND precomputed_mutation_wes_profiled_by_study;
DROP TABLE IF EXISTS precomputed_mutation_wes_profiled_by_study__staging;

-- ----------------------------------------------------------------------------
-- precomputed_frequency_studies  (coverage manifest — built LAST)
-- ----------------------------------------------------------------------------
-- Every study present when the aggregates above were built. Built after
-- the data tables so a failed rebuild never advertises coverage that the
-- data tables don't have. Studies NOT listed here are computed live by
-- the views below.
DROP TABLE IF EXISTS precomputed_frequency_studies__staging;

CREATE TABLE precomputed_frequency_studies__staging (
    cancer_study_identifier String COMMENT 'Study whose frequencies are served from the precomputed_* tables',
    built_at DateTime              COMMENT 'When the precomputed_* aggregates were last rebuilt'
)
ENGINE = MergeTree
ORDER BY cancer_study_identifier
COMMENT 'Studies covered by the precomputed_* frequency tables. Cohort studies missing here are computed live.';

INSERT INTO precomputed_frequency_studies__staging
SELECT DISTINCT cancer_study_identifier, now()
FROM cancer_study;

CREATE TABLE IF NOT EXISTS precomputed_frequency_studies AS precomputed_frequency_studies__staging;
EXCHANGE TABLES precomputed_frequency_studies__staging AND precomputed_frequency_studies;
DROP TABLE IF EXISTS precomputed_frequency_studies__staging;

-- ============================================================================
-- Agent-facing views over the precomputed tables (+ live fallback)
-- ============================================================================
-- ClickHouse parameterized views (23.1+) are the documented equivalent of
-- stored procedures for reusable parameterized SELECTs — see
-- https://clickhouse.com/docs/guides/developer/stored-procedures-and-prepared-statements
-- Results are numerically identical to the *_live views in
-- sql/4-mutation-frequency-views.sql.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- gene_mutation_frequency_by_cancer_type(preference, gene)
-- ----------------------------------------------------------------------------
-- "Gene X mutation frequency across cancer types in cohort Y".
--
-- Parameters:
--   preference  — cancer_study_query_preferences.preference_name. Default
--                 'pan_cancer_tcga' (TCGA PanCancer Atlas: consistent
--                 CANCER_TYPE labels, balanced sample sizes). Discover the
--                 others with SELECT DISTINCT preference_name FROM
--                 cancer_study_query_preferences; see
--                 cbioportal://mutation-frequency-guide for when to switch.
--   gene        — HUGO gene symbol (e.g. 'TP53', 'KRAS'), exact match.
--
-- Usage:
--   SELECT * FROM gene_mutation_frequency_by_cancer_type(
--       preference='pan_cancer_tcga', gene='TP53'
--   ) ORDER BY frequency_pct DESC;
--
-- Returns one row per cancer type with ≥ 50 profiled samples for the gene:
-- (cancer_type, altered_samples, profiled_samples, frequency_pct).
-- ----------------------------------------------------------------------------

DROP VIEW IF EXISTS gene_mutation_frequency_by_cancer_type;

CREATE VIEW gene_mutation_frequency_by_cancer_type AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
-- Cohort studies the precompute does not cover (normally none). These go
-- through the live recipe below; everything else is read from the
-- precomputed tables. The two sets are disjoint, so summing is exact.
covered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier NOT IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
    )
),
uncovered_sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT cancer_type, sum(n) AS altered_samples
    FROM (
        SELECT cancer_type, altered_samples AS n
        FROM precomputed_alteration_altered_by_cancer_type
        WHERE alteration = 'mutation'
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT ged.sample_unique_id) AS n
        FROM genomic_event_derived ged
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM uncovered_profiles)
          AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
          AND ged.hugo_gene_symbol = {gene:String}
          AND ged.off_panel = 0
          AND (
            ('mutation' = 'mutation'
                AND ged.variant_type = 'mutation'
                AND ged.mutation_status != 'UNCALLED')
            OR ('mutation' = 'amplification'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = 2)
            OR ('mutation' = 'deep_deletion'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = -2)
            OR ('mutation' = 'structural_variant'
                AND ged.variant_type = 'structural_variant')
          )
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
),
profiled AS (
    SELECT cancer_type, sum(n) AS profiled_samples
    FROM (
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_panel_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_wes_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT p.sample_unique_id) AS n
        FROM (
            SELECT stgp.sample_unique_id
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene g ON gpl.gene_id = g.entrez_gene_id
            WHERE g.hugo_gene_symbol = {gene:String}
              AND stgp.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND stgp.alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
            UNION ALL
            SELECT sample_unique_id
            FROM sample_to_gene_panel_derived
            WHERE gene_panel_id = 'WES'
              AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
        ) p
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- gene_mutation_frequency_in_study(study, gene)
-- ----------------------------------------------------------------------------
-- Same recipe scoped to one named study (e.g. 'brca_metabric'). One row
-- for single-cancer-type studies; one row per CANCER_TYPE with ≥ 50
-- profiled samples for multi-cancer studies (e.g. msk_chord_2024).
--
--   SELECT * FROM gene_mutation_frequency_in_study(
--       study='brca_metabric', gene='TP53'
--   ) ORDER BY frequency_pct DESC;
-- ----------------------------------------------------------------------------

DROP VIEW IF EXISTS gene_mutation_frequency_in_study;

CREATE VIEW gene_mutation_frequency_in_study AS
WITH cohort AS (
    SELECT {study:String} AS cancer_study_identifier
),
-- Cohort studies the precompute does not cover (normally none). These go
-- through the live recipe below; everything else is read from the
-- precomputed tables. The two sets are disjoint, so summing is exact.
covered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier NOT IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
    )
),
uncovered_sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT cancer_type, sum(n) AS altered_samples
    FROM (
        SELECT cancer_type, altered_samples AS n
        FROM precomputed_alteration_altered_by_cancer_type
        WHERE alteration = 'mutation'
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT ged.sample_unique_id) AS n
        FROM genomic_event_derived ged
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM uncovered_profiles)
          AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
          AND ged.hugo_gene_symbol = {gene:String}
          AND ged.off_panel = 0
          AND (
            ('mutation' = 'mutation'
                AND ged.variant_type = 'mutation'
                AND ged.mutation_status != 'UNCALLED')
            OR ('mutation' = 'amplification'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = 2)
            OR ('mutation' = 'deep_deletion'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = -2)
            OR ('mutation' = 'structural_variant'
                AND ged.variant_type = 'structural_variant')
          )
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
),
profiled AS (
    SELECT cancer_type, sum(n) AS profiled_samples
    FROM (
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_panel_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_wes_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT p.sample_unique_id) AS n
        FROM (
            SELECT stgp.sample_unique_id
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene g ON gpl.gene_id = g.entrez_gene_id
            WHERE g.hugo_gene_symbol = {gene:String}
              AND stgp.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND stgp.alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
            UNION ALL
            SELECT sample_unique_id
            FROM sample_to_gene_panel_derived
            WHERE gene_panel_id = 'WES'
              AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
        ) p
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- gene_mutation_frequency_in_studies(studies, gene)
-- ----------------------------------------------------------------------------
-- Same recipe over an ad-hoc Array(String) of study ids. The CALLER is
-- responsible for non-overlap: the same physical sample in two studies
-- has two sample_unique_ids and counts twice. Shipped preferences are
-- vetted non-overlapping; ad-hoc lists are not.
--
--   SELECT * FROM gene_mutation_frequency_in_studies(
--       studies=['brca_metabric','brca_tcga_pan_can_atlas_2018'], gene='TP53'
--   ) ORDER BY frequency_pct DESC;
-- ----------------------------------------------------------------------------

DROP VIEW IF EXISTS gene_mutation_frequency_in_studies;

CREATE VIEW gene_mutation_frequency_in_studies AS
WITH cohort AS (
    SELECT arrayJoin({studies:Array(String)}) AS cancer_study_identifier
),
-- Cohort studies the precompute does not cover (normally none). These go
-- through the live recipe below; everything else is read from the
-- precomputed tables. The two sets are disjoint, so summing is exact.
covered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier NOT IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
    )
),
uncovered_sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT cancer_type, sum(n) AS altered_samples
    FROM (
        SELECT cancer_type, altered_samples AS n
        FROM precomputed_alteration_altered_by_cancer_type
        WHERE alteration = 'mutation'
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT ged.sample_unique_id) AS n
        FROM genomic_event_derived ged
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM uncovered_profiles)
          AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
          AND ged.hugo_gene_symbol = {gene:String}
          AND ged.off_panel = 0
          AND (
            ('mutation' = 'mutation'
                AND ged.variant_type = 'mutation'
                AND ged.mutation_status != 'UNCALLED')
            OR ('mutation' = 'amplification'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = 2)
            OR ('mutation' = 'deep_deletion'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = -2)
            OR ('mutation' = 'structural_variant'
                AND ged.variant_type = 'structural_variant')
          )
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
),
profiled AS (
    SELECT cancer_type, sum(n) AS profiled_samples
    FROM (
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_panel_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_wes_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT p.sample_unique_id) AS n
        FROM (
            SELECT stgp.sample_unique_id
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene g ON gpl.gene_id = g.entrez_gene_id
            WHERE g.hugo_gene_symbol = {gene:String}
              AND stgp.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND stgp.alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
            UNION ALL
            SELECT sample_unique_id
            FROM sample_to_gene_panel_derived
            WHERE gene_panel_id = 'WES'
              AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND alteration_type = multiIf(
          'mutation' = 'mutation',           'MUTATION_EXTENDED',
          'mutation' = 'amplification',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          'mutation' = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
        ) p
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- gene_alteration_frequency_by_cancer_type(preference, gene, alteration)
-- ----------------------------------------------------------------------------
-- Generalizes the mutation recipe to CNA and SV. alteration is one of:
--   'mutation'           — point mutations (UNCALLED excluded)  / MUTATION_EXTENDED
--   'amplification'      — CNA == +2 (high-level amp)           / COPY_NUMBER_ALTERATION
--   'deep_deletion'      — CNA == -2 (homozygous deletion)      / COPY_NUMBER_ALTERATION
--   'structural_variant' — fusion / SV                           / STRUCTURAL_VARIANT
--
--   SELECT * FROM gene_alteration_frequency_by_cancer_type(
--       preference='pan_cancer_tcga', gene='MYC', alteration='amplification'
--   ) ORDER BY frequency_pct DESC;
--
-- Same output columns as gene_mutation_frequency_by_cancer_type; for
-- alteration='mutation' the result equals that view.
-- ----------------------------------------------------------------------------

DROP VIEW IF EXISTS gene_alteration_frequency_by_cancer_type;

CREATE VIEW gene_alteration_frequency_by_cancer_type AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
-- Cohort studies the precompute does not cover (normally none). These go
-- through the live recipe below; everything else is read from the
-- precomputed tables. The two sets are disjoint, so summing is exact.
covered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier NOT IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
    )
),
uncovered_sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT cancer_type, sum(n) AS altered_samples
    FROM (
        SELECT cancer_type, altered_samples AS n
        FROM precomputed_alteration_altered_by_cancer_type
        WHERE alteration = {alteration:String}
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT ged.sample_unique_id) AS n
        FROM genomic_event_derived ged
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM uncovered_profiles)
          AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
          AND ged.hugo_gene_symbol = {gene:String}
          AND ged.off_panel = 0
          AND (
            ({alteration:String} = 'mutation'
                AND ged.variant_type = 'mutation'
                AND ged.mutation_status != 'UNCALLED')
            OR ({alteration:String} = 'amplification'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = 2)
            OR ({alteration:String} = 'deep_deletion'
                AND ged.variant_type = 'cna'
                AND ged.cna_alteration = -2)
            OR ({alteration:String} = 'structural_variant'
                AND ged.variant_type = 'structural_variant')
          )
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
),
profiled AS (
    SELECT cancer_type, sum(n) AS profiled_samples
    FROM (
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_panel_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND hugo_gene_symbol = {gene:String}
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT cancer_type, profiled_samples AS n
        FROM precomputed_alteration_wes_profiled_by_cancer_type
        WHERE alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
          AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT sct.cancer_type, COUNT(DISTINCT p.sample_unique_id) AS n
        FROM (
            SELECT stgp.sample_unique_id
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene g ON gpl.gene_id = g.entrez_gene_id
            WHERE g.hugo_gene_symbol = {gene:String}
              AND stgp.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND stgp.alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
            UNION ALL
            SELECT sample_unique_id
            FROM sample_to_gene_panel_derived
            WHERE gene_panel_id = 'WES'
              AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
              AND alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
        ) p
        JOIN uncovered_sample_cancer_type sct USING (sample_unique_id)
        GROUP BY sct.cancer_type
    )
    GROUP BY cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- top_mutated_genes_in_cohort(preference, top_n)
-- ----------------------------------------------------------------------------
-- Top-N most-mutated genes in a cohort. Mirrors cbioportal-backend's
-- StudyViewMapper.getMutatedGenes with a WES-aware per-gene denominator:
-- profiled_samples = (cohort WES samples) + (cohort samples on a named
-- panel listing the gene).
--
--   SELECT * FROM top_mutated_genes_in_cohort(preference='pan_cancer_tcga', top_n=20);
--
-- Returns (hugo_gene_symbol, altered_samples, profiled_samples,
-- frequency_pct, total_mutation_events), sorted by altered_samples DESC
-- then hugo_gene_symbol ASC (same as the backend).
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS top_mutated_genes_in_cohort;

CREATE VIEW top_mutated_genes_in_cohort AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
covered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered AS (
    SELECT cancer_study_identifier FROM cohort
    WHERE cancer_study_identifier NOT IN (
        SELECT cancer_study_identifier FROM precomputed_frequency_studies
    )
),
uncovered_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
    )
),
wes_profiled_count AS (
    SELECT sum(n) AS n
    FROM (
        SELECT profiled_samples AS n
        FROM precomputed_mutation_wes_profiled_by_study
        WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT COUNT(DISTINCT w.sample_unique_id) AS n
        FROM mutation_wes_coverage w
        WHERE w.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
    )
),
panel_profiled_per_gene AS (
    SELECT hugo_gene_symbol, sum(n) AS n
    FROM (
        SELECT hugo_gene_symbol, profiled_samples AS n
        FROM precomputed_mutation_panel_profiled_by_study
        WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT mpgc.hugo_gene_symbol, COUNT(DISTINCT mpgc.sample_unique_id) AS n
        FROM mutation_panel_gene_coverage mpgc
        WHERE mpgc.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
        GROUP BY mpgc.hugo_gene_symbol
    )
    GROUP BY hugo_gene_symbol
),
altered_per_gene AS (
    SELECT hugo_gene_symbol,
           sum(altered) AS altered_samples,
           sum(events) AS total_mutation_events
    FROM (
        SELECT hugo_gene_symbol, altered_samples AS altered, total_mutation_events AS events
        FROM precomputed_mutation_altered_by_study
        WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM covered)
        UNION ALL
        SELECT ged.hugo_gene_symbol,
               COUNT(DISTINCT ged.sample_unique_id) AS altered,
               COUNT(*) AS events
        FROM genomic_event_derived ged
        WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM uncovered_profiles)
          AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM uncovered)
          AND ged.variant_type = 'mutation'
          AND ged.mutation_status != 'UNCALLED'
          AND ged.off_panel = 0
        GROUP BY ged.hugo_gene_symbol
    )
    GROUP BY hugo_gene_symbol
)
SELECT a.hugo_gene_symbol,
       a.altered_samples,
       (SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0) AS profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF((SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0), 0), 1) AS frequency_pct,
       a.total_mutation_events
FROM altered_per_gene a
LEFT JOIN panel_profiled_per_gene p USING (hugo_gene_symbol)
ORDER BY altered_samples DESC, hugo_gene_symbol ASC
LIMIT {top_n:UInt32};
