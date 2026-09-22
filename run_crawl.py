"""
Saudi 360 — phase 1 crawler.

    python run_crawl.py                # v1 scope (20 products), EN + AR
    python run_crawl.py --all          # every product in the taxonomy
    python run_crawl.py --no-files     # catalogue only, download nothing
    python run_crawl.py --lang en      # skip the Arabic pass

Safe to interrupt: it checkpoints after each product and resumes where it
stopped. Re-running later downloads only what changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from crawler.gastat import (  # noqa: E402
    Checkpoint,
    Fetcher,
    LISTING_CAP,
    crawl_listing,
    crawl_taxonomy,
    download,
)

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STORE = DATA / "files"
CATALOG = DATA / "catalog"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("run")


def safe_name(url: str) -> str:
    """Filename from a Liferay document URL, without the uuid and cache-buster."""
    stem = url.split("?")[0].split("/")[-2]
    stem = re.sub(r"_fixed_\d+$", "", stem.replace("+", " "))
    from urllib.parse import unquote

    return re.sub(r'[<>:"/\\|?*]', "_", unquote(stem))[:150]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every product, not just v1 scope")
    ap.add_argument("--no-files", action="store_true", help="catalogue only")
    ap.add_argument("--lang", default="en,ar", help="comma-separated: en,ar")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--reset", action="store_true", help="ignore the checkpoint")
    ap.add_argument("--scope", default="scope.json", help="scope file to use")
    ap.add_argument(
        "--max-pdf-mb", type=int, default=20,
        help="skip PDFs larger than this (0 = no limit). Nothing parses PDFs, so "
             "oversized ones are recorded in oversized.csv and fetched on demand.",
    )
    args = ap.parse_args()

    langs = [l.strip() for l in args.lang.split(",") if l.strip()]
    ckpt_path = DATA / "checkpoint.json"
    if args.reset and ckpt_path.exists():
        ckpt_path.unlink()
    ckpt = Checkpoint(ckpt_path)
    fetch = Fetcher(delay=args.delay)

    try:
        # -- pass 1: taxonomy -------------------------------------------------
        log.info("pass 1/3  taxonomy")
        taxonomy: dict[str, list] = {}
        for lang in langs:
            products = crawl_taxonomy(fetch, lang)
            taxonomy[lang] = [asdict(p) for p in products]
            log.info("  %s: %d products", lang, len(products))

        CATALOG.mkdir(parents=True, exist_ok=True)
        (CATALOG / "taxonomy.json").write_text(
            json.dumps(taxonomy, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        primary = langs[0]
        by_id = {p["category_id"]: p for p in taxonomy[primary]}

        if args.all:
            targets = list(by_id.keys())
        else:
            scope = json.loads((ROOT / args.scope).read_text("utf-8"))
            targets = [p["category_id"] for p in scope["products"]]
            # archived categories are not in the live taxonomy
            for p in scope["products"]:
                by_id.setdefault(p["category_id"], {
                    "category_id": p["category_id"], "product": p["product"],
                    "domain": p["domain"], "subdomain": p["subdomain"],
                })
        log.info("  targeting %d products", len(targets))

        # -- pass 2: listings -------------------------------------------------
        log.info("pass 2/3  release listings")
        catalogue: list[dict] = []
        shortfalls: list[tuple[str, int, int]] = []

        for i, cat_id in enumerate(targets, 1):
            meta = by_id[cat_id]
            entry = {**meta, "langs": {}}
            for lang in langs:
                releases, claimed = crawl_listing(fetch, cat_id, lang)
                entry["langs"][lang] = {
                    "claimed_total": claimed,
                    "retrieved": len(releases),
                    "releases": [asdict(r) for r in releases],
                }
                if lang == primary:
                    if claimed and len(releases) >= LISTING_CAP and claimed > len(releases):
                        shortfalls.append((meta["product"], claimed, len(releases)))
                    log.info(
                        "  [%2d/%d] %-42s %3d releases%s",
                        i, len(targets), meta["product"][:42], len(releases),
                        f"  (listing caps {claimed - len(releases)} more)"
                        if claimed and claimed > len(releases) else "",
                    )
            catalogue.append(entry)

        (CATALOG / "catalogue.json").write_text(
            json.dumps(catalogue, ensure_ascii=False), encoding="utf-8"
        )
        write_manifest(catalogue, primary)

        if shortfalls:
            log.warning(
                "listing cap hid %d releases across %d products — see catalog/shortfall.csv",
                sum(c - r for _, c, r in shortfalls), len(shortfalls),
            )
            with (CATALOG / "shortfall.csv").open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["product", "claimed_total", "retrieved", "unreachable"])
                for prod, claimed, got in shortfalls:
                    w.writerow([prod, claimed, got, claimed - got])

        # -- pass 3: files ----------------------------------------------------
        if args.no_files:
            log.info("pass 3/3  skipped (--no-files)")
            return 0

        log.info("pass 3/3  downloading files")
        total_new = total_cached = total_failed = total_big = 0
        oversized: list[dict] = []
        max_bytes = args.max_pdf_mb * 1024 * 1024
        links: list[dict] = []

        for i, entry in enumerate(catalogue, 1):
            cat_id = entry["category_id"]
            new = cached = failed = big = 0

            for rel in entry["langs"][primary]["releases"]:
                seen_in_release: set[str] = set()
                for f in rel["files"]:
                    key = f["url"].split("?")[0]
                    if key in seen_in_release:
                        continue          # same file listed twice in one release
                    seen_in_release.add(key)

                    ext = Path(safe_name(f["url"])).suffix or f".{f['ext']}"
                    outcome, digest = download(
                        fetch, f["url"], STORE, ckpt.files, ext, max_bytes
                    )
                    new += outcome == "new"
                    cached += outcome == "cached"
                    failed += outcome == "failed"
                    if outcome == "oversized":
                        big += 1
                        links.append({
                            "category_id": cat_id, "pub_id": rel["pub_id"],
                            "filename": safe_name(f["url"]), "ext": f["ext"],
                            "sha256": "", "stored": "no", "path": "",
                            "url": f["url"],
                        })
                        oversized.append({
                            "category_id": cat_id, "pub_id": rel["pub_id"],
                            "filename": safe_name(f["url"]),
                            "mb": round(ckpt.files[key]["bytes"] / 1048576, 1),
                            "url": "https://www.stats.gov.sa" + f["url"],
                        })
                    if digest:
                        links.append({
                            "category_id": cat_id, "pub_id": rel["pub_id"],
                            "filename": safe_name(f["url"]), "ext": f["ext"],
                            "sha256": digest, "stored": "yes",
                            "path": f"{digest[:2]}/{digest}{ext}",
                            "url": f["url"],
                        })

            total_new += new
            total_cached += cached
            total_failed += failed
            total_big += big
            ckpt.finish(cat_id)
            log.info(
                "  [%2d/%d] %-42s %3d new  %3d cached%s",
                i, len(catalogue), entry["product"][:42], new, cached,
                (f"  {big} oversized" if big else "")
                + (f"  {failed} FAILED" if failed else ""),
            )

        write_links(links)
        if oversized:
            path = CATALOG / "oversized.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(
                    fh, fieldnames=["category_id", "pub_id", "filename", "mb", "url"]
                )
                w.writeheader()
                w.writerows(oversized)
            log.info(
                "  %d PDFs over %dMB skipped (%.1f GB) -> %s",
                len(oversized), args.max_pdf_mb,
                sum(o["mb"] for o in oversized) / 1024, path,
            )
        size = sum(f.stat().st_size for f in STORE.rglob("*") if f.is_file())
        log.info(
            "done — %d new, %d already held, %d failed · store %.1f GB across %d blobs",
            total_new, total_cached, total_failed,
            size / 1024**3, len({l["sha256"] for l in links if l["sha256"]}),
        )
        if total_failed:
            log.warning("%d files failed to download — re-run to retry them", total_failed)
        return 0

    except KeyboardInterrupt:
        log.warning("interrupted — checkpoint saved, re-run to resume")
        ckpt.save()
        return 130
    finally:
        ckpt.save()
        fetch.close()


def write_manifest(catalogue: list[dict], lang: str) -> None:
    path = CATALOG / "file_manifest.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([
            "domain", "subdomain", "product", "category_id",
            "pub_id", "release_title", "year", "periodicity", "ext", "url",
        ])
        for e in catalogue:
            for rel in e["langs"][lang]["releases"]:
                for f in rel["files"]:
                    w.writerow([
                        e["domain"], e["subdomain"], e["product"], e["category_id"],
                        rel["pub_id"], rel["title"], rel["year"], rel["periodicity"],
                        f["ext"], "https://www.stats.gov.sa" + f["url"],
                    ])
    log.info("  manifest -> %s", path)


def write_links(links: list[dict]) -> None:
    """release -> file. The join between the catalogue and the blob store.

    `path` is the file's location under data/files and is authoritative.
    `ext` is the site's own icon label and is NOT reliable: 118 legacy .xls files
    are labelled xlsx, and two PDFs are labelled word.

    Rows with stored="no" are oversized PDFs held only by URL; see oversized.csv.
    """
    path = CATALOG / "release_files.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "category_id", "pub_id", "filename", "ext", "sha256", "stored",
                "path", "url",
            ],
        )
        w.writeheader()
        w.writerows(links)
    log.info("  links    -> %s", path)


if __name__ == "__main__":
    raise SystemExit(main())
