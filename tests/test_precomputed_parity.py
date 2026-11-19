"""Parity tests for sql/6-precomputed-mutation-frequency.sql.

Builds a small synthetic cBioPortal-shaped database in `clickhouse local`,
applies the real SQL files (3, 4, 6 and the public-portal preferences) in
apply_sql.sh order, and checks that every agent-facing frequency view
returns exactly what its `_live` twin in sql/4-* returns:

- covered path: every study is in the precompute manifest;
- fallback path: some / all cohort studies are missing from the manifest
  and must be computed live;
- idempotency: re-applying the files leaves results unchanged.

The synthetic data deliberately includes the traps the recipes handle:
WES vs named-panel samples, samples with BOTH a WES and a panel row,
samples on two panels, samples without CANCER_TYPE, UNCALLED and
off-panel events, CNA ±1/±2, SVs, multi-cancer-type studies, and genes
absent from some panels.

Skipped when the `clickhouse` binary is not on PATH.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SQL_DIR = REPO / "sql"
CLICKHOUSE = shutil.which("clickhouse")

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not on PATH")

DB = "cbio_test"

GENES = {  # entrez -> symbol
    7157: "TP53",
    3845: "KRAS",
    4609: "MYC",
    2064: "ERBB2",
    1956: "EGFR",
    238: "ALK",
    5728: "PTEN",
    7273: "TTN",
}
# Named panels: internal_id, stable_id, entrez ids listed.
PANELS = [
    (1, "PANEL_SMALL", [7157, 3845, 4609, 238]),
    (2, "PANEL_BIG", [7157, 3845, 4609, 2064, 1956, 238, 5728]),
]
ALTERATION_TYPES = ["MUTATION_EXTENDED", "COPY_NUMBER_ALTERATION", "STRUCTURAL_VARIANT"]
PROFILE_SUFFIX = {
    "mutation": "_mutations",
    "cna": "_gistic",  # deliberately NOT "<study>_cna", like real gistic profiles
    "structural_variant": "_structural_variants",
}

# study id -> (cancer types to draw from, sample count, coverage mode)
STUDIES = {
    "brca_tcga_pan_can_atlas_2018": (["Breast Cancer"], 140, "wes"),
    "luad_tcga_pan_can_atlas_2018": (["Non-Small Cell Lung Cancer"], 120, "wes"),
    "mixed_panel_study": (["Breast Cancer", "Non-Small Cell Lung Cancer", "Glioma"], 260, "panel"),
    "mixed_hybrid_study": (["Glioma", "Breast Cancer"], 180, "hybrid"),
}


def _q(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _values(rows: list[tuple]) -> str:
    def fmt(v):
        if v is None:
            return "NULL"
        if isinstance(v, str):
            return _q(v)
        return str(v)

    return ",\n".join("(" + ", ".join(fmt(v) for v in row) + ")" for row in rows)


def _synthetic_data_sql() -> str:
    rng = random.Random(20261119)
    cancer_study, genetic_profile, clinical, events, stgp = [], [], [], [], []
    profile_id = 0
    for study_idx, (study, (cancer_types, n_samples, mode)) in enumerate(STUDIES.items(), 1):
        cancer_study.append((study_idx, study))
        for suffix in PROFILE_SUFFIX.values():
            profile_id += 1
            genetic_profile.append((profile_id, study + suffix, study_idx))
        for i in range(n_samples):
            sample = f"{study}_S{i:04d}"
            patient = f"{study}_P{i // 2:04d}"
            # ~5% of samples have no CANCER_TYPE row.
            if rng.random() > 0.05:
                clinical.append(
                    (sample, patient, "CANCER_TYPE", rng.choice(cancer_types), study, "sample")
                )
            clinical.append((sample, patient, "SAMPLE_TYPE", "Primary", study, "sample"))

            # Coverage per alteration type.
            for alt_type in ALTERATION_TYPES:
                if mode == "wes":
                    panels = ["WES"]
                elif mode == "panel":
                    panels = [rng.choice(["PANEL_SMALL", "PANEL_BIG"])]
                    if rng.random() < 0.1:
                        panels = ["PANEL_SMALL", "PANEL_BIG"]  # on two panels
                else:  # hybrid
                    r = rng.random()
                    if r < 0.4:
                        panels = ["WES"]
                    elif r < 0.8:
                        panels = ["PANEL_BIG"]
                    else:
                        panels = ["WES", "PANEL_SMALL"]  # WES + panel overlap
                if alt_type == "STRUCTURAL_VARIANT" and rng.random() < 0.3:
                    continue  # not profiled for SV
                for gp in panels:
                    stgp.append((sample, gp, alt_type, f"{study}_{alt_type.lower()}", study))

            # Events: random, with a few heavily-mutated genes so the
            # >= 50-profiled filter and top-N ties both get exercised.
            for entrez, symbol in GENES.items():
                p_mut = {"TP53": 0.45, "KRAS": 0.25, "TTN": 0.3}.get(symbol, 0.08)
                for _ in range(rng.choice([1, 1, 1, 2])):
                    if rng.random() < p_mut:
                        status = rng.choice(["SOMATIC"] * 8 + ["UNKNOWN", "UNCALLED"])
                        off_panel = 1 if rng.random() < 0.05 else 0
                        events.append(
                            (
                                sample,
                                symbol,
                                entrez,
                                study,
                                study + PROFILE_SUFFIX["mutation"],
                                "mutation",
                                status,
                                None,
                                off_panel,
                            )
                        )
                if rng.random() < 0.12:
                    cna = rng.choice([2, -2, 1, -1])
                    events.append(
                        (
                            sample,
                            symbol,
                            entrez,
                            study,
                            study + PROFILE_SUFFIX["cna"],
                            "cna",
                            "",
                            cna,
                            0,
                        )
                    )
                if rng.random() < 0.03:
                    events.append(
                        (
                            sample,
                            symbol,
                            entrez,
                            study,
                            study + PROFILE_SUFFIX["structural_variant"],
                            "structural_variant",
                            "",
                            None,
                            0,
                        )
                    )

    gene_panel = [(pid, sid) for pid, sid, _ in PANELS]
    gene_panel_list = [(pid, g) for pid, _, genes in PANELS for g in genes]
    gene = list(GENES.items())

    return f"""
