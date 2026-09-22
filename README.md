# Saudi 360

A centralized, queryable repository of Saudi official statistics from
[GASTAT](https://www.stats.gov.sa), and an interactive site on top of it.

Full plan: [Saudi 360 — GASTAT Data Platform Plan](https://claude.ai/code/artifact/e3d9ddf3-7c7d-4258-8b0d-9be500efb58c)

## Status

| Phase | State |
| --- | --- |
| 0 · Inventory | Done — 105 products, 1,102 releases, 2,015 files catalogued |
| 1 · Crawler | Done — 917 releases, 1,974 files, 1.3 GB, 0 failures |
| 2 · Parser engine | Done — matrix + row-series readers, bilingual labels |
| 3 · Recipe coverage | Done — 94 of 99 products yield data |
| 4 · Warehouse (DuckDB) | Done — 1,425,130 observations, 796,357 series |
| 5 · Website | Done — 94 products, grouped as GASTAT publishes them, bilingual |
| 6 · 360 layer | Done — map (312 measures, 47 products), Vision 2030 lens (94 products) |

## Running the crawler

One-time setup:

```bash
pip install httpx openpyxl
```

Then:

```bash
python run_crawl.py
```

That crawls the v1 scope — the 20 products with real time series. Add
`--scope scope_full.json` for all 99 products (917 releases, 1.3 GB).
First run takes about 70 minutes at the default one request per second and lands
1.0 GB. A later run with everything cached takes 2 minutes.

Useful variations:

```bash
python run_crawl.py --no-files     # catalogue only, download nothing
python run_crawl.py --all          # all 105 products, not just v1 scope
python run_crawl.py --lang en      # skip the Arabic pass
python run_crawl.py --delay 2      # slower, if the site starts rate-limiting
python run_crawl.py --max-pdf-mb 0 # also fetch the huge PDFs (+4.0 GB)
python run_crawl.py --scope scope_full.json   # all 99 products, not just the v1 20
```

It is safe to interrupt with Ctrl-C. State is checkpointed after every product,
and re-running resumes where it stopped. A later run re-downloads only files
that actually changed, so the monthly refresh is quick.

## Parsing and building

```bash
python run_parse_all.py --scope scope_full.json   # all 99 products -> data/parsed/facts.csv
python build_warehouse_all.py# facts.csv -> data/saudi360.duckdb (dedupes restatements)
python export_site.py        # warehouse -> site/app.json
python build_geography.py    # gazetteer -> dim_geography + v_regional + v_map_metric
python build_map.py          # published TopoJSON -> site/geo/sau.geo.json
python export_geo.py         # warehouse -> site/geo.json (map + Vision 2030 lens)
```

`run_parse.py` / `build_warehouse.py` are the earlier CPI-only vertical slice, kept
because its targeted recipe reads the COICOP hierarchy (the `level` column) that the
general reader does not model.

Query it with anything that speaks DuckDB:

```bash
duckdb data/saudi360.duckdb "SELECT period, value FROM v_observation WHERE level=0 ORDER BY period"
```

Views worth knowing:

| View | What it answers |
| --- | --- |
| `v_observation` | Facts joined to every dimension, with the source file |
| `v_product_coverage` | Per product: series, observations, span, releases |
| `v_series_span` | Each series, its index base and how many points it has |
| `v_suspect_series` | Series that mix measures or units — kept off the charts |
| `v_regional` | Observations placed on one of the 13 administrative regions |
| `v_map_metric` | Measures covering at least 8 regions in a single period |

`fact_revision` holds the superseded readings: every time a later release restated
a period, the earlier reading is kept rather than discarded.

The front page's five headline figures are pinned to one exact series each in
`export_site.py` and checked against the number GASTAT states in prose in its own
release. A spec that resolves to more than one value at its latest period is
dropped rather than guessed — the guard has fired twice and caught real drift.

**Two exclusion views guard the charts.** `v_ambiguous_series` records series where
one (series, period) held more than one distinct reading — the source does not pin
the value down, so the site declines to draw it. `v_suspect_series` records series
that mix measures or units. Both stay queryable; neither reaches a chart.

## The site

Visual language follows [datasaudi.sa](https://datasaudi.sa/en) — the palette, type
and controls were read off that site rather than guessed: ground `#1e1c1c`, accent
`#009dad`, slate panels, condensed uppercase headings, split toggle bars.
Navigation follows GASTAT's own three-level grouping (domain → sub-domain →
product), so the site is browsable the way the source organisation publishes.

`site/index.html` reads `site/app.json`. A plain static page — no build step, no
backend — covering all 20 products: overview, per-product charts, cross-product
comparison (re-based to 100), searchable catalogue, English/Arabic with RTL, light
and dark. Preview it with:

```bash
python -m http.server 8777 --directory site
```

## Layout

```
saudi360/
  scope.json              the 20 v1 products, with category ids
  scope_full.json         every product that publishes releases (99)
  run_crawl.py            entry point
  crawler/gastat.py       fetching, parsing, downloading, checkpointing
  data/
    catalog/
      taxonomy.json       full domain -> subdomain -> product tree (en + ar)
      catalogue.json      every release of every in-scope product
      file_manifest.csv   one row per downloadable file link
      release_files.csv   release -> stored file (joins catalogue to the store)
      shortfall.csv       releases the portal's listing cap hides
      oversized.csv       PDFs over 20MB, held by URL rather than stored
    files/<aa>/<sha256>   content-addressed store: each distinct file held once
    checkpoint.json       resume state, per-URL hashes and validators
    parsed/               observations_*.csv, rejects_*.csv
    saudi360.duckdb       the warehouse
  parser/excel.py         fingerprinting, recipes, bilingual + rebase handling
  run_parse.py            workbooks -> tidy observations
  build_warehouse.py      observations -> star schema
  parser/general.py       general matrix + row-series readers
  run_parse_all.py        all products -> facts.csv
  build_warehouse_all.py  facts -> star schema + revision history
  export_site.py          warehouse -> site/app.json
  build_geography.py      city/region gazetteer -> geography dimension
  build_map.py            TopoJSON -> GeoJSON for the map
  export_geo.py           warehouse -> site/geo.json
  site/                   index.html + app.json + geo.json + geo/sau.geo.json
  recon/                  phase 0 reconnaissance scripts and output
```

## Things worth knowing about the source

- Listings are **server-rendered**, so no browser automation is needed.
- The listing **caps at 100 releases per product** even when its own counter
  claims more. 265 releases are unreachable this way — see `shortfall.csv`.
  Recovering them needs the Archived Data section, site search, or asking GASTAT.
- `start` in the listing URL is a **1-based page number**, not an offset.
  `delta` accepts 20, 40 or 60 only.
- The site returns **HTTP 429 readily**. Do not lower `--delay` below 1 second.
- `robots.txt` disallows `/api/`, `/c/portal/` and `/o/`. The crawler touches none
  of them.
- **Half of all file links are duplicates.** Annual compendiums re-link the same
  monthly workbooks, so 3,054 links across the v1 scope resolve to 1,547 distinct
  files. The store is content-addressed by SHA-256, which halves the corpus on
  disk and means each workbook is parsed exactly once in phase 2.
- **42 PDFs are over 20MB and total 3.6 GB** — one atlas runs to 259 MB, while every
  Excel file in the v1 scope comes to 271 MB. Nothing parses PDFs, and provenance
  needs only the URL, so oversized ones are recorded in `oversized.csv` and skipped.
  `--max-pdf-mb 0` fetches them if you ever want local copies.
- **Verified against GASTAT's own PDF releases** (different files and format from the
  spreadsheets parsed here): CPI annual inflation Aug 2026 1.817% vs GASTAT 1.8%;
  CPI monthly +0.13% vs 0.1%; Industrial Production annual −8.13% vs −8.1%; Saudi
  unemployment Q1 2026 6.4% and participation 49.0%, both matching.
- **A blank cell formatted as a date** reads back as Excel's epoch, producing
  observations dated 1900. The warehouse drops periods outside 1960–2035.
- **Bilingual labels can share a cell with no separator.** A header stacked over
  several rows arrives as "الأرقام القياسية Index Numbers الرياض Riyadh". Splitting
  only on newlines and pipes sent the whole thing to Arabic and left the English
  label blank, which collapsed every column of such a table into one series.
  `split_bilingual` now splits per word, by script.
- **Some tables are labelled in Arabic only.** Requiring an English label silently
  discarded every series in two products. The Arabic label is the label.
- **Place names need exact matching, never substrings.** "Thailand" contains *hail*
  and "Harari" contains *arar*, so a substring rule invents Saudi regions out of
  coffee and rice. `build_geography.py` matches whole normalised labels only.
- **GASTAT labels places three ways**: CPI by city, most products by administrative
  region, trade by port or airport. The gazetteer reconciles 65 label forms onto the
  13 regions of the published boundary file.
- **The CPI was rebased** from 2018=100 to 2023=100 partway through the series, and
  the COICOP classification was re-cut at the same time. Splicing across either
  break produces nonsense — a fake 7% deflation in the first case. `index_base` is
  part of every series identity; see `v_classification_break`.
- Excel files are formatted for reading, not loading: banner rows above headers,
  Arabic and English stacked inside single cells, merged multi-level headers,
  years running sideways, months and quarters interleaved in one column.
  This is why phase 2 is a parsing engine rather than a script.

## A note on the Vision 2030 lens

The site groups GASTAT products under Vision 2030's three themes. That grouping is
**ours**, made for navigation. It is not GASTAT's, not Vision 2030's, and the
products are not the programme's official KPIs. No official target value appears
anywhere in this project, and none should be added without a cited source.

## Attribution

GASTAT's [Use Policy](https://www.stats.gov.sa/en/use-policy) clause 1.2.2 permits
reuse, including commercial, provided GASTAT is acknowledged as the official
source and modifications are clearly indicated.

Any published output of this project must therefore:

- credit GASTAT as the official source, on the site and on every export;
- state clearly where figures are derived rather than as-published, and link the
  source file;
- avoid presenting derived figures as official statistics or implying GASTAT
  endorsement (clause 1.4.2);
- use no GASTAT trademark or logo (clause 1.2.3).

Clause 1.1.2 lets GASTAT change these terms without notice — re-read before each
major release.
