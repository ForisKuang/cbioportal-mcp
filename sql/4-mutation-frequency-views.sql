-- ============================================================================
-- Mutation-frequency views (coverage building blocks + frequency recipes)
-- ============================================================================
-- This file is the mutation side of the agent's gene-frequency API:
--   - Two coverage building-block views (`mutation_panel_gene_coverage`,
--     `mutation_wes_coverage`).
--   - The LIVE (recompute-on-read) parameterized frequency recipes,
--     suffixed `_live`. The agent-facing names without the suffix are
--     created in sql/6-precomputed-mutation-frequency.sql on top of
--     per-study aggregates materialized at data-refresh time.
--
-- Sibling files in this directory:
--   sql/5-gene-expression-views.sql — gene_pair_coexpression and any
--     other expression / copy-number-value / methylation correlation
--     views. Anything backed by `genetic_alteration_derived` lives
--     there, not here.
--
-- The agent-facing docs are at `cbioportal://mutation-frequency-guide`.
-- ============================================================================

-- ============================================================================
-- mutation_panel_gene_coverage + mutation_wes_coverage
-- ============================================================================
-- The canonical "is this sample profiled for gene G?" query has to handle
-- two cases:
--
--   1. The sample is on a NAMED gene panel (MSK-IMPACT, METABRIC_173,
--      ARCHER-SOLID, ...) and the panel's `gene_panel_list` entries do
--      or don't include G.
--   2. The sample is sequenced via WES (`gene_panel_id = 'WES'`), in
--      which case ALL genes are considered profiled. WES is NOT in the
--      `gene_panel` table at all, so any JOIN through `gene_panel` /
--      `gene_panel_list` silently drops every WES row — this is the bug
--      that produced 172% TP53 frequencies in cross-cancer-type queries
--      before the views below existed.
--
-- These two views together let queries answer "samples profiled for
-- mutations in gene G" with one UNION ALL instead of re-deriving the
-- WES branch every time:
--
--   SELECT sample_unique_id, cancer_study_identifier
--   FROM mutation_panel_gene_coverage WHERE hugo_gene_symbol = 'TP53'
--   UNION ALL
--   SELECT sample_unique_id, cancer_study_identifier
--   FROM mutation_wes_coverage;
--
-- See cbioportal://mutation-frequency-guide for the full canonical
-- frequency-by-cancer-type recipe.
--
-- Refreshed (DROP + CREATE) on every clone. Views are cheap — they hold
-- no data, just the query definition.
-- ============================================================================

DROP VIEW IF EXISTS mutation_panel_gene_coverage;

CREATE VIEW mutation_panel_gene_coverage AS
SELECT
    stgp.sample_unique_id,
    stgp.cancer_study_identifier,
    g.hugo_gene_symbol,
    stgp.gene_panel_id
FROM sample_to_gene_panel_derived stgp
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED';

DROP VIEW IF EXISTS mutation_wes_coverage;

CREATE VIEW mutation_wes_coverage AS
SELECT
    sample_unique_id,
    cancer_study_identifier
FROM sample_to_gene_panel_derived
WHERE alteration_type = 'MUTATION_EXTENDED'
  AND gene_panel_id = 'WES';

-- ============================================================================
-- Live (recompute-on-read) frequency recipes: *_live views
-- ============================================================================
-- These five parameterized views are the REFERENCE definition of every
-- mutation / alteration frequency number the agent reports. They recompute
-- the event × clinical × panel-coverage joins on every read.
--
-- The agent-facing names WITHOUT the `_live` suffix
-- (gene_mutation_frequency_by_cancer_type, gene_mutation_frequency_in_study,
-- gene_mutation_frequency_in_studies, gene_alteration_frequency_by_cancer_type,
-- top_mutated_genes_in_cohort) are defined in
-- sql/6-precomputed-mutation-frequency.sql. They return numerically
-- identical results but read per-study aggregates materialized at
-- data-refresh time, and fall back to this live logic only for studies
-- missing from the precompute. scripts/check_precomputed_parity.sql
-- compares the two.
--
-- Keep the counting rules here and in sql/6-* in sync: any change to the
-- numerator / denominator definition must be made in BOTH files, and the
-- parity script re-run.
--
-- Pruning notes (ClickHouse sorting keys, checked against the public
-- deployment's system.tables):
--   genomic_event_derived         ORDER BY (genetic_profile_stable_id,
--                                  cancer_study_identifier, variant_type,
--                                  entrez_gene_id, hugo_gene_symbol, ...)
--   clinical_data_derived         ORDER BY (cancer_study_identifier, type,
--                                  attribute_name, sample_unique_id)
--   sample_to_gene_panel_derived  ORDER BY (gene_panel_id, alteration_type,
--                                  genetic_profile_id, sample_unique_id)
-- so every branch below filters genomic_event_derived on the cohort's
-- genetic_profile_stable_id set (resolved via genetic_profile, which holds
-- every profile referenced by genomic_event_derived) AND on
-- cancer_study_identifier, and filters clinical_data_derived /
-- coverage branches on cancer_study_identifier directly instead of only
-- via a post-hoc JOIN. These predicates are redundant with the cohort
-- JOIN semantics — they never change results, only how much is read.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- gene_mutation_frequency_by_cancer_type_live(preference, gene)
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS gene_mutation_frequency_by_cancer_type_live;

CREATE VIEW gene_mutation_frequency_by_cancer_type_live AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
cohort_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
    )
),
sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM cohort_profiles)
      AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
      AND ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = {gene:String}
      AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
    UNION ALL
    SELECT sample_unique_id
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
    JOIN sample_cancer_type sct USING (sample_unique_id)
    GROUP BY sct.cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- gene_mutation_frequency_in_study_live(study, gene)
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS gene_mutation_frequency_in_study_live;

