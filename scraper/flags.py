#!/usr/bin/env python3
"""
Sampling-rule tripwires over data/price-log.csv.

  2c — price movement: a listing whose price-per-gram moved more than 20%
       in either direction within any 60-day window.
  2d — description drift: a listing whose description_hash changed while
       product_name_as_listed stayed the same.

Prints a markdown report of fired rules; exits 0 with "No flags." when
quiet. Read-only — flags fire purchases per CAVIAR-PROTOCOL.md §2; this
script never buys, edits config, or writes to the logs.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "price-log.csv"
WINDOW_DAYS = 60
THRESHOLD = 0.20


def main() -> int:
    if not LOG.exists():
        print("No price log yet.")
        return 0
    with LOG.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("listing_url")]

    by_listing: dict[str, list[dict]] = {}
    for r in rows:
        by_listing.setdefault(r["listing_url"], []).append(r)

    flags: list[str] = []
    for url, obs in by_listing.items():
        obs.sort(key=lambda r: r["observed_at"])

        # 2c — compare every pair of observations within the window
        priced = [(datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")),
                   float(r["price_per_gram_usd"]), r["observation_id"])
                  for r in obs if r.get("price_per_gram_usd")]
        fired_2c = False
        for i in range(len(priced)):
            if fired_2c:
                break
            for j in range(i + 1, len(priced)):
                t0, p0, id0 = priced[i]
                t1, p1, id1 = priced[j]
                if (t1 - t0).days > WINDOW_DAYS or p0 == 0:
                    continue
                change = (p1 - p0) / p0
                if abs(change) > THRESHOLD:
                    flags.append(
                        f"- **2c** {url}: price/gram {p0:.4f} → {p1:.4f} "
                        f"({change:+.1%}) between {id0} and {id1}")
                    fired_2c = True
                    break

        # 2d — hash changed, name constant, across consecutive hashed rows
        hashed = [r for r in obs
                  if r.get("description_hash") and r.get("product_name_as_listed")]
        for a, b in zip(hashed, hashed[1:]):
            if (a["description_hash"] != b["description_hash"]
                    and a["product_name_as_listed"] == b["product_name_as_listed"]):
                flags.append(
                    f"- **2d** {url}: description_hash "
                    f"{a['description_hash']} → {b['description_hash']} "
                    f"({a['observation_id']} → {b['observation_id']}), "
                    f"name unchanged — diff the snapshots")

    if not flags:
        print("No flags.")
        return 0
    print(f"## Sampling-rule flags — {len(flags)} fired\n")
    print("Each 2c/2d flag generates a purchase under CAVIAR-PROTOCOL.md §2.\n")
    print("\n".join(flags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
