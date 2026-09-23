# Deploying Saudi 360

The site is static. The pipeline runs on your machine and commits its output, so
Cloudflare builds nothing — it serves `site/` exactly as it is in the repository.

Everything the site needs is committed:

| Path | Size | What it is |
|---|---|---|
| `site/index.html` | 60 KB | the whole application |
| `site/app.json` | 1.1 MB | curated series, breakdowns, provenance |
| `site/geo.json` | 300 KB | the 13 administrative regions |
| `site/data/*.parquet` | 29 MB | the full warehouse, for the Query page |
| `site/_headers` | 1 KB | cache and range-request headers |

## Live site

**https://saudi-360-3e7.pages.dev**

Deployed with Wrangler from this directory. Authentication is a browser OAuth
grant (`npx wrangler login`) stored in your own Wrangler config — no API token
is ever pasted into a chat or committed.

### Deploying an update

```bash
npx wrangler pages deploy site --project-name saudi-360 --branch main
```

Note for whoever runs this next: current Wrangler delegates `pages` commands to
the newer Workers-based Pages and misreads this repository's `wrangler.toml` as
a Worker config. The project was created once with `--force` to pin it to
classic Pages; now that it exists, plain `wrangler pages deploy` works and
`--force` should not be passed again.

### Connecting it to GitHub instead

To have Cloudflare redeploy on every push rather than deploying by hand, attach
the repository in **Workers & Pages → saudi-360 → Settings → Builds → Connect to
Git**, with framework preset **None**, an empty build command and build output
directory **`site`**.

For your own domain, use the project's **Custom domains** tab.

## Refreshing the data

GASTAT publishes monthly. To pick up a new release:

```bash
python run_crawl.py --scope scope_full.json
python run_parse_all.py --scope scope_full.json
python build_warehouse_all.py
python build_geography.py
python export_site.py
python export_geo.py
python build_parquet.py
cp dist/r2/*.parquet dist/r2/manifest.json site/data/
python check_against_source.py
```

`check_against_source.py` exits non-zero if anything is wrong, so it gates the
deploy. **Do not push a refresh it has failed** — it asserts figures against the
published workbooks and against what GASTAT states in prose, and it checks that
the site charts the newest data it holds rather than a series that stopped years
ago. Both of those have caught real faults that every other test passed.

Then commit and push; Cloudflare redeploys on its own.

## The Parquet ships from Pages, at a cost

The whole set is 29 MB and Pages' per-file limit is 25 MiB against a largest
file of 17 MB, so it fits. That removes a bucket, a public bucket URL and a
CORS policy.

**It also costs something, and the cost was measured rather than assumed.**
DuckDB-WASM asks for byte ranges so that a query reads only the row groups it
touches. Pages answers a ranged request for these files with `200` and the whole
file — verified against the deployed site, with and without an `Accept-Ranges`
header of our own, on both a 17 MB file and a 5 KB one. So the first use of the
Query page downloads the set entire. It then works normally: a CPI query over
1.2 million rows returns in about 400 ms.

Repeat visits revalidate and get a `304` while the warehouse is unchanged, so
the download is once per rebuild, not once per visit.

**R2 does serve partial content.** Moving the files there is what makes the page
fetch only the bytes a query touches, and it is the right change if the Query
page gets real use or if mobile visitors matter. It needs an R2 write scope,
which the Wrangler OAuth grant used for this deploy does not include:

1. Create a bucket in the dashboard and make it public.
2. Upload `dist/r2/*` to it.
3. Stop committing `site/data/` (restore the ignore rule).
4. Set `window.SAUDI360_DATA_BASE` in `site/index.html` to the bucket's public
   URL, and allow this origin in the bucket's CORS policy.

Nothing else changes; the page already resolves that base into absolute URLs.

Each refresh also adds roughly 29 MB to the repository's history, which is the
other reason to move to R2 eventually.

## Attribution

The footer carries GASTAT's required attribution and states that the site is not
an official GASTAT publication. GASTAT's Use Policy permits reuse provided the
source is credited and modifications are indicated; it also forbids using
GASTAT's trademarks or logos without written consent, so none appear here.
Clause 1.1.2 lets those terms change without notice, so they are worth
re-reading before any wider publication.
