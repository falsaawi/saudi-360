"""
Saudi 360 — warehouse across all products.

    python build_warehouse_all.py

Reads data/parsed/facts.csv and builds data/saudi360.duckdb.

The parser emits every value it can read, which means the same period is
restated by every later release that republishes it. That is revision history,
not new information, so the fact table keeps ONE row per (series, period) --
the reading from the most recent release -- and the superseded readings stay
queryable in fact_revision.

A series is (product, source table, row label, column label, measure kind, index
base). Both the table and the index base are part of the identity on purpose: a
workbook repeats the same row labels across tables (a national total in one, the
same label per city in another), and the same indicator on a new base is not
splicable onto the old one.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DATA = ROOT / "data"
FACTS = DATA / "parsed" / "facts.csv"
FILES_CSV = DATA / "catalog" / "release_files.csv"
DB = DATA / "saudi360.duckdb"

# Fewest points that can honestly be drawn as a line. Defined here because
# v_chartable is what both the exporter and the checks read.
MIN_CHART_POINTS = 3

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("warehouse")


def main() -> int:
    if not FACTS.exists():
        log.error("missing %s — run run_parse_all.py first", FACTS)
        return 1
    if DB.exists():
        DB.unlink()

    con = duckdb.connect(str(DB))
    con.execute("PRAGMA memory_limit='3GB'")

    # Series identity must survive cosmetic drift between releases: footnote
    # markers, stray punctuation, case and spacing all vary for what is plainly
    # the same row. Without folding them, a ten-year series fragments into ten
    # one-point series and nothing can be charted.
    con.execute(r"""
        CREATE MACRO fold(x) AS
            nullif(
              lower(
                trim(
                  regexp_replace(
                    regexp_replace(
                      regexp_replace(coalesce(x, ''), '\(\s*\d+\s*\)', '', 'g'),
                      '[*٭∗·•:\-–\s]+$', ''),
                    '\s+', ' ', 'g')
                )
              ), '')
        ;
    """)

    log.info("reading %s (%.0f MB)", FACTS.name, FACTS.stat().st_size / 1048576)
    con.execute(f"""
        CREATE TABLE raw AS
        SELECT * FROM read_csv('{FACTS.as_posix()}', header=true, sample_size=-1,
            types={{'category_id':'VARCHAR','pub_id':'VARCHAR','index_base':'VARCHAR',
                    'period':'VARCHAR','period_type':'VARCHAR','row_en':'VARCHAR',
                    'row_ar':'VARCHAR','col_en':'VARCHAR','col_ar':'VARCHAR',
                    'kind':'VARCHAR','table':'VARCHAR','value':'DOUBLE',
                    'weight_pct':'DOUBLE'}})
        WHERE period IS NOT NULL AND period <> ''
          AND value IS NOT NULL
          AND (row_en <> '' OR row_ar <> '')
          -- An empty cell formatted as a date reads back as Excel's own epoch,
          -- giving observations dated 1900. Saudi official statistics do not
          -- reach back past the 1960s, so those are parse debris, not data.
          AND TRY_CAST(substr(period, 1, 4) AS INTEGER) BETWEEN 1960 AND 2035
    """)
    log.info("  raw rows: %s", f"{con.execute('SELECT COUNT(*) FROM raw').fetchone()[0]:,}")

    # Where each workbook sits on GASTAT, so a reader can open the file a figure
    # was read from and check the cell. The crawl catalogue keys files by content
    # digest and the same file is published under several products, so take one
    # URL per (product, digest) -- joining on the digest alone would multiply
    # every release row by the number of products sharing that file.
    if FILES_CSV.exists():
        con.execute(f"""
            CREATE TABLE file_url AS
            SELECT category_id, sha256, any_value(url) AS url
            FROM read_csv('{FILES_CSV.as_posix()}', header=true, all_varchar=true)
            WHERE sha256 IS NOT NULL AND url IS NOT NULL
            GROUP BY category_id, sha256
        """)
    else:
        log.warning("no %s — releases will carry no source link", FILES_CSV.name)
        con.execute("CREATE TABLE file_url (category_id VARCHAR, sha256 VARCHAR, url VARCHAR)")

    # Release recency: the pub_id whose own period is latest wins a tie.
    con.execute("""
        CREATE TABLE dim_release AS
        WITH r AS (
            SELECT
                md5(category_id || '|' || pub_id)    AS release_key,
                category_id, pub_id,
                any_value(release_title)             AS title,
                any_value(source_file)               AS source_file,
                any_value(source_sha256)             AS source_sha256,
                max(period)                          AS max_period
            FROM raw GROUP BY category_id, pub_id
        )
        SELECT r.*, u.url AS source_url
        FROM r LEFT JOIN file_url u
          ON u.category_id = r.category_id AND u.sha256 = r.source_sha256
    """)

    con.execute("""
        CREATE TABLE dim_product AS
        SELECT md5(category_id) AS product_key, category_id,
               any_value(product) AS product, any_value(domain) AS domain,
               any_value(subdomain) AS subdomain,
               COUNT(DISTINCT pub_id) AS n_releases
        FROM raw GROUP BY category_id
    """)

    # A series has to keep one identity across releases. Two pieces of metadata
    # drift between them, and both were part of the key, so one measure came
    # apart into several runs:
    #
    #   - The Arabic label. Some releases carry it and some do not, so a release
    #     that omitted it started a fresh series. Arabic names the same row the
    #     English does; it only carries identity when there is no English label.
    #   - The index base. Older workbooks often do not state "2023 = 100" where
    #     the parser can see it, which left those readings on an empty base.
    #
    # Together they split Real Estate's residential index into three runs ending
    # 2024-Q3, 2025-Q2 and 2026-Q2. The site charts one series per label, so it
    # showed a line stopping eighteen months before the data did.
    con.execute(r"""
        CREATE MACRO ident(en, ar) AS
            coalesce(nullif(coalesce(fold(en), ''), ''), coalesce(fold(ar), ''), '');
    """)

    # Everything that identifies a measure except which base its index is on.
    con.execute("""
        CREATE TABLE ident_raw AS
        SELECT *,
               md5(category_id || '|' || ident(row_en, row_ar) || '|' || ident(col_en, col_ar)
                   || '|' || coalesce(kind, '') || '|' || coalesce(fold("table"), '')
               ) AS measure_key
        FROM raw
    """)

    # An undeclared base is folded into a declared one only where the two agree
    # on the periods they share. A rebased index does not agree -- that is what
    # a rebase is -- so a genuine break stays two series instead of being
    # spliced into one plausible-looking line.
    con.execute("""
        CREATE TABLE base_merge AS
        WITH declared AS (
            SELECT measure_key, any_value(index_base) AS base
            FROM ident_raw WHERE coalesce(index_base, '') <> ''
            GROUP BY measure_key HAVING COUNT(DISTINCT index_base) = 1
        ),
        blank AS (
            SELECT measure_key, period, any_value(value) AS value
            FROM ident_raw WHERE coalesce(index_base, '') = ''
            GROUP BY measure_key, period
        ),
        known AS (
            SELECT i.measure_key, i.period, any_value(i.value) AS value
            FROM ident_raw i JOIN declared d USING (measure_key)
            WHERE coalesce(i.index_base, '') <> ''
            GROUP BY i.measure_key, i.period
        ),
        cmp AS (
            SELECT b.measure_key, COUNT(*) AS shared,
                   COUNT(*) FILTER (
                       WHERE abs(b.value - k.value) <= 0.001 * GREATEST(abs(k.value), 1)
                   ) AS agree
            FROM blank b JOIN known k USING (measure_key, period)
            GROUP BY b.measure_key
        )
        SELECT c.measure_key, d.base
        FROM cmp c JOIN declared d USING (measure_key)
        WHERE c.shared >= 2 AND c.agree = c.shared
    """)

    # A base belongs to an index and to nothing else. A percentage change is
    # base-independent -- it means the same thing either side of a rebase -- so
    # letting the base into the identity of a change or a level split those in
    # two for no reason, which is what truncated Real Estate's quarterly change.
    con.execute("""
        CREATE TABLE obs AS
        SELECT i.*,
               CASE WHEN coalesce(i.kind, '') <> 'index' THEN ''
                    WHEN coalesce(i.index_base, '') = '' THEN coalesce(m.base, '')
                    ELSE i.index_base END AS base_norm
        FROM ident_raw i LEFT JOIN base_merge m USING (measure_key)
    """)

    merged = con.execute("SELECT COUNT(*) FROM base_merge").fetchone()[0]
    log.info("  folded an undeclared base into a declared one for %s measures", f"{merged:,}")

    con.execute("""
        CREATE TABLE dim_series AS
        SELECT
            md5(measure_key || '|' || base_norm) AS series_key,
            md5(category_id) AS product_key,
            category_id,
            -- A release that omits a label must not blank out the name the
            -- other releases carry, so take a non-empty one where any exists.
            coalesce(max(nullif(row_en, '')), '') AS row_en,
            coalesce(max(nullif(row_ar, '')), '') AS row_ar,
            coalesce(max(nullif(col_en, '')), '') AS col_en,
            coalesce(max(nullif(col_ar, '')), '') AS col_ar,
            coalesce(kind, '') AS kind,
            base_norm AS index_base,
            any_value(period_type) AS period_type, any_value("table") AS table_name
        FROM obs
        GROUP BY category_id, measure_key, base_norm, coalesce(kind, '')
    """)

    # One reading per (series, period): the latest release that reports it.
    con.execute("""
        CREATE TABLE fact_observation AS
        WITH keyed AS (
            SELECT md5(measure_key || '|' || base_norm) AS series_key,
                   md5(category_id || '|' || pub_id) AS release_key,
                   md5(category_id) AS product_key,
                   period, period_type, value, weight_pct, "table" AS table_name,
                   row_number() OVER (
                       PARTITION BY measure_key, base_norm, period
                       ORDER BY pub_id DESC
                   ) AS rn
            FROM obs
        )
        SELECT series_key, release_key, product_key, period, period_type,
               value, weight_pct, table_name
        FROM keyed WHERE rn = 1
    """)

    con.execute("""
        CREATE TABLE fact_revision AS
        WITH keyed AS (
            SELECT md5(measure_key || '|' || base_norm) AS series_key,
                   md5(category_id || '|' || pub_id) AS release_key,
                   period, value,
                   row_number() OVER (
                       PARTITION BY measure_key, base_norm, period
                       ORDER BY pub_id DESC
                   ) AS rn
            FROM obs
        )
        SELECT series_key, release_key, period, value, rn AS revision_age
        FROM keyed WHERE rn > 1
    """)

    con.execute("""
        CREATE VIEW v_observation AS
        SELECT f.period, f.period_type, f.value, f.weight_pct, f.table_name,
               s.series_key, s.row_en, s.row_ar, s.col_en, s.col_ar, s.kind, s.index_base,
               p.category_id, p.product, p.domain, p.subdomain,
               r.pub_id, r.title AS release_title, r.source_file
        FROM fact_observation f
        JOIN dim_series  s ON s.series_key  = f.series_key
        JOIN dim_product p ON p.product_key = f.product_key
        JOIN dim_release r ON r.release_key = f.release_key
    """)

    # Series worth charting: enough points, a real time axis, one base.
    con.execute("""
        CREATE VIEW v_series_span AS
        SELECT s.series_key, s.category_id, s.row_en, s.row_ar, s.col_en, s.col_ar,
               s.kind, s.index_base, s.table_name,
               any_value(f.period_type) AS period_type,
               COUNT(*) AS n_points,
               MIN(f.period) AS first_period, MAX(f.period) AS last_period
        FROM dim_series s JOIN fact_observation f ON f.series_key = s.series_key
        GROUP BY ALL
    """)

    con.execute("""
        CREATE VIEW v_product_coverage AS
        SELECT p.category_id, p.product, p.domain, p.subdomain, p.n_releases,
               COUNT(DISTINCT f.series_key) AS n_series,
               COUNT(*) AS n_observations,
               MIN(f.period) AS first_period, MAX(f.period) AS last_period
        FROM dim_product p JOIN fact_observation f ON f.product_key = p.product_key
        GROUP BY ALL ORDER BY n_observations DESC
    """)

    # A series should hold one measure in one unit. Two things break that in the
    # source: index levels and percentage changes share row and column labels in
    # several products, and some products switch from proportions (0.99) to
    # percentages (98.9) between releases. Either way the merged line looks
    # plausible and means nothing, so the signature is flagged and kept off the
    # charts rather than silently drawn.
    con.execute("""
        CREATE VIEW v_suspect_series AS
        SELECT s.series_key, s.category_id, s.row_en, s.col_en, s.kind,
               COUNT(*) AS n_points,
               MIN(f.value) AS min_value, MAX(f.value) AS max_value
        FROM dim_series s JOIN fact_observation f ON f.series_key = s.series_key
        GROUP BY ALL
        HAVING MIN(f.value) < 10 AND MAX(f.value) > 50 AND COUNT(*) >= 3
    """)


    # Where a column header was not captured, every column of a table folds into
    # one series and several different numbers claim the same (series, period).
    # The de-duplication then has to pick one, and which one it picks is
    # arbitrary. Those series are recorded here so the site can decline to chart
    # a value it cannot uniquely identify -- the data stays, the claim does not.
    con.execute("""
        CREATE TABLE series_ambiguity AS
        SELECT series_key, MAX(n_distinct) AS max_distinct, SUM(n_distinct > 1) AS n_periods_ambiguous
        FROM (
            SELECT md5(category_id || '|' || coalesce(fold(row_en),'') || '|' || coalesce(fold(row_ar),'') || '|'
                       || coalesce(fold(col_en),'') || '|' || coalesce(kind,'') || '|'
                       || coalesce(index_base,'') || '|' || coalesce(fold("table"),'')) AS series_key,
                   period, COUNT(DISTINCT round(value, 3)) AS n_distinct
            FROM raw GROUP BY 1, 2
        ) GROUP BY series_key
    """)
    con.execute("""
        CREATE VIEW v_ambiguous_series AS
        SELECT * FROM series_ambiguity WHERE max_distinct > 1
    """)

    # What the site is allowed to draw. It lives here rather than in the
    # exporter so that the exporter and the checks cannot drift apart: a check
    # that the page charts the most recent data available is only worth
    # anything if "available" means the same thing to both.
    con.execute(f"""
        CREATE VIEW v_chartable AS
        SELECT v.* FROM v_series_span v
        WHERE v.n_points >= {MIN_CHART_POINTS}
          AND v.kind <> 'weight'
          -- a series mixing index levels with percentage changes draws a
          -- plausible-looking but meaningless line
          AND v.series_key NOT IN (SELECT series_key FROM v_suspect_series)
          -- and never chart a value the source does not pin down uniquely
          AND v.series_key NOT IN (SELECT series_key FROM v_ambiguous_series)
          -- a series that never moves is not a measurement; several tables
          -- carry a row-number column headed "Index" which parses as one
          AND v.series_key IN (
              SELECT series_key FROM fact_observation
              GROUP BY series_key HAVING MIN(value) <> MAX(value))
    """)

    log.info("--- built ---")
    for name, sql in [
        ("dim_product", "SELECT COUNT(*) FROM dim_product"),
        ("dim_release", "SELECT COUNT(*) FROM dim_release"),
        ("dim_series", "SELECT COUNT(*) FROM dim_series"),
        ("fact_observation", "SELECT COUNT(*) FROM fact_observation"),
        ("fact_revision (superseded)", "SELECT COUNT(*) FROM fact_revision"),
        ("chartable series (>=8 pts)", "SELECT COUNT(*) FROM v_series_span WHERE n_points >= 8"),
        ("mixed-measure suspects", "SELECT COUNT(*) FROM v_suspect_series"),
        ("ambiguous series", "SELECT COUNT(*) FROM v_ambiguous_series"),
        ("orphan facts",
         "SELECT COUNT(*) FROM fact_observation f LEFT JOIN dim_series s "
         "ON s.series_key = f.series_key WHERE s.series_key IS NULL"),
    ]:
        log.info("  %-28s %s", name, f"{con.execute(sql).fetchone()[0]:,}")

    con.execute("DROP TABLE raw")
    con.close()
    log.info("wrote %s (%.0f MB)", DB, DB.stat().st_size / 1048576)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
