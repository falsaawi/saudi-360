"""
Saudi 360 — phase 6 export: the regional map and the Vision 2030 lens.

    python export_geo.py     ->  site/geo.json

MAP. `v_map_metric` holds 593 measures that cover at least 8 of the 13
administrative regions in a single period. Shipping all of them with full
history would be tens of megabytes, so this takes the widest-covering few per
product and their most recent periods.

LENS. Vision 2030 organises the national programme under three themes. The
grouping below assigns GASTAT products to those themes so indicators can be read
by national goal rather than by producing department. It is OUR grouping of
GASTAT statistics for navigation — these are not Vision 2030's official KPIs,
and no official target values are asserted anywhere in this project.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB = ROOT / "data" / "saudi360.duckdb"
OUT = ROOT / "site" / "geo.json"

METRICS_PER_PRODUCT = 8
MAX_PERIODS = 16

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("geo-export")

# Vision 2030's three themes. With 94 products a hand-kept list of ids goes stale
# the moment the scope changes, so products are assigned by what their sub-domain
# measures, with a few explicit moves where the domain and the theme disagree.
THEMES = [
    {"id": "society", "en": "A Vibrant Society", "ar": "مجتمع حيوي",
     "note_en": "Quality of life, the two Holy Mosques, culture, health and the environment.",
     "note_ar": "جودة الحياة والحرمان الشريفان والثقافة والصحة والبيئة."},
    {"id": "economy", "en": "A Thriving Economy", "ar": "اقتصاد مزدهر",
     "note_en": "Prices, production, trade, investment, energy and the labour market.",
     "note_ar": "الأسعار والإنتاج والتجارة والاستثمار والطاقة وسوق العمل."},
    {"id": "nation", "en": "An Ambitious Nation", "ar": "وطن طموح",
     "note_en": "Public services, government effectiveness and the national statistical base.",
     "note_ar": "الخدمات العامة وفاعلية الجهات الحكومية والقاعدة الإحصائية الوطنية."},
]

# Sub-domain keywords -> theme. First match wins; the domain decides the rest.
SUBDOMAIN_THEME = [
    ("society", ("tourism", "hajj", "umrah", "environment", "health", "sport", "life styles",
                 "living conditions", "gender", "education", "census", "vitals", "demographic",
                 "housing", "social", "culture")),
    ("nation", ("spatial", "service statistics", "administrative")),
    ("economy", ("price", "business", "trade", "investment", "national accounts", "economic",
                 "labor", "labour", "energy", "agriculture", "transport", "digital",
                 "manufactur", "construction")),
]
DOMAIN_THEME = {
    "Economic Statistics": "economy",
    "Social Statistics": "society",
    "Environmental and Spatial Statistics": "society",
    "Archived Data": "nation",
}


def theme_for(domain: str, subdomain: str, product: str) -> str:
    text = f"{subdomain} {product}".lower()
    for theme, words in SUBDOMAIN_THEME:
        if any(w in text for w in words):
            return theme
    return DOMAIN_THEME.get(domain, "nation")


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)

    regions = [{"id": r[0], "en": r[1], "ar": r[2]} for r in con.execute("""
        SELECT DISTINCT region_id, region_en, region_ar FROM dim_geography ORDER BY region_id
    """).fetchall()]

    # Widest-covering metrics per product, most recent first.
    picked = con.execute(f"""
        WITH agg AS (
            SELECT category_id, any_value(product) AS product, any_value(domain) AS domain,
                   metric_en, any_value(metric_ar) AS metric_ar, any_value(kind) AS kind,
                   MAX(n_regions) AS reach, COUNT(DISTINCT period) AS n_periods,
                   MAX(period) AS latest
            FROM v_map_metric
            -- Some tables put months down the side and cities across the top, so
            -- the "metric" comes back as "August". A month is a period, not a
            -- measure, and colouring a map by it means nothing.
            WHERE NOT regexp_matches(lower(metric_en),
                  '^(january|february|march|april|may|june|july|august|september|october|november|december|q[1-4]|[0-9]{4})$')
            GROUP BY category_id, metric_en
        ), ranked AS (
            SELECT *, row_number() OVER (
                       PARTITION BY category_id
                       ORDER BY reach DESC, n_periods DESC, metric_en
                   ) AS rn
            FROM agg
        )
        SELECT category_id, product, domain, metric_en, metric_ar, kind,
               reach, n_periods, latest
        FROM ranked WHERE rn <= {METRICS_PER_PRODUCT}
        ORDER BY product, reach DESC
    """).fetchall()

    metrics, values = [], {}
    # Stripping the leading month can make two measures share a name ("April
    # Total" and "March Total" both become "Total"); keep the first, which is
    # the widest-covering, rather than offering the reader identical choices.
    seen_names: set[tuple[str, str]] = set()
    for i, (cat, product, domain, m_en, m_ar, kind, reach, n_per, latest) in enumerate(picked):
        display = strip_period(m_en)
        if (cat, display.lower()) in seen_names:
            continue
        seen_names.add((cat, display.lower()))
        mid = f"m{i}"
        rows = con.execute(f"""
            SELECT period, region_id, avg(value) AS v
            FROM v_regional
            WHERE category_id = ? AND metric_en = ? AND place_kind IN ('city','region')
              AND series_key NOT IN (SELECT series_key FROM v_ambiguous_series)
            GROUP BY 1, 2
            QUALIFY dense_rank() OVER (ORDER BY period DESC) <= {MAX_PERIODS}
            ORDER BY period
        """, [cat, m_en]).fetchall()
        if not rows:
            continue
        by_period: dict[str, dict] = {}
        for period, region, v in rows:
            by_period.setdefault(period, {})[region] = round(v, 3)
        metrics.append({
            "id": mid, "product_id": cat, "product": product, "domain": domain,
            "metric": display, "metric_ar": strip_period(m_ar or ""), "kind": kind,
            "reach": reach, "latest": latest,
            "periods": sorted(by_period, key=period_key),
        })
        values[mid] = by_period

    themes = []
    prod_meta = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT category_id, product, domain, subdomain FROM dim_product").fetchall()}
    assigned: dict[str, list[str]] = {t["id"]: [] for t in THEMES}
    for pid, (name, domain, sub) in prod_meta.items():
        assigned[theme_for(domain, sub or "", name)].append(pid)
    for th in THEMES:
        members = [{"id": pid, "name": prod_meta[pid][0]}
                   for pid in sorted(assigned[th["id"]], key=lambda x: prod_meta[x][0])]
        if not members:
            continue
        stats = con.execute(f"""
            SELECT COUNT(*) , COUNT(DISTINCT series_key), MIN(period), MAX(period)
            FROM v_observation WHERE category_id IN ({','.join('?' * len(members))})
        """, [m["id"] for m in members]).fetchone()
        themes.append({**{k: th[k] for k in ("id", "en", "ar", "note_en", "note_ar")},
                       "products": members, "n_obs": stats[0], "n_series": stats[1],
                       "first": stats[2], "last": stats[3]})

    coverage = con.execute("""
        SELECT region_id, COUNT(*) AS obs, COUNT(DISTINCT category_id) AS products
        FROM v_regional GROUP BY 1 ORDER BY 1
    """).fetchall()

    payload = {
        "regions": regions,
        "metrics": metrics,
        "values": values,
        "themes": themes,
        "coverage": {r[0]: {"obs": r[1], "products": r[2]} for r in coverage},
        "mapped_obs": con.execute("SELECT COUNT(*) FROM v_regional").fetchone()[0],
        "total_obs": con.execute("SELECT COUNT(*) FROM v_observation").fetchone()[0],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("metrics: %d across %d products", len(metrics),
             len({m["product_id"] for m in metrics}))
    log.info("themes: %s", [(t["id"], len(t["products"])) for t in themes])
    log.info("wrote %s (%.2f MB)", OUT, OUT.stat().st_size / 1048576)
    con.close()
    return 0


MONTH_PREFIX = re.compile(
    r"^\s*(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|q[1-4]|\d{4})\b[\s:,\-]*", re.IGNORECASE)


def strip_period(label: str) -> str:
    """Drop a leading month or year from a measure name.

    Tables that run months down the side leave the metric reading "April Males";
    the period is already a separate field, so the month is noise in the name.
    """
    out = MONTH_PREFIX.sub("", label or "").strip()
    return out or (label or "").strip()


def period_key(p: str) -> tuple:
    parts = p.split("-")
    year = int(parts[0])
    if len(parts) == 1:
        return (year, 0)
    sub = parts[1]
    return (year, int(sub[1:]) * 3 if sub.startswith("Q") else int(sub))


if __name__ == "__main__":
    raise SystemExit(main())
