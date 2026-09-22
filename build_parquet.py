"""
Saudi 360 — export the warehouse to Parquet for Cloudflare R2.

    python build_parquet.py     ->  dist/r2/*.parquet

The published site ships a curated few hundred series because a page cannot
carry 916,437. Parquet on R2 removes that limit without adding a server:
DuckDB-WASM in the browser reads these files over HTTP range requests and runs
real SQL against the whole corpus, so only the bytes a query touches are
fetched.

Row groups are kept small and the fact table is sorted by series so that a
query for one series reads a handful of row groups instead of the whole file.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB = ROOT / "data" / "saudi360.duckdb"
OUT = ROOT / "dist" / "r2"

# Small enough that a single-series lookup touches few groups, large enough that
# the file does not fill with per-group metadata.
ROW_GROUP = 40_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("parquet")

EXPORTS = [
    # name, query. The fact table is sorted by series so range reads are tight.
    ("fact_observation", """
        SELECT series_key, release_key, product_key, period, period_type,
               value, weight_pct, table_name
        FROM fact_observation ORDER BY series_key, period
    """),
    ("dim_series", """
        SELECT s.series_key, s.product_key, s.category_id, s.row_en, s.row_ar,
               s.col_en, s.col_ar, s.kind, s.index_base, s.period_type, s.table_name,
               (s.series_key IN (SELECT series_key FROM v_ambiguous_series)) AS ambiguous,
               (s.series_key IN (SELECT series_key FROM v_suspect_series))   AS suspect
        FROM dim_series s ORDER BY s.category_id, s.series_key
    """),
    ("dim_product", "SELECT * FROM dim_product ORDER BY category_id"),
    ("dim_release", "SELECT * FROM dim_release ORDER BY category_id, pub_id"),
    ("dim_geography", "SELECT * FROM dim_geography"),
    ("dim_place_label", "SELECT * FROM dim_place_label"),
]


def main() -> int:
    if not DB.exists():
        log.error("missing %s — build the warehouse first", DB)
        return 1
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    con = duckdb.connect(str(DB), read_only=True)
    total = 0
    for name, sql in EXPORTS:
        path = OUT / f"{name}.parquet"
        con.execute(f"""
            COPY ({sql}) TO '{path.as_posix()}'
            (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP})
        """)
        rows = con.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
        size = path.stat().st_size
        total += size
        log.info("  %-20s %10s rows %8.1f MB", name, f"{rows:,}", size / 1048576)

    # A tiny manifest so the page knows what is there without probing.
    (OUT / "manifest.json").write_text(
        '{"files":[' + ",".join(f'"{n}.parquet"' for n, _ in EXPORTS) + '],'
        f'"generated":"{con.execute("SELECT max(period) FROM fact_observation").fetchone()[0]}"}}',
        encoding="utf-8")
    con.close()
    log.info("total %.1f MB in %s", total / 1048576, OUT)
    log.info("upload with:  wrangler r2 object put saudi360/<file> --file dist/r2/<file>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
