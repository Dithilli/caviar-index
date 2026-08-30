#!/usr/bin/env python3
"""
New-source discovery for the caviar price index.

Scans the sitemaps of every domain already in vendors.yaml for caviar-ish
product URLs that are not yet tracked and not previously logged, and appends
them to data/discovery-log.csv as candidates.

Design rules (same philosophy as scrape.py):
  1. Discovery REPORTS; it never edits vendors.yaml. Adding a listing is a
     deliberate operator act — config fields are recorded by hand, verbatim.
  2. Failures are rows, not silent skips. Every domain produces at least one
     row per run, even when nothing new was found.
  3. Append only. Never rewrite an existing row.
  4. Sitemaps are fetched with the same honest user agent, robots.txt
     respected, same politeness delay.
"""

from __future__ import annotations

import csv
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape import robots_allows  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scraper" / "vendors.yaml"
DATA = ROOT / "data" / "discovery-log.csv"

FIELDNAMES = [
    "discovery_id", "discovered_at", "source_domain", "url",
    "matched_keywords", "status", "notes",
]

# A candidate product URL must contain at least one of these in its slug.
KEYWORDS = [
    "caviar", "osetra", "ossetra", "oscietra", "kaluga", "keluga", "beluga",
    "sevruga", "sturgeon", "roe", "baerii", "hackleback", "paddlefish",
    "malossol", "amur",
]

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)


def next_discovery_id(path: Path) -> int:
    if not path.exists():
        return 1
    highest = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            did = (row.get("discovery_id") or "").strip()
            if did.startswith("DSC-"):
                try:
                    highest = max(highest, int(did[4:]))
                except ValueError:
                    pass
    return highest + 1


def known_urls() -> set[str]:
    """Everything already tracked or already logged as a candidate."""
    known = set()
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for listing in cfg.get("listings") or []:
        if listing.get("url"):
            known.add(listing["url"].rstrip("/"))
    if DATA.exists():
        with DATA.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("url"):
                    known.add(row["url"].rstrip("/"))
    return known


def polite_get(session, url, cfg):
    resp = session.get(url, timeout=cfg.get("timeout_seconds", 30))
    base = cfg.get("delay_seconds", 20)
    time.sleep(base + random.uniform(0, base * 0.5))
    return resp


def product_urls_from_sitemaps(base, session, cfg, notes):
    """Fetch /sitemap.xml, follow one level of index into product sitemaps."""
    resp = polite_get(session, f"{base}/sitemap.xml", cfg)
    if resp.status_code != 200:
        notes.append(f"sitemap.xml HTTP {resp.status_code}")
        return None
    locs = LOC_RE.findall(resp.text)
    if "<sitemapindex" in resp.text:
        children = [u for u in locs if "product" in u.lower()][:10]
        if not children:
            notes.append("index has no product sitemap")
            return []
        urls = []
        for child in children:
            sub = polite_get(session, child, cfg)
            if sub.status_code != 200:
                notes.append(f"{child.rsplit('/', 1)[-1]} HTTP {sub.status_code}")
                continue
            urls.extend(LOC_RE.findall(sub.text))
        return urls
    return locs


def scan_domain(base, session, cfg, robots_cache, known, observed_at, next_id):
    """Returns (rows, next_id). At least one row per domain, always."""
    domain = urlparse(base).netloc

    def row(url="", kw="", status="", notes=""):
        return {
            "discovery_id": "",  # filled by caller ordering below
            "discovered_at": observed_at,
            "source_domain": domain,
            "url": url,
            "matched_keywords": kw,
            "status": status,
            "notes": notes[:300],
        }

    ua = cfg["user_agent"]
    allowed, robots_status = robots_allows(f"{base}/sitemap.xml", ua,
                                           robots_cache, session)
    if not allowed:
        status = ("robots_disallowed" if robots_status == "checked"
                  else "robots_unreachable")
        return [row(status=status, notes=f"robots.txt {robots_status}")]

    notes: list[str] = []
    try:
        urls = product_urls_from_sitemaps(base, session, cfg, notes)
    except Exception as exc:
        return [row(status="fetch_error",
                    notes=f"{type(exc).__name__}: {exc}")]
    if urls is None:
        return [row(status="sitemap_missing", notes="; ".join(notes))]

    rows = []
    for url in urls:
        clean = url.strip().rstrip("/")
        slug = urlparse(clean).path.lower()
        if "/product" not in slug or clean in known:
            continue
        matched = [k for k in KEYWORDS if k in slug]
        if not matched:
            continue
        known.add(clean)
        rows.append(row(url=clean, kw=" ".join(matched), status="new",
                        notes="; ".join(notes)))
    if not rows:
        rows.append(row(status="no_new_candidates",
                        notes="; ".join(notes) or f"{len(urls)} urls scanned"))
    return rows


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    listings = cfg.get("listings") or []
    bases = sorted({f"{urlparse(l['url']).scheme}://{urlparse(l['url']).netloc}"
                    for l in listings if l.get("url")})
    if not bases:
        print("No listings configured. Nothing to scan.")
        return 0

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session = requests.Session()
    session.headers.update({
        "User-Agent": cfg["user_agent"],
        "Accept": "text/xml,application/xml,text/html",
    })

    known = known_urls()
    robots_cache: dict = {}
    next_id = next_discovery_id(DATA)
    all_rows = []
    for i, base in enumerate(bases):
        print(f"[{i+1}/{len(bases)}] {base}", flush=True)
        rows = scan_domain(base, session, cfg, robots_cache, known,
                           observed_at, next_id)
        for r in rows:
            r["discovery_id"] = f"DSC-{next_id:06d}"
            next_id += 1
            all_rows.append(r)
            print(f"    -> {r['status']}  {r['url']}", flush=True)

    DATA.parent.mkdir(parents=True, exist_ok=True)
    is_new = not DATA.exists()
    with DATA.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        for r in all_rows:
            writer.writerow(r)

    new = sum(1 for r in all_rows if r["status"] == "new")
    print(f"\nWrote {len(all_rows)} rows ({new} new candidates).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
