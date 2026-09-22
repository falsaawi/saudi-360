"""
Saudi 360 — phase 4. Parsed observations -> DuckDB star schema.

    python build_warehouse.py

Builds data/saudi360.duckdb with one fact table and three dimensions, plus
quality views that make the discontinuities in the source data explicit rather
than letting a chart quietly span them.

Two discontinuities matter and both are modelled, not smoothed:
  * index base   -- GASTAT rebased CPI from 2018=100 to 2023=100
  * classification -- divisions were renamed and re-cut at the same time
A series is only continuous within one base and one classification.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DATA = ROOT / "data"
PARSED = DATA / "parsed"
DB = DATA / "saudi360.duckdb"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("warehouse")


def key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def load_observations() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(PARSED.glob("observations_*.csv")):
        with path.open(encoding="utf-8-sig") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def main() -> int:
    obs = load_observations()
    if not obs:
        log.error("no parsed observations found — run run_parse.py first")
        return 1
    log.info("loaded %d parsed observations", len(obs))

    catalogue = json.loads((DATA / "catalog" / "catalogue.json").read_text("utf-8"))
    by_cat = {c["category_id"]: c for c in catalogue}
    ar_titles = {}
    for c in catalogue:
        for r in c.get("langs", {}).get("ar", {}).get("releases", []):
            ar_titles[r["pub_id"]] = r["title"]

    products, releases, series, facts = {}, {}, {}, []

    for o in obs:
        cat = o["category_id"]
        entry = by_cat.get(cat, {})
        ar_entry = (entry.get("langs", {}).get("ar") or {})

        pkey = key("product", cat)
        products.setdefault(pkey, {
            "product_key": pkey,
            "category_id": cat,
            "product_en": entry.get("product", ""),
            "product_ar": "",
            "domain": entry.get("domain", ""),
            "subdomain": entry.get("subdomain", ""),
        })

        rkey = key("release", cat, o["pub_id"])
        releases.setdefault(rkey, {
            "release_key": rkey,
            "pub_id": o["pub_id"],
            "product_key": pkey,
            "title_en": o["release_title"],
            "title_ar": ar_titles.get(o["pub_id"], ""),
            "period": o["period"],
            "period_type": o["period_type"],
            "year": int(o["year"]),
            "month": int(o["month"]) or None,
            "index_base": o["index_base"] or None,
            "source_sha256": o["source_sha256"],
            "source_file": o["source_file"],
            "source_sheet": o["sheet"],
        })

        # A series is identified by its label within a product and level. The
        # index base is part of the identity: the same division on a different
        # base is a different, non-splicable series.
        skey = key("series", cat, o["level"], norm(o["label_en"] or o["label_ar"]),
                   o["index_base"] or "")
        s = series.setdefault(skey, {
            "series_key": skey,
            "product_key": pkey,
            "category_id": cat,
            "level": int(o["level"]),
            "label_en": o["label_en"],
            "label_ar": o["label_ar"],
            "unit": o["unit"],
            "index_base": o["index_base"] or None,
        })
        if not s["label_ar"] and o["label_ar"]:
            s["label_ar"] = o["label_ar"]

        facts.append({
            "series_key": skey,
            "release_key": rkey,
            "product_key": pkey,
            "period": o["period"],
            "period_type": o["period_type"],
            "value": float(o["value"]),
            "unit": o["unit"],
            "index_base": o["index_base"] or None,
            "weight_pct": float(o["weight_pct"]) if o["weight_pct"] else None,
        })

    if DB.exists():
        DB.unlink()
    con = duckdb.connect(str(DB))
    con.execute("CREATE SCHEMA IF NOT EXISTS main")

    def table(name: str, rows: list[dict]) -> None:
        con.register("tmp", __import__("pandas").DataFrame(rows))
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM tmp")
        con.unregister("tmp")
        log.info("  %-18s %6d rows", name, len(rows))

    table("dim_product", list(products.values()))
    table("dim_release", list(releases.values()))
    table("dim_series", list(series.values()))
    table("fact_observation", facts)

    con.execute("""
        CREATE VIEW v_observation AS
        SELECT f.period, f.period_type, f.value, f.unit, f.index_base, f.weight_pct,
               s.series_key, s.level, s.label_en, s.label_ar,
               p.category_id, p.product_en, p.domain, p.subdomain,
               r.pub_id, r.title_en AS release_title, r.source_file, r.source_sheet
        FROM fact_observation f
        JOIN dim_series  s ON s.series_key  = f.series_key
        JOIN dim_product p ON p.product_key = f.product_key
        JOIN dim_release r ON r.release_key = f.release_key
    """)

    # Every series, with the span it is valid over. Anything that looks like a
    # single long run but spans two bases will show up as two rows here.
    con.execute("""
        CREATE VIEW v_series_span AS
        SELECT s.series_key, s.category_id, s.level, s.label_en, s.label_ar,
               s.index_base, COUNT(*) AS n_obs,
               MIN(f.period) AS first_period, MAX(f.period) AS last_period
        FROM dim_series s JOIN fact_observation f ON f.series_key = s.series_key
        GROUP BY ALL
    """)

    # Labels that exist on one base but not the other: the classification
    # changed at the rebase, so these are genuine breaks, not parser misses.
    con.execute("""
        CREATE VIEW v_classification_break AS
        WITH per_label AS (
            SELECT level, label_en, index_base, COUNT(*) AS n
            FROM v_observation WHERE level <= 1 GROUP BY ALL
        )
        SELECT level, label_en,
               MAX(CASE WHEN index_base = '2018' THEN n END) AS obs_2018_base,
               MAX(CASE WHEN index_base = '2023' THEN n END) AS obs_2023_base
        FROM per_label GROUP BY ALL
        HAVING obs_2018_base IS NULL OR obs_2023_base IS NULL
    """)

    con.execute("""
        CREATE VIEW v_weight_check AS
        SELECT pub_id, release_title, index_base, ROUND(SUM(weight_pct), 2) AS weight_sum
        FROM v_observation WHERE level = 1 AND weight_pct IS NOT NULL
        GROUP BY ALL
    """)

    log.info("--- checks ---")
    for label, sql in [
        ("observations", "SELECT COUNT(*) FROM fact_observation"),
        ("series", "SELECT COUNT(*) FROM dim_series"),
        ("releases", "SELECT COUNT(*) FROM dim_release"),
        ("distinct periods", "SELECT COUNT(DISTINCT period) FROM fact_observation"),
        ("weights outside 95-105",
         "SELECT COUNT(*) FROM v_weight_check WHERE weight_sum NOT BETWEEN 95 AND 105"),
        ("classification breaks", "SELECT COUNT(*) FROM v_classification_break"),
        ("orphan facts",
         "SELECT COUNT(*) FROM fact_observation f "
         "LEFT JOIN dim_series s ON s.series_key = f.series_key "
         "WHERE s.series_key IS NULL"),
    ]:
        log.info("  %-24s %s", label, con.execute(sql).fetchone()[0])

    con.close()
    log.info("wrote %s (%.1f MB)", DB, DB.stat().st_size / 1048576)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
