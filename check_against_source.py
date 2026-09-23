"""
Saudi 360 — assert known cells against the warehouse.

    python check_against_source.py

Every expectation below was read by eye out of a GASTAT workbook or quoted from
the prose of a GASTAT release. The parser is free to change; these numbers are
not. If a refactor starts reading the wrong column, this fails loudly here
rather than quietly on the site.

Exit code is non-zero when any check fails, so it can gate a deploy.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB = ROOT / "data" / "saudi360.duckdb"
APP = ROOT / "site" / "app.json"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("check")

# (name, expected, tolerance, SQL). The SQL must return exactly one number.
CHECKS = [
    ("CPI general index, Aug 2026 (workbook 2.1)", 105.782, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='121421' AND table_name='2.1' AND period='2026-08'
          AND lower(row_en)='general index' AND kind='index'
          AND lower(col_en) LIKE 'index numbers%'
    """),
    ("CPI annual inflation, Aug 2026 (GASTAT prose: 1.8%)", 1.8167, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='121421' AND table_name='2.1' AND period='2026-08'
          AND lower(row_en)='general index' AND kind='change'
          AND lower(col_en) LIKE 'percent change%'
    """),
    ("Industrial production, annual change Jul 2026 (prose: -8.1%)", -0.0813, 0.001, """
        SELECT DISTINCT value FROM v_observation
        WHERE category_id='123454' AND period='2026-07'
          AND lower(row_en)='general index' AND kind='change'
          AND lower(col_en) LIKE '%annual%'
    """),
    ("Saudi unemployment, Q1 2026 (prose: 6.4%)", 6.367, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='417515' AND table_name='1'
          AND lower(row_en)='unemployment rate' AND lower(col_en)='saudi total'
        ORDER BY period DESC LIMIT 1
    """),
    ("Overall unemployment, Q1 2026 (prose: 3.1%)", 3.054, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='417515' AND table_name='1'
          AND lower(row_en)='unemployment rate' AND lower(col_en)='total'
        ORDER BY period DESC LIMIT 1
    """),
    ("Saudi participation, Q1 2026 (prose: 49.0%)", 48.958, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='417515' AND table_name='1'
          AND lower(row_en) LIKE 'labour force participation%'
          AND lower(col_en)='saudi total'
        ORDER BY period DESC LIMIT 1
    """),
    ("Real estate general index, Q2 2026 (workbook sheet 1)", 106.34, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='121920' AND period='2026-Q2'
          AND lower(row_en)='general index' AND kind='index'
          AND lower(col_en) LIKE 'index numbers%'
    """),
    ("Real estate, Villa, Q2 2026 (workbook sheet 1)", 94.89, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='121920' AND period='2026-Q2'
          AND lower(row_en)='villa' AND kind='index'
          AND lower(col_en) LIKE 'index numbers%'
    """),
    ("Real estate time series, 2021-Q1 index (workbook sheet 2)", 81.37, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='121920' AND period='2021-Q1' AND kind='index'
          AND lower(row_en) LIKE '%index numbers%' LIMIT 1
    """),
    # A year banner merged over four quarter columns, with the row names in a
    # column of their own beside the serial numbers: the shape that produced
    # rows called "1", "2", "3" and a column called "*".
    ("GDP at current prices, Q2 2026 (workbook 1.1)", 1263831.0, 1.0, """
        SELECT value FROM v_observation
        WHERE category_id='120038' AND table_name='1.1' AND period='2026-Q2'
          AND row_en='Gross Domestic Product'
    """),
    ("GDP oil activities, Q2 2026 (workbook 1.1)", 305849.0, 1.0, """
        SELECT value FROM v_observation
        WHERE category_id='120038' AND table_name='1.1' AND period='2026-Q2'
          AND row_en='Oil Activities'
    """),
    # A period that runs DOWN the sheet across two columns (year, then month),
    # with quarter subtotals interleaved among the months.
    ("Goods exports, Jun 2026 (workbook 1.1, million SAR)", 87762.0158, 0.01, """
        SELECT value FROM v_observation
        WHERE category_id='123481' AND table_name='1.1' AND period='2026-06'
          AND lower(row_en) LIKE 'goods exports%'
    """),
    # Every column but the first of each group inherits its year from a merged
    # banner; before that was read, these were all dated to the release year.
    ("Total population, 2024 (GASTAT: 35.3 million)", 35300280.0, 1.0, """
        SELECT value FROM v_observation
        WHERE category_id='2366987' AND period='2024'
          AND lower(row_en)='total' AND col_en='Total'
    """),
    ("Total population, 2023 (same table, earlier year banner)", 33702731.0, 1.0, """
        SELECT value FROM v_observation
        WHERE category_id='2366987' AND period='2023'
          AND lower(row_en)='total' AND col_en='Total'
    """),
]

# Structural properties that should hold for the corpus as a whole.
SHAPE = [
    # Header rows that bleed into a data block carry the period across their
    # columns, so every number in them is a year. is_header_row() drops them at
    # the parser, and the count is now genuinely zero rather than merely small.
    ("index values that are bare years, per 100k", 0, """
        SELECT CAST(round(100000.0 * COUNT(*) FILTER (
                   WHERE value BETWEEN 1990 AND 2030 AND value = floor(value)
                     AND series_key IN (SELECT series_key FROM dim_series WHERE kind='index')
               ) / GREATEST(COUNT(*), 1)) AS INTEGER)
        FROM fact_observation
    """),
    ("no observation predates 1960", 0, """
        SELECT COUNT(*) FROM fact_observation
        WHERE TRY_CAST(substr(period,1,4) AS INTEGER) < 1960
    """),
    ("no orphan facts", 0, """
        SELECT COUNT(*) FROM fact_observation f
        LEFT JOIN dim_series s USING (series_key) WHERE s.series_key IS NULL
    """),
]

# Products whose charts a reader is most likely to open, and which must
# therefore open on current figures. Pinned by hand: a generated list would
# quietly shrink the moment one of them broke.
HEADLINE_PRODUCTS = {
    "120038": "Gross Domestic Product",
    "121421": "Consumer Prices",
    "121920": "Real Estate Prices",
    "123454": "Industrial Production",
    "123481": "International Trade",
    "417515": "Labour Market",
}


def year(period: str) -> int:
    """The year a period string names, or 0. Period strings are never compared
    whole across types: "2026-03" and "2026-Q1" are the same quarter written
    two ways, and comparing the text reports a gap that is not there."""
    head = (period or "")[:4]
    return int(head) if head.isdigit() else 0


def check_site_currency(con) -> int:
    """Assert the site charts the newest data it is able to chart.

    Every value assertion above passed twice while the site drew series that
    stopped two years before the data did -- once because one measure had come
    apart into several runs, once because a discontinued run outranked the live
    one on length. Checking values says nothing about WHICH series a reader is
    shown, so these check that instead.

    The comparison is against v_chartable, the warehouse's own definition of
    what may be drawn, not against the product's raw coverage: a product whose
    recent releases publish single-period tables has nothing current to chart,
    and failing it for that would be a check that cries wolf until it is
    deleted.
    """
    if not APP.exists():
        log.error("\n=== what the site charts ===")
        log.error("  FAIL  missing %s -- run export_site.py", APP.name)
        return 1

    app = json.loads(APP.read_text(encoding="utf-8"))
    reach = {
        cat: last for cat, last in con.execute("""
            SELECT category_id, MAX(last_period) FROM v_chartable GROUP BY 1
        """).fetchall()
    }
    failed = 0
    log.info("\n=== what the site charts ===")

    stale_named = []
    for p in app["products"]:
        if not p["series"]:
            continue
        drawn = max(s["pts"][-1][0] for s in p["series"])
        best = reach.get(p["id"], "")
        # No slack here: this asks only whether the newest chartable series is
        # charted at all, and some series in the product reaches that period by
        # construction.
        if year(drawn) < year(best):
            stale_named.append((p["name"], drawn, best))

    if stale_named:
        log.error("  FAIL  %-58s %d product(s)",
                  "charts reach the newest chartable series", len(stale_named))
        for nm, drawn, best in stale_named[:6]:
            log.error("          %-40s charts to %s, could reach %s", nm[:40], drawn, best)
        failed += 1
    else:
        log.info("  ok    %-58s %s", "charts reach the newest chartable series",
                 f"{len(app['products'])} products")

    # The first series is the one the page opens on, so it carries the burden
    # of the product's headline: it is what a reader sees before touching a
    # control, and it is exactly what was wrong on Consumer Prices.
    for cat, label in sorted(HEADLINE_PRODUCTS.items(), key=lambda kv: kv[1]):
        p = next((x for x in app["products"] if x["id"] == cat), None)
        if p is None or not p["series"]:
            log.error("  FAIL  %-58s no charted series", f"{label}: opens on current data")
            failed += 1
            continue
        head = p["series"][0]
        first = head["pts"][-1][0]
        best = reach.get(cat, "")
        # Slack for an annual series only. Giving every series a year of it let
        # Industrial Production open on a monthly run ending a year behind the
        # monthly data and still be called current.
        slack = 1 if head.get("freq") == "annual" else 0
        if year(first) < year(best) - slack:
            log.error("  FAIL  %-58s opens on %s, data to %s",
                      f"{label}: opens on current data", first, best)
            failed += 1
        else:
            log.info("  ok    %-58s %s", f"{label}: opens on current data", first)

    return failed


def main() -> int:
    if not DB.exists():
        log.error("missing %s", DB)
        return 1
    con = duckdb.connect(str(DB), read_only=True)
    failed = 0

    log.info("=== values against the source ===")
    for name, expect, tol, sql in CHECKS:
        try:
            rows = con.execute(sql).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.error("  FAIL  %-58s query error: %s", name, exc)
            failed += 1
            continue
        if not rows:
            log.error("  FAIL  %-58s no value found", name)
            failed += 1
            continue
        if len({round(r[0], 6) for r in rows}) > 1:
            log.error("  FAIL  %-58s %d different values match", name, len(rows))
            failed += 1
            continue
        got = rows[0][0]
        if abs(got - expect) <= tol:
            log.info("  ok    %-58s %s", name, f"{got:.4f}")
        else:
            log.error("  FAIL  %-58s expected %s, got %s", name, expect, f"{got:.4f}")
            failed += 1

    log.info("\n=== corpus shape ===")
    for name, expect, sql in SHAPE:
        got = con.execute(sql).fetchone()[0]
        if got == expect:
            log.info("  ok    %-58s %s", name, got)
        else:
            log.error("  FAIL  %-58s expected %s, got %s", name, expect, f"{got:,}")
            failed += 1

    failed += check_site_currency(con)

    # Reported for context rather than asserted: these move as coverage improves.
    log.info("\n=== coverage (informational) ===")
    for label, sql in [
        ("series with 2+ points",
         "SELECT COUNT(*) FROM (SELECT series_key FROM fact_observation GROUP BY 1 HAVING COUNT(*)>1)"),
        ("series with 9+ points",
         "SELECT COUNT(*) FROM (SELECT series_key FROM fact_observation GROUP BY 1 HAVING COUNT(*)>=9)"),
        ("products with a chartable series",
         """SELECT COUNT(DISTINCT s.category_id) FROM dim_series s
            WHERE s.series_key IN (SELECT series_key FROM fact_observation
                                   GROUP BY 1 HAVING COUNT(*)>=3)"""),
    ]:
        log.info("  %-58s %s", label, f"{con.execute(sql).fetchone()[0]:,}")

    con.close()
    if failed:
        log.error("\n%d check(s) FAILED", failed)
        return 1
    log.info("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