CREATE DATABASE IF NOT EXISTS {DB} ENGINE = Atomic;
USE {DB};
CREATE TABLE cancer_study (cancer_study_id Int64, cancer_study_identifier String)
    ENGINE = MergeTree ORDER BY cancer_study_id;
CREATE TABLE genetic_profile (genetic_profile_id Int64, stable_id String, cancer_study_id Int64)
    ENGINE = MergeTree ORDER BY genetic_profile_id;
CREATE TABLE clinical_data_derived (
    sample_unique_id String, patient_unique_id String, attribute_name String,
    attribute_value String, cancer_study_identifier String, type String)
    ENGINE = MergeTree ORDER BY (cancer_study_identifier, type, attribute_name, sample_unique_id);
CREATE TABLE genomic_event_derived (
    sample_unique_id String, hugo_gene_symbol String, entrez_gene_id Int32,
    cancer_study_identifier LowCardinality(String),
    genetic_profile_stable_id LowCardinality(String), variant_type LowCardinality(String),
    mutation_status LowCardinality(String), cna_alteration Nullable(Int8), off_panel UInt8)
    ENGINE = MergeTree
    ORDER BY (genetic_profile_stable_id, cancer_study_identifier, variant_type,
              entrez_gene_id, hugo_gene_symbol, sample_unique_id);
CREATE TABLE sample_to_gene_panel_derived (
    sample_unique_id String, gene_panel_id LowCardinality(String),
    alteration_type LowCardinality(String), genetic_profile_id String,
    cancer_study_identifier LowCardinality(String))
    ENGINE = MergeTree
    ORDER BY (gene_panel_id, alteration_type, genetic_profile_id, sample_unique_id);
CREATE TABLE gene_panel (internal_id Int64, stable_id String)
    ENGINE = MergeTree ORDER BY internal_id;
CREATE TABLE gene_panel_list (internal_id Int64, gene_id Int64)
    ENGINE = MergeTree ORDER BY (internal_id, gene_id);
CREATE TABLE gene (entrez_gene_id Int64, hugo_gene_symbol String)
    ENGINE = MergeTree ORDER BY entrez_gene_id;
