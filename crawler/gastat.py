"""
GASTAT crawler — stats.gov.sa

Three passes, each resumable:
  1. taxonomy  : the full domain -> subdomain -> product tree (EN + AR)
  2. listings  : every release of every in-scope product, with its file links
  3. files     : download each file, skipping anything unchanged

Design notes
------------
All listings are server-rendered HTML, so no browser is needed.
The site rate-limits readily (429 on ordinary page loads), so the default is
one request per second, single-threaded, with exponential backoff.

Never requests /api/, /c/portal/ or /o/ — those are disallowed by robots.txt.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator

import httpx

BASE = "https://www.stats.gov.sa"
CONTACT = "saudi360-research"

# Tab ids are fixed portal-wide.
TAB_PUBLICATIONS = "436312"
TAB_METHODOLOGY = "436318"
TAB_DASHBOARDS = "436327"

# The listing accepts delta of 20/40/60 only, and `start` is a 1-based PAGE
# NUMBER (not an offset). Results are capped at 100 per category regardless.
PAGE_SIZE = 60
LISTING_CAP = 100

log = logging.getLogger("gastat")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

class Fetcher:
    """Polite HTTP client: paced, retrying, and honest about who it is."""

    def __init__(self, delay: float = 1.0, timeout: float = 90.0):
        self.delay = delay
        self._last = 0.0
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    f"Mozilla/5.0 (compatible; {CONTACT}/1.0; "
                    "+non-commercial statistical research)"
                ),
                "Accept-Language": "en,ar",
            },
        )

    def _pace(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def get(self, url: str, *, tries: int = 5, **kw) -> httpx.Response | None:
        """GET with backoff. Returns None once retries are exhausted."""
        for attempt in range(tries):
            self._pace()
            try:
                r = self.client.get(url, **kw)
            except httpx.HTTPError as exc:
                log.warning("network error %s (attempt %d): %s", url, attempt + 1, exc)
                time.sleep(4 * (attempt + 1))
                continue

            if r.status_code in (429, 503):
                wait = 10 * (attempt + 1)
                log.warning("HTTP %s on %s — backing off %ss", r.status_code, url, wait)
                time.sleep(wait)
                continue
            if r.status_code == 304:          # unchanged, caller handles
                return r
            if r.status_code >= 400:
                log.error("HTTP %s on %s", r.status_code, url)
                return None
            return r

        log.error("giving up on %s after %d attempts", url, tries)
        return None

    def content_length(self, url: str) -> int | None:
        """Size from a HEAD request, or None if the server does not say."""
        self._pace()
        try:
            r = self.client.head(url)
        except httpx.HTTPError:
            return None
        if r.status_code >= 400:
            return None
        try:
            return int(r.headers["Content-Length"])
        except (KeyError, ValueError):
            return None

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


@dataclass
class FileLink:
    url: str
    ext: str


@dataclass
class Release:
    pub_id: str
    title: str
    year: str | None
    periodicity: str | None
    month: str | None
    files: list[FileLink] = field(default_factory=list)


@dataclass
class Product:
    category_id: str
    product: str
    domain: str
    subdomain: str


def parse_taxonomy(page: str, lang: str = "en") -> list[Product]:
    """Domain -> subdomain -> product, from the /statistics page."""
    domains = {
        "119021": {"en": "Economic Statistics", "ar": "الإحصاءات الاقتصادية"},
        "119025": {"en": "Social Statistics", "ar": "الإحصاءات الاجتماعية"},
        "124295": {
            "en": "Environmental and Spatial Statistics",
            "ar": "الإحصاءات البيئية والمكانية",
        },
    }

    sub_names: dict[str, str] = {}
    domain_subs: dict[str, list[str]] = {}
    for m in re.finditer(
        r'href="/statistics\?index=(\d+)&(?:amp;)?subindex=(\d+)"[^>]*>(.*?)</a>',
        page, re.S,
    ):
        dom_id, sub_id, label = m.group(1), m.group(2), _text(m.group(3))
        if dom_id in domains:
            domain_subs.setdefault(dom_id, []).append(sub_id)
            sub_names[sub_id] = label

    sub_products: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(
        r'id="category-collapse-(\d+)"(.*?)(?=<div class=[\'"]accordion-item|$)',
        page, re.S,
    ):
        sub_products[m.group(1)] = [
            (p.group(1), _text(p.group(2)))
            for p in re.finditer(r'data-category-id="(\d+)"\s*>(.*?)</a>', m.group(2), re.S)
        ]

    out: list[Product] = []
    for dom_id, subs in domain_subs.items():
        for sub_id in dict.fromkeys(subs):
            for cat_id, name in sub_products.get(sub_id, []):
                out.append(Product(cat_id, name, domains[dom_id][lang], sub_names[sub_id]))
    return out


def parse_listing(page: str) -> list[Release]:
    """Releases on one listing page. Each is an accordion item."""
    releases: list[Release] = []

    for block in re.split(r"<!-- PUBLICATION ACCORDION ITEM -->", page):
        head = re.search(
            r'data-bs-target="#publication-collapse-(\d+)"(.*?)</a>', block, re.S
        )
        if not head:
            continue

        title = re.sub(r"^>?\s*Publications\s*", "", _text(head.group(2))).lstrip("> ").strip()
        meta = dict(
            re.findall(
                r'<span class="th">\s*(\w+)\s*</span>\s*<span class="td">\s*([^<]*?)\s*</span>',
                block,
            )
        )
        files = [
            FileLink(html.unescape(u), ext)
            for u, ext in re.findall(
                r'href="(/documents/[^"]+)"[^>]*>\s*<i class="dl-file-earmark-(\w+)-icon"',
                block, re.S,
            )
        ]
        releases.append(
            Release(
                pub_id=head.group(1),
                title=title,
                year=meta.get("Year"),
                periodicity=meta.get("Periodicity"),
                month=meta.get("Month"),
                files=files,
            )
        )
    return releases


def claimed_total(page: str) -> int | None:
    """The 'Showing 1 to N of M entries' counter, which can exceed what is served."""
    m = re.search(r"Showing \d+ to \d+ of ([\d,]+) entries", page)
    return int(m.group(1).replace(",", "")) if m else None


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------

def crawl_taxonomy(fetch: Fetcher, lang: str = "en") -> list[Product]:
    r = fetch.get(f"{BASE}/{lang}/statistics")
    if r is None:
        return []
    return parse_taxonomy(r.text, lang)


def crawl_listing(
    fetch: Fetcher, category_id: str, lang: str = "en", tab: str = TAB_PUBLICATIONS
) -> tuple[list[Release], int | None]:
    """Every reachable release for one product.

    The portal serves at most LISTING_CAP releases per category even when the
    counter claims more, so the claimed total is returned alongside for the
    caller to log the shortfall.
    """
    seen: dict[str, Release] = {}
    claimed: int | None = None

    for page_no in range(1, 10):
        url = (
            f"{BASE}/{lang}/statistics-tabs"
            f"?tab={tab}&category={category_id}&delta={PAGE_SIZE}&start={page_no}"
        )
        r = fetch.get(url)
        if r is None:
            break
        if claimed is None:
            claimed = claimed_total(r.text)

        rows = parse_listing(r.text)
        if not rows:
            break
        for rel in rows:
            seen[rel.pub_id] = rel
        if len(seen) >= LISTING_CAP:
            break

    return list(seen.values()), claimed


def blob_path(store: Path, digest: str, ext: str) -> Path:
    """Where a file with this content lives. Fanned out to keep directories small."""
    return store / digest[:2] / f"{digest}{ext}"


def download(
    fetch: Fetcher, file_url: str, store: Path, state: dict, ext: str = "",
    max_bytes: int = 0,
) -> tuple[str, str | None]:
    """Fetch one file into the content-addressed store.

    Roughly half of all file links are the same document referenced from several
    releases — annual compendiums re-link the monthly files they summarise. So
    files are stored once by SHA-256 and referenced by digest, which halves the
    corpus on disk and means phase 2 parses each distinct workbook exactly once.

    Returns (outcome, sha256); outcome is "new", "cached", "oversized" or "failed".
    """
    key = file_url.split("?")[0]
    prior = state.get(key, {})
    if prior.get("skipped") == "oversized" and max_bytes:
        return "oversized", None

    # Known digest whose blob is already on disk: nothing to do, no request.
    if prior.get("sha256"):
        existing = blob_path(store, prior["sha256"], prior.get("ext", ext))
        if existing.exists():
            return "cached", prior["sha256"]

    headers = {}
    if prior.get("etag"):
        headers["If-None-Match"] = prior["etag"]
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = prior["last_modified"]

    # A handful of atlas-style PDFs run to hundreds of megabytes and dominate the
    # corpus. Nothing parses PDFs — they are provenance, and provenance needs the
    # URL, which the manifest already holds. So oversized ones are recorded and
    # skipped rather than stored. max_bytes=0 disables the limit.
    if max_bytes and ext.lower() == ".pdf":
        size = fetch.content_length(BASE + file_url)
        if size and size > max_bytes:
            state[key] = {"skipped": "oversized", "bytes": size, "ext": ext}
            return "oversized", None

    r = fetch.get(BASE + file_url, headers=headers)
    if r is None:
        return "failed", prior.get("sha256")
    if r.status_code == 304:
        return "cached", prior.get("sha256")

    digest = hashlib.sha256(r.content).hexdigest()
    dest = blob_path(store, digest, ext)
    outcome = "cached"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        outcome = "new"

    state[key] = {
        "sha256": digest,
        "ext": ext,
        "etag": r.headers.get("ETag"),
        "last_modified": r.headers.get("Last-Modified"),
        "bytes": len(r.content),
    }
    return outcome, digest


# --------------------------------------------------------------------------
# checkpointing
# --------------------------------------------------------------------------

class Checkpoint:
    """Resume state, flushed after every product."""

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text("utf-8")) if path.exists() else {
            "done": [], "files": {}
        }

    @property
    def done(self) -> set[str]:
        return set(self.data["done"])

    @property
    def files(self) -> dict:
        return self.data["files"]

    def finish(self, category_id: str) -> None:
        if category_id not in self.data["done"]:
            self.data["done"].append(category_id)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
