"""
Saudi 360 — phase 2 parser, Consumer Prices vertical slice.

    python run_parse.py                 # parse CPI, write tidy output + rejects
    python run_parse.py --category 121421

Reads the workbooks the crawler stored, finds the expenditure-category table in
each release, and writes one observation per category per release. Anything it
cannot read honestly goes to rejects.csv with a reason rather than being guessed.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from parser.excel import (  # noqa: E402
    Rejected,
    index_base,
    load_grid,
    parse_sheet,
    sheet_names,
)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STORE = DATA / "files"
CATALOG = DATA / "catalog"
OUT = DATA / "parsed"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("parse")

MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)
}
# The category table is the third sheet in every CPI layout seen so far, but the
# sheet is located by scanning rather than trusted by position.
CANDIDATE_SHEETS = range(0, 6)


def reference_period(release: dict) -> tuple[int, int] | None:
    """The period a release reports.

    GASTAT's own month field is "-" on 55 of 95 monthly CPI releases, so the
    release title is used as a fallback -- it carries the month in every case
    seen. Annual releases get month 0.
    """
    year = release.get("year")
    if not (year and str(year).isdigit()):
        return None
    year = int(year)

    month = (release.get("month") or "").strip().lower()
    if month in MONTHS:
        return year, MONTHS[month]

    title = (release.get("title") or "").lower()
    for name, num in MONTHS.items():
        if re.search(rf"\b{name[:3]}[a-z]*\b", title):
            return year, num

    if (release.get("periodicity") or "").lower() in ("yearly", "annual"):
        return year, 0
    return None


def pick_sheet(path: Path, want=None):
    """The sheet holding the expenditure-category table, by trying each in turn."""
    names = sheet_names(path)
    best = None
    reasons: list[str] = []
    for idx in CANDIDATE_SHEETS:
        if idx >= len(names):
            break
        try:
            title, grid = load_grid(path, idx)
            recipe, obs = parse_sheet(grid, len(names), want)
            base = index_base(grid)
        except Rejected as exc:
            reasons.append(f"sheet {idx}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            reasons.append(f"sheet {idx}: {type(exc).__name__}: {exc}")
            continue
        # prefer the richest table: the category breakdown has many levels
        score = (len({o.level for o in obs}), len(obs))
        if best is None or score > best[0]:
            best = (score, idx, title, recipe, obs, base)
    return best, reasons


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="121421", help="GASTAT category id")
    ap.add_argument("--all", action="store_true", help="every product in scope.json")
    args = ap.parse_args()

    if args.all:
        scope = json.loads((ROOT / "scope.json").read_text("utf-8"))
        summary = []
        for p in scope["products"]:
            log.info("=" * 74)
            try:
                n_obs, n_ok, n_rej = parse_category(p["category_id"])
            except Exception as exc:  # noqa: BLE001
                log.error("  %s failed: %s", p["product"], exc)
                n_obs = n_ok = 0
                n_rej = -1
            summary.append((p["product"], n_obs, n_ok, n_rej))
        log.info("=" * 74)
        log.info("%-46s %8s %6s %6s", "product", "obs", "ok", "rej")
        for name, o, ok, rj in summary:
            log.info("%-46s %8d %6d %6d", name[:46], o, ok, rj)
        log.info("TOTAL observations: %d", sum(s[1] for s in summary))
        return 0

    parse_category(args.category)
    return 0


def parse_category(category_id: str) -> tuple[int, int, int]:
    catalogue = json.loads((CATALOG / "catalogue.json").read_text("utf-8"))
    entry = next(c for c in catalogue if c["category_id"] == category_id)
    releases = {r["pub_id"]: r for r in entry["langs"]["en"]["releases"]}

    links = [
        r
        for r in csv.DictReader((CATALOG / "release_files.csv").open(encoding="utf-8-sig"))
        if r["category_id"] == category_id
        and r["stored"] == "yes"
        and Path(r["path"]).suffix.lower() in (".xls", ".xlsx")
    ]
    by_file: dict[str, dict] = {}
    for r in links:
        by_file.setdefault(r["sha256"], r)

    log.info("%s — %d releases, %d distinct spreadsheets",
             entry["product"], len(releases), len(by_file))

    rows: list[dict] = []
    rejects: list[dict] = []
    recipes_used: Counter = Counter()

    for i, r in enumerate(by_file.values(), 1):
        path = STORE / r["path"]
        release = releases.get(r["pub_id"], {})
        period = reference_period(release)

        if period is None:
            rejects.append({
                "pub_id": r["pub_id"], "filename": r["filename"],
                "reason": "no reference period in release metadata",
            })
            continue

        picked, reasons = pick_sheet(path, period)
        if picked is None:
            rejects.append({
                "pub_id": r["pub_id"], "filename": r["filename"],
                "reason": "; ".join(reasons[:3]) or "no sheet matched any recipe",
            })
            continue

        _, sheet_idx, title, recipe, observations, base = picked
        recipes_used[recipe] += 1
        year, month = period
        for o in observations:
            rows.append({
                "category_id": category_id,
                "pub_id": r["pub_id"],
                "release_title": release.get("title", ""),
                "year": year,
                "month": month,
                "period": f"{year}-{month:02d}" if month else f"{year}",
                "period_type": "monthly" if month else "annual",
                "level": o.level,
                "label_en": o.label_en,
                "label_ar": o.label_ar,
                "weight_pct": o.weight,
                "value": o.value,
                "unit": "index",
                "index_base": base or "",
                "sheet": title,
                "recipe": recipe,
                "source_sha256": r["sha256"],
                "source_file": r["filename"],
            })
        if i % 20 == 0:
            log.info("  %d/%d files", i, len(by_file))

    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / f"observations_{category_id}.csv", rows)
    write_csv(OUT / f"rejects_{category_id}.csv", rejects)

    log.info("parsed %d observations from %d files (%d rejected)",
             len(rows), len(by_file) - len(rejects), len(rejects))
    log.info("recipes used: %s", dict(recipes_used))
    if rejects:
        log.warning("%d files rejected — see %s",
                    len(rejects), OUT / f"rejects_{category_id}.csv")
    validate(rows)
    return len(rows), len(by_file) - len(rejects), len(rejects)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("  -> %s (%d rows)", path, len(rows))


def validate(rows: list[dict]) -> None:
    """Checks that would catch a parser reading the wrong column."""
    if not rows:
        log.error("validation: nothing parsed")
        return

    log.info("--- validation ---")
    periods = sorted({r["period"] for r in rows})
    log.info("periods covered      : %d, %s .. %s", len(periods), periods[0], periods[-1])

    # 1. the general index should exist in every release and sit near 100
    headline = [r for r in rows if r["level"] == 0]
    log.info("releases with a level-0 headline: %d", len({r["pub_id"] for r in headline}))
    odd = [r for r in headline if not 50 <= r["value"] <= 200]
    log.info("headline values outside 50-200   : %d", len(odd))

    # 2. weights should sum to about 100 within a release
    sums = defaultdict(float)
    for r in rows:
        if r["level"] == 1 and r["weight_pct"]:
            sums[r["pub_id"]] += float(r["weight_pct"])
    ok = sum(1 for v in sums.values() if 95 <= v <= 105)
    log.info("releases whose level-1 weights sum to ~100: %d/%d", ok, len(sums))

    # 3. index base: values on different bases are not comparable
    bases = Counter(r["index_base"] or "(not stated)" for r in rows)
    log.info("index bases present   : %s", dict(bases))
    if len([b for b in bases if b != "(not stated)"]) > 1:
        log.warning(
            "series spans %d index bases - year-on-year change across a rebase "
            "is meaningless until the series is linked", len(bases))

    # 4. cross-release agreement: the same period parsed from two releases
    #    should give the same headline value
    by_period = defaultdict(set)
    for r in headline:
        by_period[r["period"]].add(round(float(r["value"]), 2))
    clashes = {p: v for p, v in by_period.items() if len(v) > 1}
    log.info("periods with conflicting headline values: %d", len(clashes))
    for p, v in list(clashes.items())[:5]:
        log.warning("   %s -> %s", p, sorted(v))


if __name__ == "__main__":
    raise SystemExit(main())
