#!/usr/bin/env python3
"""
Caviar price index scraper.

Design rules (these are deliberate — see CAVIAR-PROTOCOL.md):
  1. Archive raw HTML to disk FIRST, then parse from the file on disk.
     When a vendor changes their markup and a selector breaks, the archive
     lets you reparse history. If you only kept the extracted price, it's gone.
  2. Failures are recorded as rows with scrape_status != "ok" and a null price.
     Never a silent skip. Gaps in the series must be visible as gaps.
  3. Append only. Never rewrite an existing row.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scraper" / "vendors.yaml"
DATA = ROOT / "data" / "price-log.csv"
SNAPSHOTS = ROOT / "snapshots"

FIELDNAMES = [
    "observation_id", "observed_at", "vendor_name", "listing_url",
    "product_name_as_listed", "brand_as_listed", "species_as_listed",
    "grade_as_listed", "origin_as_listed", "net_weight_g",
    "list_price_usd", "sale_price_usd", "price_per_gram_usd",
    "discount_pct_claimed", "in_stock", "shipping_stated_usd",
    "description_hash", "snapshot_path", "scrape_status", "notes",
]

PRICE_RE = re.compile(r"[-+]?[\d,]*\.?\d+")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def parse_price(text):
    """Pull the first number out of a price string. Returns float or None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = str(text).replace(",", "")
    m = PRICE_RE.search(cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def next_observation_id(path: Path) -> int:
    """Highest existing OBS-###### plus one. Never reuses an id."""
    if not path.exists():
        return 1
    highest = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            oid = (row.get("observation_id") or "").strip()
            if oid.startswith("OBS-"):
                try:
                    highest = max(highest, int(oid[4:]))
                except ValueError:
                    pass
    return highest + 1


def robots_allows(url: str, user_agent: str, cache: dict, session):
    """
    Check robots.txt. Fails closed: if we cannot read it, we do not fetch.

    Fetched with OUR declared user agent via the session — the same identity
    whose permission is being checked. (urllib.robotparser's own read() uses
    the Python-urllib default agent, which some WAFs 403 even when the policy
    itself allows everyone; that is a misread of the policy, not a disallow.)

    Returns (allowed: bool, status: str) so the log distinguishes "the site
    told us no" from "the site was unreachable" — those need different
    responses from you.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in cache:
        try:
            resp = session.get(f"{base}/robots.txt", timeout=15)
        except Exception as exc:
            cache[base] = f"unreachable: {type(exc).__name__}"
        else:
            rp = urllib.robotparser.RobotFileParser()
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
                cache[base] = rp
            elif resp.status_code in (401, 403):
                # The site refuses to even serve us the policy: conservative
                # reading is that our agent is not welcome. Distinct from
                # unreachable — do not retry around it.
                rp.disallow_all = True
                cache[base] = rp
            elif 400 <= resp.status_code < 500:
                # No robots.txt (e.g. 404): no rules, everything allowed.
                rp.allow_all = True
                cache[base] = rp
            else:
                # 5xx: the policy exists but can't be read right now.
                cache[base] = f"unreachable: HTTP {resp.status_code}"
    rp = cache[base]
    if isinstance(rp, str):
        return False, rp
    try:
        return rp.can_fetch(user_agent, url), "checked"
    except Exception as exc:
        return False, f"parse failed: {type(exc).__name__}"


# ----------------------------------------------------------------------------
# extraction
# ----------------------------------------------------------------------------

def iter_jsonld(soup):
    """Yield every JSON-LD object on the page, flattening @graph."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def from_jsonld(soup):
    """
    Preferred path: most e-commerce platforms emit schema.org Product markup.
    It is far more stable than CSS selectors, which break on every theme change.
    """
    out = {}
    for node in iter_jsonld(soup):
        types = node.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if not any(str(t).lower() == "product" for t in types):
            continue

        out["product_name_as_listed"] = node.get("name")
        out["description"] = node.get("description")

        brand = node.get("brand")
        if isinstance(brand, dict):
            out["brand_as_listed"] = brand.get("name")
        elif isinstance(brand, str):
            out["brand_as_listed"] = brand

        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            out["sale_price_usd"] = parse_price(offers.get("price"))
            avail = str(offers.get("availability") or "")
            if avail:
                out["in_stock"] = "instock" in avail.lower().replace("_", "")
        if out.get("product_name_as_listed") or out.get("sale_price_usd"):
            return out
    return out


def from_selectors(soup, selectors):
    """Fallback path: per-site CSS selectors from vendors.yaml."""
    out = {}
    if not selectors:
        return out

    def text_at(key):
        sel = selectors.get(key)
        if not sel:
            return None
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else None

    name = text_at("name")
    if name:
        out["product_name_as_listed"] = name

    desc = text_at("description")
    if desc:
        out["description"] = desc

    sale = parse_price(text_at("sale_price"))
    if sale is not None:
        out["sale_price_usd"] = sale

    listp = parse_price(text_at("list_price"))
    if listp is not None:
        out["list_price_usd"] = listp

    ship = parse_price(text_at("shipping"))
    if ship is not None:
        out["shipping_stated_usd"] = ship

    sold_out_sel = selectors.get("sold_out_marker")
    if sold_out_sel:
        out["in_stock"] = soup.select_one(sold_out_sel) is None

    return out


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def scrape_one(listing, cfg, session, robots_cache, day_dir, observed_at):
    """Returns exactly one row dict. Never raises."""
    row = {k: "" for k in FIELDNAMES}
    row["observed_at"] = observed_at
    row["vendor_name"] = listing.get("vendor_name", "")
    row["listing_url"] = listing.get("url", "")

    # Static, operator-recorded fields. These come from config, not the page,
    # because they are the listing's declared claims — recorded once, by hand.
    for key in ("brand_as_listed", "species_as_listed", "grade_as_listed",
                "origin_as_listed", "net_weight_g"):
        if listing.get(key) is not None:
            row[key] = listing[key]

    url = listing.get("url")
    if not url:
        row["scrape_status"] = "config_error"
        row["notes"] = "listing has no url"
        return row

    ua = cfg["user_agent"]
    allowed, robots_status = robots_allows(url, ua, robots_cache, session)
    if not allowed:
        if robots_status == "checked":
            row["scrape_status"] = "robots_disallowed"
            row["notes"] = "robots.txt disallows this path for our user agent"
        else:
            row["scrape_status"] = "robots_unreachable"
            row["notes"] = f"did not fetch; robots.txt {robots_status}"
        return row

    # --- fetch ---------------------------------------------------------
    try:
        resp = session.get(url, timeout=cfg.get("timeout_seconds", 30))
    except Exception as exc:
        row["scrape_status"] = "fetch_error"
        row["notes"] = f"{type(exc).__name__}: {exc}"[:300]
        return row

    if resp.status_code != 200:
        row["scrape_status"] = f"http_{resp.status_code}"
        row["notes"] = "non-200 response"
        return row

    # --- archive BEFORE parsing (rule 1) --------------------------------
    day_dir.mkdir(parents=True, exist_ok=True)
    snap = day_dir / f"{listing['id']}.html"
    try:
        snap.write_text(resp.text, encoding="utf-8")
        row["snapshot_path"] = str(snap.relative_to(ROOT))
    except Exception as exc:
        row["scrape_status"] = "archive_error"
        row["notes"] = f"could not write snapshot: {exc}"[:300]
        return row

    # --- parse from the archived file, not from memory -------------------
    try:
        soup = BeautifulSoup(snap.read_text(encoding="utf-8"), "html.parser")
    except Exception as exc:
        row["scrape_status"] = "parse_error"
        row["notes"] = f"{type(exc).__name__}: {exc}"[:300]
        return row

    extracted = from_jsonld(soup)
    method = "jsonld" if extracted.get("sale_price_usd") is not None else None

    fallback = from_selectors(soup, listing.get("selectors"))
    for k, v in fallback.items():
        if extracted.get(k) in (None, ""):
            extracted[k] = v
            if k == "sale_price_usd" and v is not None:
                method = method or "selectors"

    if extracted.get("product_name_as_listed"):
        row["product_name_as_listed"] = extracted["product_name_as_listed"]
    if extracted.get("brand_as_listed") and not row["brand_as_listed"]:
        row["brand_as_listed"] = extracted["brand_as_listed"]

    sale = extracted.get("sale_price_usd")
    listp = extracted.get("list_price_usd")
    if sale is not None:
        row["sale_price_usd"] = f"{sale:.2f}"
    if listp is not None:
        row["list_price_usd"] = f"{listp:.2f}"
    if extracted.get("shipping_stated_usd") is not None:
        row["shipping_stated_usd"] = f"{extracted['shipping_stated_usd']:.2f}"
    if "in_stock" in extracted and extracted["in_stock"] is not None:
        row["in_stock"] = "TRUE" if extracted["in_stock"] else "FALSE"

    weight = listing.get("net_weight_g")
    if sale is not None and weight:
        try:
            row["price_per_gram_usd"] = f"{sale / float(weight):.4f}"
        except (ValueError, ZeroDivisionError):
            pass

    if sale is not None and listp not in (None, 0):
        try:
            row["discount_pct_claimed"] = f"{(1 - sale / listp) * 100:.1f}"
        except ZeroDivisionError:
            pass

    # description_hash is the drift detector: same product name, changed text.
    desc = extracted.get("description")
    if desc:
        norm = " ".join(str(desc).split()).lower()
        row["description_hash"] = hashlib.sha256(norm.encode()).hexdigest()[:16]

    if sale is None:
        row["scrape_status"] = "no_price_found"
        row["notes"] = "page archived; no price via json-ld or selectors"
    else:
        row["scrape_status"] = "ok"
        row["notes"] = f"extracted_via={method}"

    return row


def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    listings = cfg.get("listings") or []
    if not listings:
        print("No listings configured. Nothing to do.")
        return 0

    contact = cfg.get("contact_email", "")
    if "example.com" in contact or not contact:
        print("WARNING: set a real contact_email in vendors.yaml before running "
              "against live sites.", file=sys.stderr)

    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_dir = SNAPSHOTS / observed_at[:10]

    session = requests.Session()
    session.headers.update({
        "User-Agent": cfg["user_agent"],
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })

    robots_cache: dict = {}
    next_id = next_observation_id(DATA)
    rows = []

    for i, listing in enumerate(listings):
        print(f"[{i+1}/{len(listings)}] {listing.get('id')}", flush=True)
        row = scrape_one(listing, cfg, session, robots_cache, day_dir, observed_at)
        row["observation_id"] = f"OBS-{next_id:06d}"
        next_id += 1
        rows.append(row)
        print(f"    -> {row['scrape_status']}  {row['sale_price_usd'] or ''}",
              flush=True)

        if i < len(listings) - 1:
            base = cfg.get("delay_seconds", 20)
            time.sleep(base + random.uniform(0, base * 0.5))

    DATA.parent.mkdir(parents=True, exist_ok=True)
    is_new = not DATA.exists()
    with DATA.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)

    ok = sum(1 for r in rows if r["scrape_status"] == "ok")
    print(f"\nWrote {len(rows)} observations ({ok} ok, {len(rows)-ok} flagged).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
