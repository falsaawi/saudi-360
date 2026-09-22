"""
Saudi 360 — phase 3. Parse every product in scope into one flat fact table.

    python run_parse_all.py                # all 20 products
    python run_parse_all.py --category 121421

Each workbook's sheets are read with the general matrix / row-series readers.
A sheet that matches neither is recorded in the reject log with its reason; it
is never guessed at.

Output: data/parsed/facts.csv  — one row per (table, row label, column, period).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from parser.excel import Rejected, index_base, load_grid, sheet_names  # noqa: E402
from parser.general import read_table  # noqa: E402
from run_parse import MONTHS  # noqa: E402

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STORE = DATA / "files"
CATALOG = DATA / "catalog"
OUT = DATA / "parsed"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("parse-all")

MAX_SHEETS = 14          # workbooks run to 58 sheets; the useful tables are early
MAX_ROWS_PER_SHEET = 4000


def release_period(release: dict) -> tuple[str, str] | None:
    year = release.get("year")
    if not (year and str(year).isdigit()):
        return None
    year = int(year)
    title = (release.get("title") or "").lower()

    month = (release.get("month") or "").strip().lower()
    if month in MONTHS:
        return f"{year}-{MONTHS[month]:02d}", "monthly"
    for name, num in MONTHS.items():
        if re.search(r"\b" + name[:3] + r"[a-z]*\b", title):
            return f"{year}-{num:02d}", "monthly"
    q = re.search(r"\bq([1-4])\b", title)
    if q:
        return f"{year}-Q{q.group(1)}", "quarterly"
    return str(year), "annual"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", help="one GASTAT category id (default: all in scope)")
    ap.add_argument("--max-sheets", type=int, default=MAX_SHEETS)
    ap.add_argument("--scope", default="scope.json", help="scope file to use")
    args = ap.parse_args()

    scope = json.loads((ROOT / args.scope).read_text("utf-8"))["products"]
    if args.category:
        scope = [p for p in scope if p["category_id"] == args.category]

    catalogue = {c["category_id"]: c for c in
                 json.loads((CATALOG / "catalogue.json").read_text("utf-8"))}
    links: dict[str, list[dict]] = {}
    for r in csv.DictReader((CATALOG / "release_files.csv").open(encoding="utf-8-sig")):
        if r["stored"] == "yes" and Path(r["path"]).suffix.lower() in (".xls", ".xlsx"):
            links.setdefault(r["category_id"], []).append(r)

    facts: list[dict] = []
    rejects: list[dict] = []
    summary: list[tuple] = []

    for p in scope:
        cat = p["category_id"]
        entry = catalogue.get(cat, {})
        releases = {r["pub_id"]: r for r in entry.get("langs", {}).get("en", {}).get("releases", [])}
        seen: dict[str, dict] = {}
        for r in links.get(cat, []):
            seen.setdefault(r["sha256"], r)

        n0 = len(facts)
        ok = 0
        recipes: Counter = Counter()

        for r in seen.values():
            path = STORE / r["path"]
            rel = releases.get(r["pub_id"], {})
            fallback = release_period(rel)
            try:
                names = sheet_names(path)
            except Exception as exc:  # noqa: BLE001
                rejects.append({"category_id": cat, "product": p["product"],
                                "filename": r["filename"], "sheet": "",
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue

            got_any = False
            for idx, sheet in enumerate(names[: args.max_sheets]):
                try:
                    title, grid = load_grid(path, idx)
                    recipe, records = read_table(grid, fallback, sheet)
                except Rejected as exc:
                    rejects.append({"category_id": cat, "product": p["product"],
                                    "filename": r["filename"], "sheet": sheet,
                                    "reason": str(exc)})
                    continue
                except Exception as exc:  # noqa: BLE001
                    rejects.append({"category_id": cat, "product": p["product"],
                                    "filename": r["filename"], "sheet": sheet,
                                    "reason": f"{type(exc).__name__}: {exc}"})
                    continue

                if len(records) > MAX_ROWS_PER_SHEET:
                    records = records[:MAX_ROWS_PER_SHEET]
                base = index_base(grid)
                recipes[recipe] += 1
                got_any = True
                for rec in records:
                    facts.append({
                        "category_id": cat,
                        "product": p["product"],
                        "domain": p["domain"],
                        "subdomain": p["subdomain"],
                        "pub_id": r["pub_id"],
                        "release_title": rel.get("title", ""),
                        "table": title,
                        "recipe": recipe,
                        "row_en": rec.row_en, "row_ar": rec.row_ar,
                        "col_en": rec.col_en, "col_ar": rec.col_ar,
                        "kind": rec.kind,
                        "period": rec.period or "",
                        "period_type": rec.period_type or "",
                        "value": rec.value,
                        "weight_pct": rec.weight if rec.weight is not None else "",
                        "index_base": base or "",
                        "source_sha256": r["sha256"],
                        "source_file": r["filename"],
                    })
            ok += got_any

        added = len(facts) - n0
        summary.append((p["product"], added, ok, len(seen), dict(recipes)))
        log.info("%-44s %8d facts  %3d/%-3d files  %s",
                 p["product"][:44], added, ok, len(seen), dict(recipes))

    OUT.mkdir(parents=True, exist_ok=True)
    write(OUT / "facts.csv", facts)
    write(OUT / "rejects_all.csv", rejects)

    log.info("=" * 78)
    log.info("TOTAL %d facts across %d products; %d sheet rejections",
             len(facts), len(summary), len(rejects))
    covered = sum(1 for s in summary if s[1] > 0)
    log.info("products with data: %d/%d", covered, len(summary))
    for name, n, ok, tot, _ in summary:
        if n == 0:
            log.warning("  no data: %s (%d files)", name, tot)
    return 0


def write(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    log.info("  -> %s (%d rows, %.1f MB)", path, len(rows), path.stat().st_size / 1048576)


if __name__ == "__main__":
    raise SystemExit(main())