CREATE VIEW gene_mutation_frequency_in_study_live AS
WITH study_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier = {study:String}
    )
),
sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier = {study:String}
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM study_profiles)
      AND ged.cancer_study_identifier = {study:String}
      AND ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = {gene:String}
      AND cancer_study_identifier = {study:String}
    UNION ALL
    SELECT sample_unique_id
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier = {study:String}
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
    JOIN sample_cancer_type sct USING (sample_unique_id)
    GROUP BY sct.cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- gene_mutation_frequency_in_studies_live(studies, gene)
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS gene_mutation_frequency_in_studies_live;

CREATE VIEW gene_mutation_frequency_in_studies_live AS
WITH studies_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN {studies:Array(String)}
    )
),
sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN {studies:Array(String)}
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM studies_profiles)
      AND ged.cancer_study_identifier IN {studies:Array(String)}
      AND ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = {gene:String}
      AND cancer_study_identifier IN {studies:Array(String)}
    UNION ALL
    SELECT sample_unique_id
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier IN {studies:Array(String)}
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
    JOIN sample_cancer_type sct USING (sample_unique_id)
    GROUP BY sct.cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- gene_alteration_frequency_by_cancer_type_live(preference, gene, alteration)
-- ----------------------------------------------------------------------------
-- alteration token → numerator filter / denominator alteration_type:
--   'mutation'           variant_type='mutation' AND mutation_status!='UNCALLED' / MUTATION_EXTENDED
--   'amplification'      variant_type='cna' AND cna_alteration=2                 / COPY_NUMBER_ALTERATION
--   'deep_deletion'      variant_type='cna' AND cna_alteration=-2                / COPY_NUMBER_ALTERATION
--   'structural_variant' variant_type='structural_variant'                       / STRUCTURAL_VARIANT
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS gene_alteration_frequency_by_cancer_type_live;

CREATE VIEW gene_alteration_frequency_by_cancer_type_live AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
cohort_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
    )
),
sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM cohort_profiles)
      AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
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
),
profiled_samples_for_gene AS (
    SELECT stgp.sample_unique_id
    FROM sample_to_gene_panel_derived stgp
    JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
    WHERE g.hugo_gene_symbol = {gene:String}
      AND stgp.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
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
      AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
      AND alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
    JOIN sample_cancer_type sct USING (sample_unique_id)
    GROUP BY sct.cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;

-- ----------------------------------------------------------------------------
-- top_mutated_genes_in_cohort_live(preference, top_n)
-- ----------------------------------------------------------------------------
-- Mirrors cbioportal-backend's StudyViewMapper.getMutatedGenes with a
-- WES-aware per-gene denominator: WES samples are profiled for every gene
-- (one number per cohort); named-panel samples are profiled only for the
-- genes their panel lists. The two pieces are added per gene (not
-- DISTINCT-unioned — this matches the backend; the precomputed view in
-- sql/6-* reproduces the same additive formula).
-- ----------------------------------------------------------------------------
DROP VIEW IF EXISTS top_mutated_genes_in_cohort_live;

CREATE VIEW top_mutated_genes_in_cohort_live AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
cohort_profiles AS (
    SELECT gp.stable_id
    FROM genetic_profile gp
    WHERE gp.cancer_study_id IN (
        SELECT cs.cancer_study_id FROM cancer_study cs
        WHERE cs.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
    )
),
wes_profiled_count AS (
    SELECT COUNT(DISTINCT w.sample_unique_id) AS n
    FROM mutation_wes_coverage w
    WHERE w.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
),
panel_profiled_per_gene AS (
    SELECT mpgc.hugo_gene_symbol, COUNT(DISTINCT mpgc.sample_unique_id) AS n
    FROM mutation_panel_gene_coverage mpgc
    WHERE mpgc.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
    GROUP BY mpgc.hugo_gene_symbol
),
altered_per_gene AS (
    SELECT ged.hugo_gene_symbol,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples,
           COUNT(*) AS total_mutation_events
    FROM genomic_event_derived ged
    WHERE ged.genetic_profile_stable_id IN (SELECT stable_id FROM cohort_profiles)
      AND ged.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
      AND ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.off_panel = 0
    GROUP BY ged.hugo_gene_symbol
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
