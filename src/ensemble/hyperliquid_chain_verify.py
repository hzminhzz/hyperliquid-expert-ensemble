"""Independent read-only Hyperliquid 90d fill PnL reconciliation.

Official public Info API, never trading endpoints. Fills are bounded by the
documented 2,000/page and most-recent-10,000 limitations.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

API = "https://api.hyperliquid.xyz/info"
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"
MAX_PAGES = 6
PAGE_SIZE = 2000


def private_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    path.chmod(0o600)


def summarize(fills: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = sorted({str(x.get("feeToken", "UNKNOWN")) for x in fills})
    if any(token != "USDC" for token in tokens):
        return {"gross": None, "fees": None, "net": None, "tokens": tokens,
                "status": "UNCONVERTED_NON_USDC_FEES"}
    gross = sum((Decimal(str(f.get("closedPnl", "0"))) for f in fills), Decimal(0))
    fees = sum((Decimal(str(f.get("fee", "0"))) for f in fills), Decimal(0))
    return {"gross": str(gross), "fees": str(fees), "net": str(gross - fees),
            "tokens": tokens, "status": "USDC_FEE_RECONSTRUCTABLE"}


def compare(net: str | None, reported: str | None, complete: bool) -> tuple[str, float | None]:
    if not complete or net is None or not reported:
        return "INCONCLUSIVE", None
    diff = float(Decimal(net) - Decimal(reported))
    tol = max(0.05, abs(float(reported)) * 0.0001)
    return ("MATCH" if abs(diff) <= tol else "DIFFERS", diff)


class InfoClient:
    def __init__(self) -> None:
        self.count = 0
        self.previous = 0.0

    def fills_page(self, wallet: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        gap = time.monotonic() - self.previous
        if self.count and gap < 7.5:
            time.sleep(7.5 - gap)
        body = {"type": "userFillsByTime", "user": wallet,
                "startTime": start_ms, "endTime": end_ms,
                "aggregateByTime": False}
        request = urllib.request.Request(
            API, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            data=json.dumps(body, separators=(",", ":")).encode(),
        )
        for attempt in range(2):
            self.previous = time.monotonic()
            self.count += 1
            try:
                with urllib.request.urlopen(request, timeout=40) as response:
                    result = json.loads(response.read(25_000_000))
                if not isinstance(result, list):
                    raise TypeError("Unexpected Hyperliquid fill response")
                return result
            except urllib.error.HTTPError as error:
                if attempt == 0 and error.code in (429, 503):
                    time.sleep(62)
                    continue
                raise RuntimeError(f"Hyperliquid API HTTP {error.code}") from None
        raise RuntimeError("Info API retry budget exceeded")


def get_full_fills(client: InfoClient, wallet: str, start_ms: int, end_ms: int) -> tuple[list[dict[str, Any]], list[str], int]:
    fills: list[dict[str, Any]] = []
    seen: set[str] = set()
    warnings: list[str] = []
    cursor = start_ms
    pages = 0
    for _ in range(MAX_PAGES):
        batch = client.fills_page(wallet, cursor, end_ms)
        pages += 1
        if not batch:
            break
        for record in batch:
            when = record.get("time")
            if not isinstance(when, int) or not start_ms <= when <= end_ms:
                warnings.append("OUT_OF_RANGE_OR_MALFORMED_FILL")
                continue
            key = str(record.get("tid") or (record.get("hash"), record.get("oid"), when, record.get("sz")))
            if key not in seen:
                seen.add(key)
                fills.append(record)
        if len(batch) < PAGE_SIZE:
            break
        newest = max(int(x["time"]) for x in batch)
        if newest <= cursor:
            warnings.append("NO_PAGINATION_PROGRESS")
            break
        cursor = newest
        if len(fills) >= 10000:
            warnings.append("RECENT_10000_HISTORY_LIMIT")
            break
    else:
        warnings.append("MAX_PAGES_REACHED")
    if len(fills) >= 10000:
        warnings.append("POTENTIAL_PRE_WINDOW_TRUNCATION")
    fills.sort(key=lambda x: int(x["time"]))
    return fills, sorted(set(warnings)), pages


def run(hyperx_dir: Path) -> dict[str, Any]:
    with (hyperx_dir / "wallet_90d_verified.csv").open(newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))
    if len(candidates) != 10:
        raise ValueError("incomplete ten-wallet source")
    manifest = json.loads((hyperx_dir / "manifest.json").read_text())
    until = datetime.fromisoformat(manifest["as_of_utc"]).astimezone(UTC)
    from_time = until - timedelta(days=90)
    start_ms = int(from_time.timestamp() * 1000)
    end_ms = int(until.timestamp() * 1000)
    out = hyperx_dir / "official-chain"
    out.mkdir(parents=False, mode=0o700, exist_ok=False)
    out.chmod(0o700)
    client = InfoClient()
    results: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, 1):
        wallet = candidate["wallet_id"]
        fills, warnings, pages = get_full_fills(client, wallet, start_ms, end_ms)
        calc = summarize(fills)
        complete = not warnings and calc["status"] == "USDC_FEE_RECONSTRUCTABLE"
        batch_status, batch_diff = compare(calc["net"], candidate["batch_realized_pnl_usd"], complete)
        deep_status, deep_diff = compare(calc["net"], candidate["deep_realized_pnl_usd"], complete)
        last_hyperx = datetime.fromisoformat(candidate["trade_time_last_utc"]).timestamp() * 1000 if candidate.get("trade_time_last_utc") else None
        extra = sum(last_hyperx is not None and f["time"] > last_hyperx for f in fills)
        result = {
            "previous_predictive_rank": candidate["previous_predictive_rank"],
            "wallet_id": wallet,
            "wallet_short": candidate["wallet_short"],
            "window_start_utc": from_time.isoformat(),
            "window_end_utc": until.isoformat(),
            "official_fill_count": len(fills),
            "official_pages": pages,
            "official_gross_closed_usdc": calc["gross"],
            "official_fees_usdc": calc["fees"],
            "official_net_realized_usdc": calc["net"],
            "hyperx_batch_realized_usd": candidate["batch_realized_pnl_usd"],
            "hyperx_deep_realized_usd": candidate["deep_realized_pnl_usd"],
            "official_minus_hyperx_batch_usd": batch_diff,
            "official_minus_hyperx_deep_usd": deep_diff,
            "batch_reconciliation": batch_status,
            "deep_reconciliation": deep_status,
            "newer_fills_than_provider_last_trade": extra,
            "earliest_onchain_fill_utc": datetime.fromtimestamp(fills[0]["time"]/1000, UTC).isoformat() if fills else None,
            "last_onchain_fill_utc": datetime.fromtimestamp(fills[-1]["time"]/1000, UTC).isoformat() if fills else None,
            "fee_token_types": json.dumps(calc["tokens"]),
            "history_quality": ";".join(warnings) or "COMPLETE_TO_RETRIEVAL_LIMITS",
        }
        results.append(result)
        with gzip.open(out / f"wallet_{index:02d}_fills.json.gz", "wt", encoding="utf-8") as f:
            json.dump(fills, f, separators=(",", ":"))
        (out / f"wallet_{index:02d}_fills.json.gz").chmod(0o600)
        csv_write(out / "onchain_reconciliation.csv", results)
        print(f"On-chain {index}/10 {candidate['wallet_short']}: fills={len(fills)} gross={calc['gross']} fee={calc['fees']} net={calc['net']} versus_batch={batch_status} versus_deep={deep_status} warnings={warnings}", flush=True)
    matches_batch = sum(x["batch_reconciliation"] == "MATCH" for x in results)
    matches_deep = sum(x["deep_reconciliation"] == "MATCH" for x in results)
    flagged = sum(x["history_quality"] != "COMPLETE_TO_RETRIEVAL_LIMITS" for x in results)
    lines = [
        "# Hyperliquid official API: 90-day on-chain fill reconciliation",
        "",
        f"Cut: {from_time.isoformat()} to {until.isoformat()} (HyperX batch-start cutoff).",
        "Independent public source: https://api.hyperliquid.xyz/info, userFillsByTime.",
        "API limitations: maximum 2000 per page; only last 10000 fills available. Reported reconstruction sums official closedPnl minus USDC fees; it excludes funding, transfers and open-position MTM.",
        "",
        "| Wallet | Fills | Official realized USD | HyperX batch USD | Difference USD | Match |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in results:
        a = float(row["official_net_realized_usdc"]) if row["official_net_realized_usdc"] else None
        b = float(row["hyperx_batch_realized_usd"]) if row["hyperx_batch_realized_usd"] else None
        d = row["official_minus_hyperx_batch_usd"]
        lines.append(f"| {row['wallet_short']} | {row['official_fill_count']} | {a:+,.2f} | {b:+,.2f} | {d:+,.2f} | {row['batch_reconciliation']} |" if a is not None and b is not None and d is not None else f"| {row['wallet_short']} | {row['official_fill_count']} | NA | NA | NA | {row['batch_reconciliation']} |")
    lines += ["", f"Matched with HyperX batch: {matches_batch}/10. Matched with HyperX deep recompute: {matches_deep}/10. Incomplete histories: {flagged}/10.",
              "Differences may reflect the provider's cached snapshot, cutoff differences or fills after the provider update; no invalid ROI claims are inferred.",
              "Full wallet IDs and compressed raw fills are private and not committed.", ""]
    private_write(out / "report.md", "\n".join(lines))
    private_write(out / "manifest.json", json.dumps({
        "source": API, "period": "90d", "as_of_utc": until.isoformat(),
        "calls": client.count, "match_batch": matches_batch,
        "match_deep": matches_deep, "incomplete": flagged,
        "read_only": True,
    }, indent=2))
    summary = {"report": str(out), "match_batch": matches_batch,
               "match_deep": matches_deep, "incomplete": flagged}
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hyperx-dir", type=Path, required=True)
    args = p.parse_args()
    run(args.hyperx_dir)


if __name__ == "__main__":
    main()