INSERT INTO cancer_study VALUES {_values(cancer_study)};
INSERT INTO genetic_profile VALUES {_values(genetic_profile)};
INSERT INTO clinical_data_derived VALUES {_values(clinical)};
INSERT INTO genomic_event_derived VALUES {_values(events)};
INSERT INTO sample_to_gene_panel_derived VALUES {_values(stgp)};
INSERT INTO gene_panel VALUES {_values(gene_panel)};
INSERT INTO gene_panel_list VALUES {_values(gene_panel_list)};
INSERT INTO gene VALUES {_values(gene)};
"""


class ClickHouseLocal:
    def __init__(self, path: Path):
        self.path = path

    def run(self, sql: str) -> str:
        # stdin must be closed: clickhouse local otherwise waits on it as input data.
        result = subprocess.run(
            [
                CLICKHOUSE,
                "local",
                "--path",
                str(self.path),
                "--database",
                DB,
                "--multiquery",
                "--query",
                sql,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"clickhouse local failed:\n{result.stderr}\n--- SQL ---\n{sql[:2000]}"
            )
        return result.stdout

    def run_file(self, path: Path) -> None:
        self.run(path.read_text())

    def scalar(self, sql: str) -> str:
        return self.run(sql).strip()


def _apply_all(ch: ClickHouseLocal) -> None:
    """Apply the files the frequency views depend on, in apply_sql.sh order."""
    for name in (
        "3-add-cancer-study-query-preferences.sql",
        "4-mutation-frequency-views.sql",
        "6-precomputed-mutation-frequency.sql",
    ):
        ch.run_file(SQL_DIR / name)
    # Portal-specific runs after the portable files; these INSERTs are
    # existence-gated and add nothing here (proves they're harmless).
    for f in sorted((SQL_DIR / "portal-specific").glob("*/[0-9]*-*.sql")):
        ch.run_file(f)
    # Test-only preferences, inserted AFTER sql/6 like portal-specific
    # preferences are in production.
    ch.run(
        """
        INSERT INTO cancer_study_query_preferences
        SELECT 'test_all', cancer_study_identifier, 'test' FROM cancer_study;
        INSERT INTO cancer_study_query_preferences VALUES
            ('test_mixed', 'mixed_panel_study', 'test'),
            ('test_mixed', 'mixed_hybrid_study', 'test'),
            ('test_with_missing', 'mixed_hybrid_study', 'test'),
            ('test_with_missing', 'not_loaded_study', 'test');
        """
    )


@pytest.fixture(scope="module")
def ch(tmp_path_factory) -> ClickHouseLocal:
    path = tmp_path_factory.mktemp("clickhouse")
    ch = ClickHouseLocal(path)
    # Database must exist before --database can select it.
    subprocess.run(
        [
            CLICKHOUSE,
            "local",
            "--path",
            str(path),
            "--query",
            f"CREATE DATABASE IF NOT EXISTS {DB} ENGINE = Atomic",
        ],
        stdin=subprocess.DEVNULL,
        check=True,
        capture_output=True,
        timeout=60,
    )
    ch.run(_synthetic_data_sql())
    _apply_all(ch)
    return ch


PREFERENCES = ["pan_cancer_tcga", "test_all", "test_mixed", "test_with_missing", "no_such_pref"]
GENE_ARGS = list(GENES.values()) + ["NOT_A_GENE"]
ALTERATIONS = ["mutation", "amplification", "deep_deletion", "structural_variant", "bogus"]
STUDY_ARGS = list(STUDIES) + ["not_loaded_study"]
STUDY_LISTS = [
    ["brca_tcga_pan_can_atlas_2018", "mixed_hybrid_study"],
    list(STUDIES),
    ["mixed_panel_study", "not_loaded_study"],
]


def _call_pairs() -> list[tuple[str, str]]:
    """(precomputed view call, live view call) for every covered combination."""
    pairs = []
    for pref in PREFERENCES:
        for gene in GENE_ARGS:
            args = f"preference={_q(pref)}, gene={_q(gene)}"
            pairs.append(
                (
                    f"gene_mutation_frequency_by_cancer_type({args})",
                    f"gene_mutation_frequency_by_cancer_type_live({args})",
                )
            )
            for alt in ALTERATIONS:
                a = f"{args}, alteration={_q(alt)}"
                pairs.append(
                    (
                        f"gene_alteration_frequency_by_cancer_type({a})",
                        f"gene_alteration_frequency_by_cancer_type_live({a})",
                    )
                )
        for top_n in (1, 3, 20):
            a = f"preference={_q(pref)}, top_n={top_n}"
            pairs.append(
                (f"top_mutated_genes_in_cohort({a})", f"top_mutated_genes_in_cohort_live({a})")
            )
    for gene in GENE_ARGS:
        for study in STUDY_ARGS:
            a = f"study={_q(study)}, gene={_q(gene)}"
            pairs.append(
                (
                    f"gene_mutation_frequency_in_study({a})",
                    f"gene_mutation_frequency_in_study_live({a})",
                )
            )
        for studies in STUDY_LISTS:
            arr = "[" + ", ".join(_q(s) for s in studies) + "]"
            a = f"studies={arr}, gene={_q(gene)}"
            pairs.append(
                (
                    f"gene_mutation_frequency_in_studies({a})",
                    f"gene_mutation_frequency_in_studies_live({a})",
                )
            )
    return pairs


def _mismatches(ch: ClickHouseLocal, pairs: list[tuple[str, str]] | None = None) -> list[str]:
    """Return the view calls whose output differs from the live view."""
    pairs = _call_pairs() if pairs is None else pairs
    # One statement per pair, all in a single clickhouse-local process; each
    # prints its index only when the two outputs differ.
    statements = []
    for i, (pre, live) in enumerate(pairs):
        statements.append(
            f"""SELECT {i} WHERE
                (SELECT groupArray(tuple(*)) FROM (SELECT * FROM {pre} ORDER BY ALL))
             != (SELECT groupArray(tuple(*)) FROM (SELECT * FROM {live} ORDER BY ALL));"""
        )
    out = ch.run("\n".join(statements))
    return [pairs[int(line)][0] for line in out.split()]


def test_synthetic_fixture_exercises_the_traps(ch):
    """Guard against the fixture silently degenerating into a trivial case."""
    checks = {
        # samples with both a WES and a named-panel row for the same type
        "wes_panel_overlap": """SELECT count() FROM (
            SELECT alteration_type, sample_unique_id FROM sample_to_gene_panel_derived
            GROUP BY alteration_type, sample_unique_id
            HAVING countIf(gene_panel_id = 'WES') > 0 AND countIf(gene_panel_id != 'WES') > 0)""",
        "two_panels": """SELECT count() FROM (
            SELECT alteration_type, sample_unique_id FROM sample_to_gene_panel_derived
            WHERE gene_panel_id != 'WES' GROUP BY alteration_type, sample_unique_id
            HAVING count() > 1)""",
        "no_cancer_type": """SELECT count() FROM (SELECT DISTINCT sample_unique_id
            FROM sample_to_gene_panel_derived) WHERE sample_unique_id NOT IN (
            SELECT sample_unique_id FROM clinical_data_derived
            WHERE attribute_name = 'CANCER_TYPE')""",
        "uncalled": "SELECT countIf(mutation_status = 'UNCALLED') FROM genomic_event_derived",
        "off_panel": "SELECT countIf(off_panel = 1) FROM genomic_event_derived",
        "rows_passing_50": """SELECT count() FROM gene_alteration_frequency_by_cancer_type(
            preference='test_all', gene='TP53', alteration='mutation')""",
        "precomputed_rows": "SELECT count() FROM precomputed_alteration_altered_by_cancer_type",
        "manifest_rows": "SELECT count() FROM precomputed_frequency_studies",
    }
    for name, sql in checks.items():
        assert int(ch.scalar(sql)) > 0, name


def test_precomputed_views_match_live_views(ch):
    assert _mismatches(ch) == []


def test_views_actually_read_precomputed_tables(ch):
    """Tamper with a precomputed row: the view must change, the live view must not."""
    before = ch.scalar(
        "SELECT altered_samples FROM gene_mutation_frequency_in_study("
        "study='brca_tcga_pan_can_atlas_2018', gene='TP53')"
    )
    ch.run(
        "ALTER TABLE precomputed_alteration_altered_by_cancer_type "
        "UPDATE altered_samples = altered_samples + 1000 "
        "WHERE alteration = 'mutation' AND hugo_gene_symbol = 'TP53' "
        "AND cancer_study_identifier = 'brca_tcga_pan_can_atlas_2018' SETTINGS mutations_sync = 1"
    )
    try:
        tampered = ch.scalar(
            "SELECT altered_samples FROM gene_mutation_frequency_in_study("
            "study='brca_tcga_pan_can_atlas_2018', gene='TP53')"
        )
        live = ch.scalar(
            "SELECT altered_samples FROM gene_mutation_frequency_in_study_live("
            "study='brca_tcga_pan_can_atlas_2018', gene='TP53')"
        )
        assert int(tampered) == int(before) + 1000
        assert int(live) == int(before)
        # ...and the parity comparison itself must flag it.
        tampered_pairs = [
            p
            for p in _call_pairs()
            if "'brca_tcga_pan_can_atlas_2018'" in p[0] and "gene='TP53'" in p[0]
        ]
        assert tampered_pairs and _mismatches(ch, tampered_pairs) == [p[0] for p in tampered_pairs]
    finally:
        ch.run_file(SQL_DIR / "6-precomputed-mutation-frequency.sql")


def test_fallback_to_live_for_studies_missing_from_precompute(ch):
    """Studies missing from the manifest are computed live, exactly."""
    # Partial coverage: drop two studies from the manifest AND corrupt their
    # precomputed rows, so any read of those rows would show up as a diff.
    ch.run(
        """
        ALTER TABLE precomputed_frequency_studies DELETE
            WHERE cancer_study_identifier IN ('mixed_hybrid_study', 'luad_tcga_pan_can_atlas_2018')
            SETTINGS mutations_sync = 1;
        ALTER TABLE precomputed_alteration_altered_by_cancer_type
            UPDATE altered_samples = altered_samples + 1000
            WHERE cancer_study_identifier = 'mixed_hybrid_study' SETTINGS mutations_sync = 1;
        ALTER TABLE precomputed_mutation_altered_by_study
            UPDATE altered_samples = altered_samples + 1000
            WHERE cancer_study_identifier = 'luad_tcga_pan_can_atlas_2018'
            SETTINGS mutations_sync = 1;
        """
    )
    try:
        assert _mismatches(ch) == []
        # No coverage at all: everything goes through the live branch.
        ch.run("TRUNCATE TABLE precomputed_frequency_studies")
        assert _mismatches(ch) == []
    finally:
        ch.run_file(SQL_DIR / "6-precomputed-mutation-frequency.sql")


def test_reapplying_sql_is_idempotent(ch):
    snapshot_sql = """
        SELECT
            (SELECT sum(altered_samples) FROM precomputed_alteration_altered_by_cancer_type),
            (SELECT sum(profiled_samples)
             FROM precomputed_alteration_panel_profiled_by_cancer_type),
            (SELECT sum(profiled_samples) FROM precomputed_alteration_wes_profiled_by_cancer_type),
            (SELECT sum(total_mutation_events) FROM precomputed_mutation_altered_by_study),
            (SELECT sum(profiled_samples) FROM precomputed_mutation_panel_profiled_by_study),
            (SELECT sum(profiled_samples) FROM precomputed_mutation_wes_profiled_by_study),
            (SELECT count() FROM precomputed_frequency_studies),
            (SELECT groupArray(tuple(*))
             FROM top_mutated_genes_in_cohort(preference='test_all', top_n=20)),
            (SELECT count() FROM system.tables
             WHERE database = currentDatabase() AND name LIKE '%__staging')
    """
    before = ch.scalar(snapshot_sql)
    for name in ("4-mutation-frequency-views.sql", "6-precomputed-mutation-frequency.sql"):
        ch.run_file(SQL_DIR / name)
        ch.run_file(SQL_DIR / name)
    assert ch.scalar(snapshot_sql) == before
    assert before.split("\t")[-1] == "0"  # no staging tables left behind
    assert _mismatches(ch) == []


GUIDE = REPO / "src" / "cbioportal_mcp" / "resources" / "mutation-frequency-guide.md"


def _guide_precomputed_examples() -> list[str]:
    blocks = GUIDE.read_text().split("```sql\n")[1:]
    return [b.split("```", 1)[0] for b in blocks if "precomputed_" in b.split("```", 1)[0]]


def test_guide_precomputed_examples_match_views(ch):
    """The guide's hand-written precomputed_* queries must agree with the views."""
    examples = _guide_precomputed_examples()
    assert len(examples) == 2
    per_cancer_type, per_study = examples

    # Per-cancer-type example (minus its LIMIT BY) vs the per-gene view.
    unlimited = per_cancer_type.rsplit("LIMIT", 1)[0].replace("'pan_cancer_tcga'", "'test_all'")
    for gene in GENES.values():
        guide_rows = ch.run(
            f"SELECT cancer_type, altered_samples, profiled_samples, frequency_pct "
            f"FROM ({unlimited}) WHERE hugo_gene_symbol = {_q(gene)} ORDER BY ALL"
        )
        view_rows = ch.run(
            "SELECT * FROM gene_mutation_frequency_by_cancer_type("
            f"preference='test_all', gene={_q(gene)}) ORDER BY ALL"
        )
        assert guide_rows == view_rows, gene

    # Single-study top-N example vs the live top-N view over a 1-study preference.
    ch.run(
        "INSERT INTO cancer_study_query_preferences VALUES "
        "('test_single_hybrid', 'mixed_hybrid_study', 'test')"
    )
    guide_rows = ch.run(per_study.replace("'your_study_id'", "'mixed_hybrid_study'"))
    live_rows = ch.run(
        "SELECT * FROM top_mutated_genes_in_cohort_live(preference='test_single_hybrid', top_n=20)"
    )
    # Guide column order: gene, mutations, altered, profiled, pct; view: gene, altered,
    # profiled, pct, mutations.
    reordered = [
        "\t".join([c[0], c[4], c[1], c[2], c[3]])
        for c in (line.split("\t") for line in live_rows.strip().splitlines())
    ]
    assert guide_rows.strip().splitlines() == reordered
