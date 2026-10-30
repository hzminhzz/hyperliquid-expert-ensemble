"""Reconcile HyperX realized PnL from cached Hyperliquid fills and spot fees.

Uses official spotMeta to convert fees paid in base tokens of USDC-quoted spot
pairs at the actual fill price. No order or trading access, and no second
large historical fill download. Every unsupported conversion is censored.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .hyperliquid_chain_verify import compare, csv_write, private_write

INFO_URL = "https://api.hyperliquid.xyz/info"
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"


def spot_pairs(meta: dict[str, Any]) -> dict[str, tuple[str, str]]:
    tokens = {
        int(token["index"]): str(token["name"])
        for token in meta["tokens"] if isinstance(token, dict)
    }
    pairs: dict[str, tuple[str, str]] = {}
    for market in meta["universe"]:
        idx = market.get("tokens")
        if not isinstance(idx, list) or len(idx) != 2:
            continue
        left, right = tokens.get(int(idx[0])), tokens.get(int(idx[1]))
        if left and right:
            pairs[str(market["name"])] = left, right
    return pairs


def fee_in_usdc(fill: dict[str, Any], pairs: dict[str, tuple[str, str]]) -> tuple[Decimal | None, str]:
    fee = Decimal(str(fill.get("fee", "0")))
    token = str(fill.get("feeToken", "UNKNOWN"))
    if token == "USDC":
        return fee, "DIRECT_USDC"
    pair = pairs.get(str(fill.get("coin", "")))
    if pair is not None and pair == (token, "USDC"):
        price = Decimal(str(fill.get("px", "0")))
        if price > 0:
            return fee * price, "SPOT_BASE_AT_EXECUTION_PRICE"
    return None, "UNQUALIFIED_FEE_CONVERSION"


def pnl_from_fills(fills: list[dict[str, Any]], pairs: dict[str, tuple[str, str]]) -> dict[str, Any]:
    gross = Decimal(0)
    fees = Decimal(0)
    converted = 0
    unknown: list[str] = []
    for fill in fills:
        gross += Decimal(str(fill.get("closedPnl", "0")))
        charge, source = fee_in_usdc(fill, pairs)
        if charge is None:
            unknown.append(str(fill.get("feeToken", "UNKNOWN")) + ":" + str(fill.get("coin")))
        else:
            fees += charge
            if source == "SPOT_BASE_AT_EXECUTION_PRICE":
                converted += 1
    return {
        "gross": str(gross),
        "fee_usdc": str(fees) if not unknown else None,
        "net": str(gross - fees) if not unknown else None,
        "converted_spot_base_fees": converted,
        "unconverted": sorted(set(unknown)),
    }


def get_meta() -> dict[str, Any]:
    request = urllib.request.Request(
        INFO_URL, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        data=b'{"type":"spotMeta"}',
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        data = json.loads(response.read(12_000_000))
    if not isinstance(data, dict) or not isinstance(data.get("universe"), list):
        raise TypeError("unexpected official spot metadata")
    return data


def run(hyperx_dir: Path) -> dict[str, Any]:
    original = hyperx_dir / "official-chain"
    with (hyperx_dir / "wallet_90d_verified.csv").open(newline="", encoding="utf-8") as f:
        hyperx = list(csv.DictReader(f))
    if len(hyperx) != 10:
        raise ValueError("missing complete 10-wallet HyperX roster")
    meta = get_meta()
    pairs = spot_pairs(meta)
    dest = hyperx_dir / "spot-fee-reconciled"
    dest.mkdir(parents=False, mode=0o700, exist_ok=False)
    dest.chmod(0o700)
    private_write(dest / "official_spot_metadata.json", json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    rows = []
    for index, record in enumerate(hyperx, 1):
        source = original / f"wallet_{index:02d}_fills.json.gz"
        with gzip.open(source, "rt", encoding="utf-8") as file:
            fills = json.load(file)
        if not isinstance(fills, list):
            raise TypeError("unexpected cached raw fills")
        result = pnl_from_fills(fills, pairs)
        net = result["net"]
        batch_status, batch_diff = compare(net, record["batch_realized_pnl_usd"], net is not None)
        deep_status, deep_diff = compare(net, record["deep_realized_pnl_usd"], net is not None)
        fee_diff = (float(Decimal(result["fee_usdc"]) - Decimal(record["reported_fee_usd"]))
                    if result["fee_usdc"] is not None and record.get("reported_fee_usd") else None)
        row = {
            "rank": index, "wallet_id": record["wallet_id"], "wallet_short": record["wallet_short"],
            "official_fills": len(fills),
            "official_gross_closed_pnl_usdc": result["gross"],
            "official_fee_usdc_with_spot_conversion": result["fee_usdc"],
            "official_net_realized_usdc": net,
            "hyperx_batch_realized": record["batch_realized_pnl_usd"],
            "hyperx_deep_realized": record["deep_realized_pnl_usd"],
            "official_minus_batch": batch_diff,
            "official_minus_deep": deep_diff,
            "official_vs_batch": batch_status,
            "official_vs_deep": deep_status,
            "official_minus_hyperx_deep_fees": fee_diff,
            "converted_spot_fee_fills": result["converted_spot_base_fees"],
            "unconverted_spot_fees": ";".join(result["unconverted"]),
            "quality": "ONCHAIN_RECONCILED_REALIZED_ONLY" if deep_status == "MATCH" else "UNVERIFIED_TOTAL_OR_TIMING",
        }
        rows.append(row)
        print(f"Spot fee verified {index}/10: {row['wallet_short']} net={net} deep={deep_status} fee_diff={fee_diff} spot_fees={result['converted_spot_base_fees']} unknown={result['unconverted']}", flush=True)
    csv_write(dest / "wallet_90d_onchain_verified.csv", rows)
    match_batch = sum(x["official_vs_batch"] == "MATCH" for x in rows)
    match_deep = sum(x["official_vs_deep"] == "MATCH" for x in rows)
    unresolved = sum(bool(x["unconverted_spot_fees"]) for x in rows)
    summary = {
        "source": INFO_URL, "period": "90d",
        "onchain_match_batch": match_batch,
        "onchain_match_deep": match_deep,
        "unresolved_spot_fee_conversions": unresolved,
        "metadata_source": "spotMeta public API",
        "data_window": str(json.loads((original / "manifest.json").read_text()).get("as_of_utc")),
        "read_only": True, "generated_at": datetime.now(UTC).isoformat(),
        "spot_meta_sha256": hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest(),
    }
    private_write(dest / "manifest.json", json.dumps(summary, indent=2))
    lines = [
        "# 90-day HyperX PnL independently reconciled from Hyperliquid fills",
        "",
        "Official userFillsByTime with USDC fees and official spotMeta base-token fee conversions at each fill price. Excludes funding, unrealized PnL and external cash flows. Metadata mappings are assumed stable for the window.",
        "",
        "| Wallet | Filled executions | Official net realized | HyperX fresh realized | Difference | On-chain status |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        def amount(v: Any) -> str:
            return f"{float(v):+,.2f}" if v is not None and v != "" else "NA"
        lines.append(
            f"| {row['wallet_short']} | {row['official_fills']} | {amount(row['official_net_realized_usdc'])} | "
            f"{amount(row['hyperx_deep_realized'])} | {amount(row['official_minus_deep'])} | {row['quality']} |"
        )
    lines += ["", f"Matches with HyperX fresh analysis: {match_deep}/10; cached batch: {match_batch}/10; spot fee conversions unresolved: {unresolved}.",
              "This supports realized PnL net of execution fees only, NOT 90-day complete MTM profitability, funding-adjusted return, portfolio drawdown or future copyable alpha.",
              "", "Original full-address raw fills remain privately archived in the sibling official-chain directory; no credentials were needed for this read-only endpoint.", ""]
    private_write(dest / "report.md", "\n".join(lines))
    print(json.dumps({"report": str(dest), **summary}, indent=2), flush=True)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hyperx-dir", type=Path, required=True)
    args = p.parse_args()
    run(args.hyperx_dir)


if __name__ == "__main__":
    main()
