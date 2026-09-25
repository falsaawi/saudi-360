"""
Saudi 360 — export the warehouse to static JSON for the site.

    python export_site.py

318,686 series is far more than a page should ship, so this picks what is worth
showing and writes site/app.json:

  * per product: the longest, most distinct time series (headline first)
  * per product: the latest-period breakdown, for bar and share charts
  * per product: coverage, releases and provenance
  * cross-product: a comparison set indexed to 100 for the explorer

Selection favours series with a real time axis and a single index base, because
those are the ones that can honestly be drawn as a line.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB = ROOT / "data" / "saudi360.duckdb"
OUT = ROOT / "site" / "app.json"

MAX_SERIES_PER_PRODUCT = 45
MIN_POINTS = 3
# Below this a series is too thin to carry a product's headline slot, however
# promising its label reads.
THIN_POINTS = 8
MAX_BREAKDOWN = 20

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("export")

HEADLINE = re.compile(r"general index|total|الاجمالي|الرقم القياسي العام|overall", re.I)
NOISE = re.compile(r"^\s*(s/?n|no\.?|code|رمز|م)\s*$", re.I)


# A base note stranded in front of the name: "2023=100 Al Jouf". It is real
# information -- it is what tells two bases of the same city apart -- so it is
# moved to the end rather than dropped.
BASE_PREFIX = re.compile(r"^\(?\s*((?:19|20)\d{2})\s*=\s*100\s*\)?\s*[:\-–]?\s*")


def tidy(label: str) -> str:
    out = " ".join((label or "").split())
    if out.isupper() and len(out) > 3:
        out = out.title()
    m = BASE_PREFIX.match(out)
    if m:
        rest = BASE_PREFIX.sub("", out).strip()
        if rest:
            out = f"{rest} ({m.group(1)}=100)"
    return out


# Headline indicators for the front page. Each is pinned to one exact series
# rather than found by heuristic, and each has been checked against the figure
# GASTAT states in prose in its own PDF release. A spec that does not resolve to
# exactly one series is dropped with a warning: a front-page number that might
# be the wrong column is worse than no number at all.
HEADLINES = [
    {"id": "inflation", "en": "Inflation", "ar": "التضخم",
     "note_en": "Consumer price index, annual change",
     "note_ar": "الرقم القياسي لأسعار المستهلك، التغير السنوي",
     "category": "121421", "table": "2.1", "row": "general index", "kind": "change",
     # The column used to be headed "Percent Change in August 2026"; the period
     # now lives in the period field, so match what is left of the wording.
     "col_like": "percent change%", "unit": "%", "scale": 1, "verified": "GASTAT: 1.8%"},
    {"id": "unemp_all", "en": "Unemployment rate", "ar": "معدل البطالة",
     "note_en": "All residents, Saudi and non-Saudi",
     "note_ar": "لجميع السكان، سعوديين وغير سعوديين",
     "category": "417515", "table": "1", "row": "unemployment rate", "kind": None,
     "col": "total", "unit": "%", "scale": 1, "verified": "GASTAT: 3.1%"},
    {"id": "unemp_saudi", "en": "Saudi unemployment", "ar": "بطالة السعوديين",
     "note_en": "Saudi nationals only",
     "note_ar": "السعوديون فقط",
     "category": "417515", "table": "1", "row": "unemployment rate", "kind": None,
     "col": "saudi total", "unit": "%", "scale": 1, "verified": "GASTAT: 6.4%"},
    {"id": "participation", "en": "Saudi participation", "ar": "مشاركة السعوديين",
     "note_en": "Labour force participation, Saudi nationals",
     "note_ar": "معدل المشاركة في القوى العاملة، السعوديون",
     "category": "417515", "table": "1", "row": "labour force participation rate",
     "kind": None, "col": "saudi total", "unit": "%", "scale": 1, "verified": "GASTAT: 49.0%"},
    {"id": "ipi", "en": "Industrial production", "ar": "الإنتاج الصناعي",
     "note_en": "Industrial production index, annual change",
     "note_ar": "الرقم القياسي للإنتاج الصناعي، التغير السنوي",
     "category": "123454", "table": None, "row": "general index", "kind": "change",
     "col": "rate of change annual", "unit": "%", "scale": 100, "verified": "GASTAT: -8.1%"},
]


def resolve_headlines(con) -> list[dict]:
    """Resolve each headline spec to one number, or drop it.

    The safety property is not "one series" but "one value": the CPI change
    column carries the month in its own name ("Percent Change in August 2026
    from 2025-08-01"), so the series changes every release while still being the
    same measure. What matters is that at the latest period the spec matches
    exactly one distinct value -- otherwise the front page would be picking.
    """
    out = []
    for h in HEADLINES:
        where = ["category_id = ?", "lower(row_en) = ?"]
        args: list = [h["category"], h["row"]]
        if h.get("table"):
            where.append("table_name = ?")
            args.append(h["table"])
        if h.get("kind"):
            where.append("kind = ?")
            args.append(h["kind"])
        if h.get("col_like"):
            where.append("lower(col_en) LIKE ?")
            args.append(h["col_like"])
        elif h.get("col") is not None:
            where.append("lower(col_en) = ?")
            args.append(h["col"])
        rows = con.execute(f"""
            SELECT period, value, source_file
            FROM v_observation WHERE {' AND '.join(where)}
            ORDER BY period DESC
        """, args).fetchall()
        if not rows:
            log.warning("headline %-14s no match - dropped", h["id"])
            continue
        latest = rows[0][0]
        at_latest = [r for r in rows if r[0] == latest]
        values = {round(r[1], 6) for r in at_latest}
        if len(values) > 1:
            log.warning("headline %-14s %s gives %d different values - dropped",
                        h["id"], latest, len(values))
            continue
        period, value, src = at_latest[0]
        out.append({k: h[k] for k in ("id", "en", "ar", "note_en", "note_ar", "unit", "verified")}
                   | {"category": h["category"], "period": period,
                      "value": round(value * h["scale"], 2), "source": src})
        log.info("headline %-14s %-9s %8.2f%s  (%s)", h["id"], period,
                 value * h["scale"], h["unit"], h["verified"])
    return out


def arabic_names() -> dict:
    """Arabic product and sub-domain names from the Arabic taxonomy crawl.

    16 of the 20 products have one; the four archived series are absent from the
    live Arabic taxonomy, so those keep their English name rather than a guess.
    """
    path = ROOT / "data" / "catalog" / "taxonomy.json"
    if not path.exists():
        return {}
    tax = json.loads(path.read_text("utf-8"))
    return {p["category_id"]: p for p in tax.get("ar", [])}


# The regional view. Each indicator names one exact table whose shape has been
# read by eye and whose figures are asserted in check_against_source.py, rather
# than being found by searching labels for a region's name: a search turns up
# airports, dry ports and establishment counts that are located in a region but
# do not measure it.
#
# "row" means the region sits in the row label and `col` picks the column;
# "row_suffix" means the region and the measure share the row label, as Real
# Estate writes "Riyadh Index" and "Riyadh YoY".
# The region names a reader expects. build_geography's names come from the
# TopoJSON boundary file, which transliterates ("Ar Riyad", "Ash Sharqiyah",
# "`Asir"); those are right for joining geometry and wrong on a button.
REGION_NAMES = {
    "SA.RI": ("Riyadh", "الرياض"),
    "SA.MK": ("Makkah", "مكة المكرمة"),
    "SA.MD": ("Madinah", "المدينة المنورة"),
    "SA.QS": ("Al Qassim", "القصيم"),
    "SA.SH": ("Eastern Province", "المنطقة الشرقية"),
    "SA.AS": ("Asir", "عسير"),
    "SA.TB": ("Tabuk", "تبوك"),
    "SA.HA": ("Hail", "حائل"),
    "SA.HS": ("Northern Borders", "الحدود الشمالية"),
    "SA.JZ": ("Jazan", "جازان"),
    "SA.NJ": ("Najran", "نجران"),
    "SA.BA": ("Al Bahah", "الباحة"),
    "SA.JF": ("Al Jouf", "الجوف"),
}

# Which regions lead the picker. Riyadh first because it is the one most
# readers open the page for; the rest follow by population weight, then the
# remainder alphabetically.
REGION_ORDER = ["SA.RI", "SA.MK", "SA.SH", "SA.MD", "SA.QS", "SA.AS"]

REGION_INDICATORS = [
    {"id": "cpi", "en": "Consumer prices", "ar": "الأرقام القياسية لأسعار المستهلك",
     "note_en": "Index, 2023 = 100", "note_ar": "الرقم القياسي، 2023 = 100",
     "category": "121421", "table": "4.1", "mode": "row", "col": "", "unit": "", "dp": 2},
    {"id": "inflation", "en": "Inflation", "ar": "التضخم",
     "note_en": "Consumer prices, annual change", "note_ar": "أسعار المستهلك، التغير السنوي",
     "category": "121421", "table": "4.2", "mode": "row", "col": "", "unit": "%", "dp": 2,
     "signed": True},
    {"id": "repi", "en": "Real estate prices", "ar": "أسعار العقارات",
     "note_en": "Index, 2023 = 100", "note_ar": "الرقم القياسي، 2023 = 100",
     "category": "121920", "table": "3", "mode": "row_suffix", "suffix": "Index",
     "unit": "", "dp": 2},
    {"id": "repi_yoy", "en": "Real estate, annual change", "ar": "العقارات، التغير السنوي",
     "note_en": "Price index, year on year", "note_ar": "الرقم القياسي، سنويًا",
     "category": "121920", "table": "3", "mode": "row_suffix", "suffix": "YoY",
     "unit": "%", "dp": 1, "signed": True},
    {"id": "unemp", "en": "Unemployment rate", "ar": "معدل البطالة",
     "note_en": "All residents, Saudi and non-Saudi", "note_ar": "لجميع السكان",
     "category": "417515", "table": "2-4", "mode": "row", "col": "total", "unit": "%", "dp": 2},
    {"id": "unemp_saudi", "en": "Saudi unemployment", "ar": "بطالة السعوديين",
     "note_en": "Saudi nationals only", "note_ar": "السعوديون فقط",
     "category": "417515", "table": "2-4", "mode": "row", "col": "saudi total",
     "unit": "%", "dp": 2},
    # Counts rather than rates. Each note says exactly what the source counts:
    # the housing table is Saudi households only, not every dwelling, and the
    # workers table counts people on the job rather than the labour force.
    {"id": "workers", "en": "Workers on the job", "ar": "المشتغلون",
     "note_en": "Register-based, Saudi and non-Saudi",
     "note_ar": "من السجلات الإدارية، سعوديون وغير سعوديين",
     "category": "124074", "table": "3-4", "mode": "row", "col": "total",
     "unit": "", "dp": 0},
    {"id": "housing", "en": "Housing units", "ar": "المساكن",
     "note_en": "Occupied by Saudi households",
     "note_ar": "المشغولة بأسر سعودية",
     "category": "3175009", "table": "1", "mode": "row", "col": "total",
     "unit": "", "dp": 0},
    {"id": "power", "en": "Residential electricity", "ar": "الكهرباء السكنية",
     "note_en": "Energy sold, gigawatt-hours",
     "note_ar": "الطاقة المباعة، جيجاوات ساعة",
     "category": "124767", "table": "7", "mode": "row", "col": "residential",
     "unit": " GWh", "dp": 0},
]


def build_regions(con) -> list[dict]:
    """One card set per administrative region, plus its own history to chart."""
    from build_geography import REGIONS, resolve

    out = {code: {"code": code,
                  "en": REGION_NAMES.get(code, (en, ar))[0],
                  "ar": REGION_NAMES.get(code, (en, ar))[1],
                  "ind": {}, "series": {}}
           for code, (en, ar) in REGIONS.items()}

    for spec in REGION_INDICATORS:
        where = ["category_id = ?", "trim(table_name) = ?"]
        args: list = [spec["category"], spec["table"]]
        if spec["mode"] == "row":
            where.append("lower(trim(col_en)) = ?")
            args.append(spec["col"])
        rows = con.execute(f"""
            SELECT trim(row_en) AS label, period, value
            FROM v_observation
            WHERE {' AND '.join(where)} AND value IS NOT NULL
            ORDER BY period
        """, args).fetchall()

        by_region: dict[str, list] = {}
        for label, period, value in rows:
            name = tidy(label)
            if spec["mode"] == "row_suffix":
                if not name.endswith(" " + spec["suffix"]):
                    continue
                name = name[: -(len(spec["suffix"]) + 1)]
            hit = resolve(name)
            if not hit:
                continue
            by_region.setdefault(hit[0], []).append((period, value))

        for code, pts in by_region.items():
            if code not in out:
                continue
            pts.sort()
            # One reading per period: the same region can appear twice in a
            # sheet, and a card must not silently show whichever came last.
            dedup = {p: v for p, v in pts}
            last = max(dedup)
            out[code]["ind"][spec["id"]] = {
                "en": spec["en"], "ar": spec["ar"],
                "note_en": spec["note_en"], "note_ar": spec["note_ar"],
                "value": round(dedup[last], spec["dp"]), "unit": spec["unit"],
                # a rate is not a change: only a change gets a leading sign
                "signed": bool(spec.get("signed")),
                "period": last, "table": spec["table"],
            }
            if len(dedup) >= MIN_POINTS:
                out[code]["series"][spec["id"]] = [
                    [p, round(v, spec["dp"])] for p, v in sorted(dedup.items())][-60:]

    regions = [r for r in out.values() if r["ind"]]
    rank = {c: i for i, c in enumerate(REGION_ORDER)}
    regions.sort(key=lambda r: (rank.get(r["code"], len(rank)), r["en"]))
    log.info("regions: %d with indicators, %d indicator specs",
             len(regions), len(REGION_INDICATORS))
    for spec in REGION_INDICATORS:
        have = sum(1 for r in regions if spec["id"] in r["ind"])
        if have < len(regions):
            log.warning("  %-12s resolved for only %d of %d regions",
                        spec["id"], have, len(regions))
    return regions


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    ar = arabic_names()

    products = con.execute("""
        SELECT category_id, product, domain, subdomain, n_releases,
               n_series, n_observations, first_period, last_period
        FROM v_product_coverage ORDER BY product
    """).fetchall()

    app = {"products": [], "domains": {}, "headlines": resolve_headlines(con),
           "generated": con.execute(
        "SELECT max(last_period) FROM v_product_coverage").fetchone()[0]}

    for cat, name, domain, sub, n_rel, n_ser, n_obs, first, last in products:
        # Candidate series. v_chartable is the warehouse's own definition of
        # what may be drawn; the checks read the same view, so a check that the
        # page shows the most recent data available cannot be satisfied by the
        # two disagreeing about what is available.
        cand = con.execute("""
            SELECT series_key, row_en, row_ar, col_en, col_ar, kind, index_base,
                   n_points, first_period, last_period, period_type, table_name
            FROM v_chartable
            WHERE category_id = ? AND n_points >= ?
            ORDER BY n_points DESC, row_en
        """, [cat, MIN_POINTS]).fetchall()

        chosen, seen_rows = [], set()

        # The most recent year any chartable series of this product reaches. A
        # series a year or more behind it has been discontinued or superseded,
        # whatever its label says.
        current_year = max((int(r[9][:4]) for r in cand if r[9][:4].isdigit()), default=0)

        def score(r):
            """Rank candidates the way a reader would expect to meet them.

            Recency comes first. Consumer Prices carries the same "General
            Index / Index Numbers" label on a 2018-based run of 56 points that
            GASTAT stopped publishing in 2024 and on the live 2023-based run of
            25. On length the dead one wins, and the page charted a headline
            that stopped two years ago while the current figures sat unused.

            After that, a national headline beats the same headline broken out
            by city, even though the city series run longer: "General Index" for
            the country is the number people come for, and it loses a plain
            length contest to sixteen city series carrying the same row label.
            """
            row_label, col_label, kind = r[1] or "", r[3] or "", r[5]
            headline = bool(HEADLINE.search(f"{row_label} {col_label}"))
            aggregate = not col_label.strip()
            # A bare "value" on a headline row is usually the artefact of a code
            # or level column, not a measure; real headlines are index or change.
            measured = kind in ("index", "change")
            if headline and aggregate and measured:
                tier = 0
            elif headline and aggregate:
                tier = 1
            elif headline:
                tier = 2
            else:
                tier = 3
            # A headline label on a three-point stub is not a headline. Industrial
            # Production opened on one of those while twenty-two months of the
            # same index sat unused behind it.
            if r[7] < THIN_POINTS:
                tier += 1
            # How far behind the front this series ends comes FIRST, ahead of
            # what its label promises. A headline that stopped in 2018 is not a
            # headline any more, and letting the label win put a series eight
            # years stale at the top of Consumer Prices.
            year = int(r[9][:4]) if r[9][:4].isdigit() else 0
            behind = max(0, current_year - year)
            return (behind, tier, -r[7])
        def labels_of(r):
            """The pair a reader would see, or None when there is nothing to show.

            Some tables label rows in Arabic only. Requiring an English label
            threw those series away entirely -- for two products, all of them.
            The Arabic label is the label; the site falls back to it.
            """
            row_label = tidy(r[1]) or tidy(r[2])
            col_label = tidy(r[3]) or tidy(r[4])
            if NOISE.match(row_label) or NOISE.match(col_label):
                return None
            if not row_label and not col_label:
                return None
            return row_label, col_label

        ranked = sorted(cand, key=score)

        # One line per identity, where the base is part of the identity: a
        # rebased index keeps its label, so matching on the label alone let one
        # base hide the other and threw away either the history or the current
        # figures. Both are real, and the chart draws them as two lines that
        # are never joined.
        #
        # Within an identity take the longest run, and only then rank the
        # identities against each other by recency. Choosing the whole pool on
        # recency first would drop a discontinued base's full history in favour
        # of a shorter, slightly fresher copy of the same dead series.
        groups: dict[tuple, tuple] = {}
        for r in ranked:
            pair = labels_of(r)
            if pair is None:
                continue
            ident = (pair[0].lower(), pair[1].lower(), r[6] or "")
            best = groups.get(ident)
            if best is None or r[7] > best[7]:
                groups[ident] = r
        seen_rows.update(groups)
        chosen = sorted(groups.values(), key=score)[:MAX_SERIES_PER_PRODUCT]

        def build(r):
            """A candidate as the site would draw it, or None if it cannot be."""
            pts = con.execute("""
                SELECT period, value FROM fact_observation
                WHERE series_key = ? ORDER BY period
            """, [r[0]]).fetchall()
            if len(pts) < MIN_POINTS:
                return None
            # A line that never moves carries no information and, when it is a
            # run of zeros from a code column, is not a measurement at all.
            vals = [v for _, v in pts]
            if max(vals) == min(vals):
                return None
            return {
                "id": r[0][:12],
                "row": tidy(r[1]), "row_ar": " ".join((r[2] or "").split()),
                "col": tidy(r[3]), "col_ar": " ".join((r[4] or "").split()),
                "kind": r[5], "base": r[6] or None, "freq": r[10],
                # The workbook table the series was read from, so the site can
                # tell a reader exactly which sheet to open to check it.
                "table": r[11] or "",
                "pts": [[p, round(v, 3)] for p, v in pts],
            }

        drawn: list[tuple] = []          # (candidate, payload)
        for r in chosen:
            row = build(r)
            if row:
                drawn.append((r, row))

        # Two series earn a slot whatever their rank: the one reaching furthest
        # forward, so the page shows where the product stands now, and the one
        # reaching furthest back, so a rebase does not cost the reader the whole
        # history behind it. Consumer Prices needs both -- its live 2023-based
        # run is 25 points and the 2018-based run it replaced is 56, and the
        # regional breakdowns would otherwise crowd out whichever came second.
        #
        # This runs over what SURVIVED the filters above, not over the picks
        # made before them: a guaranteed slot that is then dropped for being
        # constant or too short is not a guarantee, which is how two products
        # still charted nothing current.
        # It searches every candidate, not just the one representative kept per
        # identity: the newest run of a measure is often not its longest, so
        # picking representatives by length hides it from the guarantee that
        # exists to find it.
        def guarantee(order, already):
            if not ranked or already():
                return
            have = {c[0] for c, _ in drawn}
            for r in sorted(ranked, key=order):
                if r[0] in have:
                    continue
                row = build(r)
                if row:
                    if len(drawn) >= MAX_SERIES_PER_PRODUCT:
                        drawn.pop()
                    drawn.append((r, row))
                    return

        def yr(p):
            return int(p[:4]) if p[:4].isdigit() else 0

        front = max((yr(r[9]) for r in ranked), default=0)
        longest = max((r[7] for r in ranked), default=0)
        guarantee(lambda r: (-yr(r[9]), score(r)),
                  lambda: any(yr(c[9]) >= front for c, _ in drawn))
        guarantee(lambda r: -r[7],
                  lambda: any(c[7] >= longest for c, _ in drawn))

        series = [row for _, row in drawn]

        # When a product's charted series sit on more than one base, the base is
        # part of what tells them apart: a rebased index keeps its label, so two
        # lines would otherwise carry the same name with no way to tell the
        # current run from the one it replaced.
        multi_base = len({s["base"] or "" for s in series}) > 1
        for s in series:
            s["blab"] = f"{s['base']}=100" if (multi_base and s["base"]) else ""

        breakdown = con.execute("""
            WITH latest AS (SELECT max(period) AS p FROM v_observation WHERE category_id = ?)
            SELECT row_en, row_ar, col_en, value, weight_pct
            FROM v_observation, latest
            WHERE category_id = ? AND period = latest.p AND kind <> 'weight'
              AND row_en <> '' AND value IS NOT NULL
            ORDER BY abs(value) DESC LIMIT ?
        """, [cat, cat, MAX_BREAKDOWN]).fetchall()

        releases = con.execute("""
            SELECT DISTINCT r.pub_id, r.title, r.source_file, r.max_period, r.source_url
            FROM dim_release r WHERE r.category_id = ?
            ORDER BY r.max_period DESC LIMIT 12
        """, [cat]).fetchall()

        bases = [b[0] for b in con.execute("""
            SELECT DISTINCT index_base FROM dim_series
            WHERE category_id = ? AND index_base <> '' ORDER BY index_base
        """, [cat]).fetchall()]

        a = ar.get(cat, {})
        app["products"].append({
            "id": cat, "name": name, "domain": domain, "subdomain": sub,
            "name_ar": a.get("product", ""), "subdomain_ar": a.get("subdomain", ""),
            "domain_ar": a.get("domain", ""),
            "releases": n_rel, "n_series": n_ser, "n_obs": n_obs,
            "first": first, "last": last, "bases": bases,
            "series": series,
            "breakdown": [{"row": tidy(b[0]), "row_ar": " ".join((b[1] or "").split()),
                           "col": tidy(b[2]), "value": round(b[3], 3),
                           "weight": round(b[4], 3) if b[4] is not None else None}
                          for b in breakdown],
            "recent": [{"pub": r[0], "title": r[1], "file": r[2], "period": r[3],
                        "url": r[4] or ""}
                       for r in releases],
        })
        log.info("  %-44s %2d series  %2d breakdown", name[:44], len(series), len(breakdown))

    for p in app["products"]:
        d = app["domains"].setdefault(p["domain"], {"products": 0, "obs": 0, "series": 0})
        d["products"] += 1
        d["obs"] += p["n_obs"]
        d["series"] += p["n_series"]

    suspects = con.execute("SELECT COUNT(*) FROM v_suspect_series").fetchone()[0]
    log.info("excluded %d mixed-measure series from charts", suspects)

    totals = con.execute("""
        SELECT (SELECT COUNT(*) FROM fact_observation),
               (SELECT COUNT(*) FROM dim_series),
               (SELECT COUNT(*) FROM dim_release),
               (SELECT COUNT(*) FROM fact_revision)
    """).fetchone()
    app["regions"] = build_regions(con)
    app["region_order"] = [s["id"] for s in REGION_INDICATORS]

    app["totals"] = {"observations": totals[0], "series": totals[1],
                     "releases": totals[2], "revisions": totals[3],
                     "products": len(app["products"]), "excluded": suspects}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(app, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("wrote %s (%.1f MB)", OUT, OUT.stat().st_size / 1048576)
    log.info("totals: %s", app["totals"])
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
