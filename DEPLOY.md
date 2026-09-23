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

## One-time setup

Connecting a repository is an account action, so it happens in your dashboard,
not from here. No token is ever pasted into a chat.

1. Go to **Cloudflare dashboard → Workers & Pages → Create → Pages → Connect to Git**.
2. Authorise GitHub and pick **falsaawi/saudi-360**.
3. Settings:
   - Production branch: `main`
   - Framework preset: **None**
   - Build command: **leave empty**
   - Build output directory: **`site`**
4. **Save and Deploy.**

The first deploy takes a minute or two. After that every `git push` to `main`
redeploys automatically.

You will get `https://saudi-360.pages.dev`. To use your own domain, add it under
the project's **Custom domains** tab.

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

## Why the Parquet ships from Pages rather than R2

The Query page reads the warehouse with HTTP range requests, which R2 was meant
to serve. At 29 MB the whole set fits in Pages, whose per-file limit is 25 MiB
against a largest file of 17 MB. That removes a bucket, a public bucket URL and
a CORS policy for no loss of function.

Each refresh adds roughly 29 MB to the repository's history. If that becomes
awkward, upload `dist/r2/` to an R2 bucket, stop committing `site/data/`, and
point the page at the bucket by setting `window.SAUDI360_DATA_BASE` in
`site/index.html` to the bucket's public URL. Nothing else changes.

## Attribution

The footer carries GASTAT's required attribution and states that the site is not
an official GASTAT publication. GASTAT's Use Policy permits reuse provided the
source is credited and modifications are indicated; it also forbids using
GASTAT's trademarks or logos without written consent, so none appear here.
Clause 1.1.2 lets those terms change without notice, so they are worth
re-reading before any wider publication.
