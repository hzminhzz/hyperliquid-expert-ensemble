"""Read-only diagnostics for retrospective fill-replay source continuity."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from decimal import Decimal
import json

from .pm_fill_replay import _coins, _source_fills
from .pm_sources import load_cohort


def diagnose(root: Path) -> dict:
    entries, _ = load_cohort(root)
    out = []
    for entry in entries:
        coins = _coins(root, entry.alias)
        events, errors = _source_fills(root, entry.alias, entry.address, coins)
        bycoin = defaultdict(list)
        for e in events:
            bycoin[e[2]].append(e)
        seen_coin = set(bycoin)
        missing = sorted(set(coins) - seen_coin)
        starts = {coin: rows[0][0] for coin, rows in bycoin.items()}
        anchor = max(starts.values()) if not missing and starts else None
        first_gaps = []
        for coin, rows in sorted(bycoin.items()):
            for prev, cur in zip(rows, rows[1:]):
                tolerance = max(abs(prev[4]), abs(cur[3]), Decimal(1)) * Decimal("0.0000001")
                if abs(prev[4] - cur[3]) > tolerance:
                    first_gaps.append(
                        {
                            "coin": coin,
                            "time": cur[0].isoformat(),
                            "before": str(cur[3]),
                            "prev_after": str(prev[4]),
                            "before_anchor": anchor is not None and cur[0] <= anchor,
                        }
                    )
        out.append(
            {
                "alias": entry.alias,
                "cohort": entry.cohort,
                "coins": len(coins),
                "active_coins": len(seen_coin),
                "fills": len(events),
                "missing_pre_cutoff_coins": missing,
                "anchor": anchor.isoformat() if anchor else None,
                "events_after_anchor": sum(e[0] > anchor for e in events) if anchor else 0,
                "gaps_before_anchor": sum(x["before_anchor"] for x in first_gaps),
                "gaps_after_anchor": sum(not x["before_anchor"] for x in first_gaps),
                "first_gaps": first_gaps[:4],
                "source_read_errors": errors[:8],
                "first_nonflat_pairs": sum(rows[0][3] != 0 for rows in bycoin.values()),
            }
        )
    return {"wallet_count": len(out), "wallets": out}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = diagnose(args.research_root)
    if args.summary:
        rows = [
            {
                k: v
                for k, v in x.items()
                if k
                in (
                    "alias",
                    "coins",
                    "fills",
                    "events_after_anchor",
                    "gaps_before_anchor",
                    "gaps_after_anchor",
                    "source_read_errors",
                    "missing_pre_cutoff_coins",
                )
            }
            for x in result["wallets"]
        ]
        print(json.dumps({"wallet_count": len(rows), "wallets": rows}, indent=2))
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
