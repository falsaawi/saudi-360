"""
Saudi 360 — phase 6. Attach a geography dimension to the warehouse.

    python build_geography.py

GASTAT labels places inconsistently: CPI reports 16 CITIES, most other products
report the 13 ADMINISTRATIVE REGIONS, and trade reports ports and airports.
Region names arrive in many spellings ("Al-Baha", "Al Bahah", "AL - Baha").

Matching is on exact normalised labels, never substrings. That matters: "Thailand"
contains "hail" and "Harari" contains "arar", so a substring rule would invent
Saudi regions out of coffee and rice. Anything the gazetteer does not recognise
stays unmapped and is reported, rather than being guessed into a region.

Region ids are those of the published topology used by the map
(datamaps sau.topo.json), so the join needs no second lookup.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB = ROOT / "data" / "saudi360.duckdb"
TOPO = ROOT / "site" / "geo" / "sau.topo.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("geo")

# id -> (english, arabic). Ids and English names come from the topology file.
REGIONS = {
    "SA.RI": ("Ar Riyad", "منطقة الرياض"),
    "SA.MK": ("Makkah", "منطقة مكة المكرمة"),
    "SA.MD": ("Al Madinah", "منطقة المدينة المنورة"),
    "SA.QS": ("Al Quassim", "منطقة القصيم"),
    "SA.SH": ("Ash Sharqiyah", "المنطقة الشرقية"),
    "SA.AS": ("`Asir", "منطقة عسير"),
    "SA.TB": ("Tabuk", "منطقة تبوك"),
    "SA.HA": ("Ha'il", "منطقة حائل"),
    "SA.HS": ("Al Hudud ash Shamaliyah", "منطقة الحدود الشمالية"),
    "SA.JZ": ("Jizan", "منطقة جازان"),
    "SA.NJ": ("Najran", "منطقة نجران"),
    "SA.BA": ("Al Bahah", "منطقة الباحة"),
    "SA.JF": ("Al Jawf", "منطقة الجوف"),
}

# Every label form seen in the corpus, mapped to its region and what it denotes.
# kind: region | city | gateway (a port or airport, which sits in a region but
# measures traffic, not the region's population or prices).
GAZETTEER: dict[str, tuple[str, str]] = {}


def add(region: str, kind: str, *labels: str) -> None:
    for lab in labels:
        GAZETTEER[norm(lab)] = (region, kind)


def norm(value: str) -> str:
    """Fold a place label to a comparable key."""
    out = unicodedata.normalize("NFKD", value or "")
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = out.lower().replace("`", "").replace("'", "").replace("’", "")
    out = re.sub(r"[\-–_/(),.]+", " ", out)
    out = re.sub(r"\b(al|ar|ash|as|el)\s+", "", out)          # article variants
    out = re.sub(r"\b(mukarramah|munawwarah|province|region|regions|area|areas|"
                 r"emirate|governorate|city)\b", " ", out)
    return " ".join(out.split())


add("SA.RI", "region", "Riyadh Region", "Ar Riyad", "Ar Riyadh", "Al Riyadh", "Riyadh")
add("SA.MK", "region", "Makkah", "Makkah Al Mukarramah", "Mecca", "Makkah Region")
add("SA.MD", "region", "Al Madinah", "Al Madinah Al Munawwarah", "Madinah", "Medina Region")
add("SA.QS", "region", "Al Qassim", "Qassim", "Al-Qassim", "Al Quassim", "Qasim",
    "Al Qaseem", "Qaseem")
add("SA.SH", "region", "Eastern Region", "Eastern Province", "Ash Sharqiyah", "Al Sharqiyah",
    "The Eastern Region")
add("SA.AS", "region", "Asir", "Aseer", "`Asir", "Asir Region")
add("SA.TB", "region", "Tabuk", "Tabuk Region", "Tabouk")
add("SA.HA", "region", "Hail", "Ha'il", "Hail Region", "Ha il")
add("SA.HS", "region", "Northern Borders", "Al Hudud ash Shamaliyah", "Northern Border",
    "Northern Boarders")
add("SA.JZ", "region", "Jazan", "Jizan", "Jazan Region")
add("SA.NJ", "region", "Najran", "Najran Region")
add("SA.BA", "region", "Al Baha", "Al Bahah", "Baha", "Bahah", "Al-Baha")
add("SA.JF", "region", "Al Jouf", "Al Jawf", "Jouf", "Jawf", "Al-Jouf")

# The 16 cities in the CPI and price-average panels.
add("SA.RI", "city", "Riyadh")
add("SA.MK", "city", "Jeddah", "Makkah", "Taif", "Al Taif")
add("SA.MD", "city", "Medina", "Madinah", "Al Madinah")
add("SA.QS", "city", "Buraydah", "Buraidah")
add("SA.SH", "city", "Dammam", "Alhofof", "Al Hofof", "Hofuf", "Al Ahsa")
add("SA.AS", "city", "Abha")
add("SA.TB", "city", "Tabuk")
add("SA.HA", "city", "Hail")
add("SA.HS", "city", "Arar")
add("SA.JZ", "city", "Jazzan", "Jazan")
add("SA.NJ", "city", "Njran", "Najran")
add("SA.BA", "city", "Baha")
add("SA.JF", "city", "Skaka", "Sakaka")

# Trade gateways: real places, but they measure flows through a point.
add("SA.RI", "gateway", "Riyadh Dry Port", "King Khalid International Airport")
add("SA.MK", "gateway", "Jeddah Islamic Sea Port", "King Abdulaziz International Airport",
    "Taif Airport")
add("SA.SH", "gateway", "King Abdulaziz Sea Port", "Dammam Airport", "King Fahd International Airport",
    "Ras Tanura", "Jubail Sea Port", "Al Batha", "Salwa", "Al Khafji")
add("SA.AS", "gateway", "Abha Airport")
add("SA.MD", "gateway", "Prince Mohammed Bin Abdulaziz Airport", "Yanbu Sea Port")
add("SA.JZ", "gateway", "Jazan Sea Port", "Jizan Sea Port")
add("SA.HS", "gateway", "Jadidat Arar", "Judaidat Arar")
add("SA.TB", "gateway", "Duba Sea Port", "Al Haditha")


def resolve(label: str) -> tuple[str, str] | None:
    """The region a label names, or None. Matches whole words, never substrings."""
    key = norm(label)
    if not key:
        return None
    hit = GAZETTEER.get(key)
    if hit:
        return hit
    words = key.split()
    # Longest run first, so "al madinah al munawwarah" wins over "al madinah".
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            hit = GAZETTEER.get(" ".join(words[i:i + size]))
            if hit:
                return hit
    return None

def main() -> int:
    if not TOPO.exists():
        log.error("missing %s — fetch the topology first", TOPO)
        return 1
    topo = json.loads(TOPO.read_text("utf-8"))
    ids = {g.get("id") for g in topo["objects"]["sau"]["geometries"]}
    unknown = set(REGIONS) - ids
    if unknown:
        log.error("region ids not in the topology: %s", sorted(unknown))
        return 1
    log.info("topology has %d regions; gazetteer covers %d with %d label forms",
             len(ids - {"-99"}), len(REGIONS), len(GAZETTEER))

    con = duckdb.connect(str(DB))
    con.execute("DROP TABLE IF EXISTS dim_geography")
    con.execute("""
        CREATE TABLE dim_geography (
            place_key VARCHAR, label VARCHAR, region_id VARCHAR,
            region_en VARCHAR, region_ar VARCHAR, kind VARCHAR
        )
    """)
    rows = [(k, k, rid, REGIONS[rid][0], REGIONS[rid][1], kind)
            for k, (rid, kind) in GAZETTEER.items()]
    con.executemany("INSERT INTO dim_geography VALUES (?,?,?,?,?,?)", rows)

    # Resolution runs here, in Python, over the distinct labels in the corpus:
    # the n-gram rule does not express cleanly in SQL, and there are only tens of
    # thousands of distinct labels to resolve once.
    labels = [r[0] for r in con.execute("""
        SELECT DISTINCT row_en AS l FROM v_observation WHERE row_en <> ''
        UNION SELECT DISTINCT col_en FROM v_observation WHERE col_en <> ''
    """).fetchall()]
    resolved = [(lab, *hit) for lab in labels if (hit := resolve(lab))]
    log.info("resolved %d of %d distinct labels to a region", len(resolved), len(labels))

    con.execute("DROP TABLE IF EXISTS dim_place_label")
    con.execute("CREATE TABLE dim_place_label (label VARCHAR, region_id VARCHAR, kind VARCHAR)")
    con.executemany("INSERT INTO dim_place_label VALUES (?,?,?)", resolved)

    con.execute("""
        CREATE OR REPLACE VIEW v_regional AS
        SELECT o.*, g.region_id, g.region_en, g.region_ar, pl.kind AS place_kind,
               CASE WHEN pr.label IS NOT NULL THEN o.row_en ELSE o.col_en END AS place_label,
               CASE WHEN pr.label IS NOT NULL THEN o.col_en ELSE o.row_en END AS metric_en,
               CASE WHEN pr.label IS NOT NULL THEN o.col_ar ELSE o.row_ar END AS metric_ar
        FROM v_observation o
        LEFT JOIN dim_place_label pr ON pr.label = o.row_en
        LEFT JOIN dim_place_label pc ON pc.label = o.col_en
        JOIN dim_place_label pl ON pl.label = coalesce(pr.label, pc.label)
        JOIN (SELECT DISTINCT region_id, region_en, region_ar FROM dim_geography) g
          ON g.region_id = pl.region_id
    """)

    # A measure is mappable when one value, in one period, covers enough of the
    # country to colour a map without large blank areas.
    con.execute("""
        CREATE OR REPLACE VIEW v_map_metric AS
        SELECT category_id, product, domain, metric_en, metric_ar, kind, period, period_type,
               COUNT(DISTINCT region_id) AS n_regions,
               COUNT(*) AS n_values
        FROM v_regional
        WHERE place_kind IN ('city', 'region') AND metric_en <> ''
          AND series_key NOT IN (SELECT series_key FROM v_ambiguous_series)
        GROUP BY ALL
        HAVING COUNT(DISTINCT region_id) >= 8
    """)

    mapped = con.execute("SELECT COUNT(*) FROM v_regional").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM v_observation").fetchone()[0]
    log.info("mapped %s of %s observations to a region (%.1f%%)",
             f"{mapped:,}", f"{total:,}", mapped / total * 100)

    by_kind = con.execute("SELECT place_kind, COUNT(*) FROM v_regional GROUP BY 1 ORDER BY 2 DESC").fetchall()
    log.info("by place kind: %s", dict(by_kind))
    log.info("regions covered: %s",
             con.execute("SELECT COUNT(DISTINCT region_id) FROM v_regional").fetchone()[0])
    log.info("products with regional data: %s",
             con.execute("SELECT COUNT(DISTINCT category_id) FROM v_regional").fetchone()[0])

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
