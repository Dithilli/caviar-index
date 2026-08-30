# caviar-index — price scraper

Daily price and description tracking for caviar listings. Feeds the sampling
rules in `CAVIAR-PROTOCOL.md` (§2c price movement, §2d description drift).

## Setup

**1. Create the repo.** Public is fine and preferred — Actions minutes are free
on public repos, and the commit history becomes a tamper-evident audit trail,
which satisfies protocol R7 for free.

```
git init
git add .
git commit -m "initial"
git remote add origin git@github.com:YOU/caviar-index.git
git push -u origin main
```

**2. Set your contact email.** Edit `scraper/vendors.yaml` and replace both
instances of `YOUR_EMAIL_HERE`. The scraper warns if you don't.

**3. Add your listings.** Copy the template block in `vendors.yaml`, one per
listing. Delete the example. Start with five to ten.

**4. Try it without selectors first.** Most Shopify, WooCommerce and
BigCommerce stores emit schema.org Product JSON-LD, which the scraper reads
automatically and which survives theme changes. Only add a `selectors:` block
if a run comes back `no_price_found`.

**5. Test locally before scheduling.**

```
pip install -r scraper/requirements.txt
python scraper/scrape.py
```

Check `data/price-log.csv`. Every listing should produce exactly one row.

**6. Enable Actions.** Settings → Actions → General → Workflow permissions →
**Read and write permissions**. Without this the commit step fails. Then run it
once manually from the Actions tab before trusting the schedule.

## Statuses

Every run writes one row per listing, always. A failed scrape is a row with a
null price and a status — never a silent skip, because a gap in the series must
be visible as a gap.

| status | meaning |
|---|---|
| `ok` | price extracted; `notes` records whether via json-ld or selectors |
| `no_price_found` | page fetched and archived, no price located — add or fix selectors |
| `http_404` etc. | non-200; listing may have been pulled |
| `robots_disallowed` | robots.txt forbids this path for our agent — do not work around it |
| `robots_unreachable` | robots.txt couldn't be read; fails closed, did not fetch |
| `fetch_error` | network failure |
| `archive_error` | couldn't write the snapshot; nothing parsed |

## Notes

- **Raw HTML is archived before parsing**, in `snapshots/YYYY-MM-DD/`. When a
  vendor changes markup and a selector breaks, you can reparse the archive.
  This is the single most important design decision in here.
- **`description_hash`** is the drift detector: same product name, changed
  description text. Fires protocol §2d.
- **Scheduled Actions runs are best-effort.** GitHub queues them and they can
  be late or occasionally skipped. Fine for a daily price series; don't build
  anything on exact timing.
- **Repos with no activity for 60 days have scheduled workflows disabled.** If
  you're only committing scraper output, that counts as activity — but if you
  pause the project, check that it's still running when you come back.
- **Don't lower `delay_seconds`.** You're making a handful of requests a day.

## Auditing

The audit trail is provable, not merely claimed. Three mechanisms:

1. **Branch protection.** `main` cannot be force-pushed or deleted, by anyone,
   including the repo owner. History only grows.
2. **The auditor** — `scraper/audit.py`, run by `.github/workflows/audit.yml`
   on every push, on a daily schedule after the scrape, and again at the end
   of each scrape run. Over the full commit history it verifies:
   - append-only logs: every committed version of the data CSVs and the
     anchors file is a byte-for-byte prefix of the next
   - observation ids unique, sequential, gap-free
   - snapshots write-once: never modified or deleted after commit
   - every snapshot referenced by a row exists
   - no duplicate (observed_at, listing_url) observation
   A red X on the audit workflow is the alarm. Anyone can verify
   independently: clone (not shallow) and run `python scraper/audit.py`.
3. **Pre-registration anchors** — `anchors/pre-registration.sha256` holds
   hashes of the project's governing documents, committed before data
   collection began. The documents are disclosed at publication; matching
   hashes prove they predate the data and were not revised to fit it.

## Discovery

`scraper/discover.py` (weekly via `.github/workflows/discover.yml`) scans the
sitemaps of every tracked domain for caviar-ish product URLs not yet tracked
and appends candidates to `data/discovery-log.csv` (append-only, audited like
the price log). It reports; it never edits `vendors.yaml` — adding a listing
stays a deliberate operator act, with claims recorded verbatim by hand.
- **Browser-mode listings**: `fetch_mode: browser` on a listing renders the
  page in headless Chromium (same honest user agent, robots respected) and
  archives the RENDERED DOM — for vendors whose price is injected client-side.
