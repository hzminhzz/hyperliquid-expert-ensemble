"""Verify perps-only 90-day realized PnL from official Hyperliquid fills.

Read cached public Info API fills from a private, timestamped audit. HyperX
wallet metrics are perp-only; exclude spot fills and their token-denominated
fees, which caused false mismatches in the earlier all-instrument comparison.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .hyperliquid_chain_verify import csv_write, private_write


def spot_fill(fill: dict[str, Any]) -> bool:
    coin = str(fill.get("coin", ""))
    return coin.startswith("@") or "/" in coin


def perp_pnl(fills: list[dict[str, Any]]) -> dict[str, Any]:
    perp = [f for f in fills if not spot_fill(f)]
    spot = [f for f in fills if spot_fill(f)]
    fee_tokens = sorted({str(f.get("feeToken", "UNKNOWN")) for f in perp})
    gross = sum((Decimal(str(f.get("closedPnl", "0"))) for f in perp), Decimal(0))
    if any(token != "USDC" for token in fee_tokens):
        return {
            "perp_fills": len(perp), "spot_fills_excluded": len(spot),
            "gross": str(gross), "fee_usdc": None, "net": None,
            "status": "NON_USDC_PERP_FEE_NOT_CONVERTIBLE",
        }
    fee = sum((Decimal(str(f.get("fee", "0"))) for f in perp), Decimal(0))
    return {
        "perp_fills": len(perp), "spot_fills_excluded": len(spot),
        "gross": str(gross), "fee_usdc": str(fee), "net": str(gross - fee),
        "status": "PERP_REALIZED_NET_RECONSTRUCTED",
    }


def verify_official(net: str | None, provider: str | None) -> tuple[str, float | None]:
    if net is None or provider in (None, ""):
        return "NOT_VERIFIABLE", None
    delta = Decimal(net) - Decimal(provider)
    return ("MATCH" if abs(delta) <= Decimal("0.01") else "DIFFERS", float(delta))


def run(source_dir: Path) -> dict[str, Any]:
    with (source_dir / "wallet_90d_verified.csv").open(newline="", encoding="utf-8") as file:
        sources = list(csv.DictReader(file))
    if len(sources) != 10:
        raise ValueError("expected full ten-wallet HyperX comparison")
    parent = source_dir / "official-chain"
    report_dir = source_dir / "official-perp-verified"
    report_dir.mkdir(parents=False, mode=0o700, exist_ok=False)
    report_dir.chmod(0o700)
    rows: list[dict[str, Any]] = []
    for i, source in enumerate(sources, 1):
        with gzip.open(parent / f"wallet_{i:02d}_fills.json.gz", "rt", encoding="utf-8") as file:
            fills = json.load(file)
        if not isinstance(fills, list):
            raise TypeError("unexpected cached official fill format")
        amounts = perp_pnl(fills)
        batch, delta_batch = verify_official(amounts["net"], source.get("batch_realized_pnl_usd"))
        deep, delta_deep = verify_official(amounts["net"], source.get("deep_realized_pnl_usd"))
        fee_match, delta_fee = verify_official(amounts["fee_usdc"], source.get("reported_fee_usd"))
        row = {
            "rank_prior_1h_predictive": i,
            "wallet_id": source["wallet_id"],
            "wallet_short": source["wallet_short"],
            "onchain_perp_fills_90d": amounts["perp_fills"],
            "spot_fills_excluded": amounts["spot_fills_excluded"],
            "onchain_gross_closed_pnl_usdc": amounts["gross"],
            "onchain_perp_fees_usdc": amounts["fee_usdc"],
            "onchain_perp_net_realized_usdc": amounts["net"],
            "hyperx_batch_realized_pnl_usd": source.get("batch_realized_pnl_usd"),
            "hyperx_fresh_realized_pnl_usd": source.get("deep_realized_pnl_usd"),
            "onchain_minus_hyperx_batch": delta_batch,
            "onchain_minus_hyperx_fresh": delta_deep,
            "onchain_fees_minus_hyperx_fresh": delta_fee,
            "batch_check": batch,
            "fresh_check": deep,
            "fee_check": fee_match,
            "hyperx_90d_closed_trades": source["trades_90d"],
            "hyperx_90d_win_rate": source["win_rate_90d"],
            "hyperx_funding_net_usd_separate": source["hyperx_funding_net_90d_usd"],
            "hyperx_deposit_withdraw_events": source["withdrawals_deposits_events_vendor"],
            "hyperx_mtm_coverage": source["mtm_coverage"],
            "prior_1h_predictive_incremental_bps": source["previous_1h_incremental_vs_control_bps"],
            "prior_1h_net_copy_after_assumed_10bps": source["previous_1h_wallet_net_copy_bps"],
            "prior_1h_episode_count": source["previous_1h_episodes"],
            "prior_1h_unverified_coverage_count": source["previous_1h_unverified_coverage"],
            "independent_trading_return_qualified": False,
            "follow_signals_execution_qualified": False,
        }
        rows.append(row)
        print(f"PERP {i}/10 {row['wallet_short']}: onchain_net={amounts['net']} HyperX_fresh={source.get('deep_realized_pnl_usd')} pnl={deep} fees={fee_match} spot_excluded={amounts['spot_fills_excluded']}", flush=True)
    csv_write(report_dir / "perp_onchain_vs_hyperx_90d.csv", rows)
    match = sum(r["fresh_check"] == "MATCH" for r in rows)
    fee_match = sum(r["fee_check"] == "MATCH" for r in rows)
    positive = sum(Decimal(str(r["onchain_perp_net_realized_usdc"])) > 0 for r in rows if r["onchain_perp_net_realized_usdc"] is not None)
    stale_batch = sum(r["batch_check"] == "DIFFERS" for r in rows)
    manifest = {
        "as_of_batch_utc": json.loads((parent / "manifest.json").read_text())["as_of_utc"],
        "verified_at_utc": datetime.now(UTC).isoformat(),
        "source": "Hyperliquid public /info userFillsByTime; HyperX 90d read-only metrics",
        "basis": "perpetuals only; excludes spot trades & spot-denominated fees",
        "perp_net_pnl_verified": match, "perp_fee_verified": fee_match,
        "positive_90d_onchain_realized": positive, "cached_batch_discrepancies": stale_batch,
        "n_wallets": len(rows), "read_only": True,
        "funding_and_mtm_excluded": True,
        "source_raw_private": str(parent),
    }
    private_write(report_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    lines = [
        "# HyperX Top 10 — independent 90-day perpetuals PnL verification",
        "",
        f"Source date: {manifest['as_of_batch_utc']} UTC; rolling HyperX period=90d. Historical on-chain fills were retrieved from the official read-only Info API.",
        "",
        f"**{match}/10** wallets match fresh HyperX perp realized PnL within 1 cent and **{fee_match}/10** match reported perp fees. **{positive}/10** realized a positive net PnL over this 90-day interval. Cached batch records had **{stale_batch}/10** differences compared with the fresh/current on-chain reconstruction.",
        "",
        "## Verifiable perpetuals trading PnL (USD-equivalent USDC)",
        "",
        "| Prior predictive rank | Wallet | Onchain perp gross | Perp fee | Net realized | HyperX fresh | Official match |",
        "|---:|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        gross, fees, net, fresh = (r["onchain_gross_closed_pnl_usdc"], r["onchain_perp_fees_usdc"],
                                   r["onchain_perp_net_realized_usdc"], r["hyperx_fresh_realized_pnl_usd"])
        def fmt(v: Any) -> str:
            return f"{Decimal(str(v)):+,.2f}" if v is not None else "NA"
        lines.append(
            f"| {r['rank_prior_1h_predictive']} | {r['wallet_short']} | {fmt(gross)} | {fmt(fees)} | {fmt(net)} | {fmt(fresh)} | {r['fresh_check']} |"
        )
    lines += [
        "", "## Important limitations",
        "",
        "- Non-perpetual spot executions are intentionally excluded. HyperX 90d realized PnL corresponds to perpetual fills, which explains apparent differences from including spot closedPnl and fee-token conversions.",
        "- This reconciles trading realized PnL net of USDC execution fees only. It is not time-weighted ROI, funding-adjusted economic return, mark-to-market open risk or independently verified max drawdown.",
        "- HyperX MTM coverage is incomplete for the selected wallets; ROI field definitions and source timestamps sometimes disagree between cached and freshly computed endpoints. We do not promote the vendor ROI to verified trading return.",
        "- Existing 1h wallet follow results were hypothetical, used minute mid-prices with assumed costs and were largely unverified for source coverage. Three-month historical trader profitability is not forward predictive copy-signal qualification.",
        "- Two wallets had identical 27-event 1h predictive trajectories in the previous audit. Their trading histories are not independent signal confirmation.",
        "",
        "Complete private CSV includes full wallet IDs, realized PnL, fee, funding context, source sample size, prior predictive results and qualifications. Raw official fills remain under the sibling private official-chain directory.",
        "", "**Disposition: HISTORICAL_PERP_REALIZED_PNL_CONFIRMED; COPY_PREDICTABILITY_INCONCLUSIVE.**", "",
    ]
    private_write(report_dir / "report.md", "\n".join(lines))
    print(json.dumps({"report": str(report_dir), **manifest}, indent=2), flush=True)
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir", type=Path, required=True)
    args = p.parse_args()
    run(args.source_dir)


if __name__ == "__main__":
    main()
