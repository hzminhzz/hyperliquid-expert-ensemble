"""Read-only HyperX 90-day verification of selected wallet research candidates.

Three *HyperX* endpoints are compared; this is not independent on-chain
verification. Provider ROI/MTM is never promoted to cash-flow-adjusted return.
No execution endpoints, keys, source journals or production services modified.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BASE = "https://data-api.hyperx.trade"
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
PERIOD = "90d"
MAX_CANDIDATES = 10
WEIGHT_LIMIT_PER_MINUTE = 23
ALLOWED_DIVERGENCE_USD = 0.02


def finite(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def short(address: str) -> str:
    return address[:7] + "..." + address[-5:]


def read_candidates(path: Path, n: int = MAX_CANDIDATES) -> list[dict[str, Any]]:
    if not 1 <= n <= MAX_CANDIDATES:
        raise ValueError("candidate count exceeded")
    with path.open(newline="", encoding="utf-8") as f:
        items = list(csv.DictReader(f))[:n]
    if len(items) != n:
        raise ValueError("private historical predictive roster incomplete")
    seen: set[str] = set()
    for i, row in enumerate(items, 1):
        address = str(row.get("wallet_id", "")).lower()
        if not ADDRESS.fullmatch(address) or address in seen:
            raise ValueError("invalid or duplicate historical candidate address")
        row["wallet_id"] = address
        row["previous_predictive_rank"] = i
        seen.add(address)
    return items


def private_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        private_write(path, "NO_ROWS\n")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    path.chmod(0o600)


class ApiClient:
    """Conservative local budget, no retry storm, optional explicit env token."""

    def __init__(self, base: str = BASE, max_weight: int = WEIGHT_LIMIT_PER_MINUTE) -> None:
        if base != BASE:
            raise ValueError("untrusted API host")
        self.base = base
        self.max_weight = max_weight
        self.calls: deque[tuple[float, int]] = deque()
        self.weight_used = 0
        self.auth = os.environ.get("HYPERX_API_TOKEN") or os.environ.get("HYPERX_API_KEY")
        self.auth_configured = bool(self.auth)
        self.request_records: list[dict[str, Any]] = []

    def throttle(self, weight: int) -> None:
        while True:
            now = time.monotonic()
            while self.calls and now - self.calls[0][0] >= 61:
                self.calls.popleft()
            if weight + sum(x[1] for x in self.calls) <= self.max_weight:
                self.calls.append((now, weight))
                self.weight_used += weight
                return
            time.sleep(max(0.5, 61 - (now - self.calls[0][0])))

    def request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, weight: int) -> dict[str, Any]:
        if not path.startswith(("/wallet_metrics/", "/wallet_metrics_query", "/wallet_analysis/")):
            raise ValueError("endpoint not allowed in verification audit")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.auth:
            headers["X-API-Key"] = self.auth
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload or {}, separators=(",", ":")).encode()
        self.throttle(weight)
        url = self.base + path
        for attempt in range(2):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=40) as response:
                    raw = response.read(12_000_000)
                    status = int(response.status)
                    remaining = response.headers.get("X-Weight-Remaining")
                    self.request_records.append({
                        "endpoint_family": path.split("/")[1], "method": method,
                        "weight": weight, "http_status": status,
                        "weight_remaining": remaining, "received_at_utc": datetime.now(UTC).isoformat(),
                        "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    })
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise TypeError("unexpected HyperX response")
                    return parsed
            except urllib.error.HTTPError as error:
                code = int(error.code)
                if code == 429 and attempt == 0:
                    time.sleep(62)
                    continue
                if 500 <= code <= 599 and attempt == 0:
                    time.sleep(4)
                    continue
                raise RuntimeError(f"HyperX response HTTP {code} for endpoint family {path.split('/')[1]}") from None
            except (TimeoutError, urllib.error.URLError) as error:
                if attempt == 0:
                    time.sleep(3)
                    continue
                raise RuntimeError(f"HyperX transport failed for {path.split('/')[1]}: {type(error).__name__}") from None
        raise RuntimeError("HyperX endpoint exhausted retry budget")


def verify_three_views(
    predictive: dict[str, Any], batch: dict[str, Any] | None,
    quick: dict[str, Any] | None, analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    address = str(predictive["wallet_id"])
    batch = batch or {}
    m = (quick or {}).get("metrics") or {}
    a = ((analysis or {}).get("wallet_metrics") or {}).get(PERIOD) or {}
    if not isinstance(batch, dict) or not isinstance(m, dict) or not isinstance(a, dict):
        raise TypeError("malformed HyperX metric schema")
    discrepancies: list[str] = []
    period_ok = all(str(x.get("period")) == PERIOD for x in (batch, quick or {}, a) if x)
    if not period_ok:
        discrepancies.append("PERIOD_MISMATCH")
    pnl_values = {
        "batch": finite(batch.get("total_realized_pnl")),
        "get": finite(m.get("total_realized_pnl")),
        "deep": finite(a.get("total_realized_pnl")),
    }
    trade_counts = {
        "batch": batch.get("total_complete_trade_count"),
        "get": m.get("total_complete_trade_count"),
        "deep": a.get("total_complete_trades_count"),
    }
    if any(v is None for v in pnl_values.values()):
        discrepancies.append("MISSING_PNL_ENDPOINT")
    elif max(float(v) for v in pnl_values.values() if v is not None) - min(float(v) for v in pnl_values.values() if v is not None) > ALLOWED_DIVERGENCE_USD:
        discrepancies.append("REALIZED_PNL_ENDPOINT_CONFLICT")
    if any(x is None for x in trade_counts.values()):
        discrepancies.append("MISSING_TRADE_COUNT_ENDPOINT")
    elif len({int(str(v)) for v in trade_counts.values() if v is not None}) > 1:
        discrepancies.append("TRADE_COUNT_ENDPOINT_CONFLICT")
    roi_batch = finite(batch.get("roi"))
    roi_analysis = finite(a.get("roi"))
    if roi_batch is not None and roi_analysis is not None and abs(roi_batch - roi_analysis) > 1e-3:
        discrepancies.append("ROI_ENDPOINT_CONFLICT")
    fee_markers = [x.get("is_contain_fee") for x in (batch, m, a)]
    if not all(x is True for x in fee_markers):
        discrepancies.append("FEE_INCLUSION_UNVERIFIED")
    mtm_coverage = str(a.get("mtm_coverage", batch.get("mtm_coverage", "missing")))
    if mtm_coverage.lower() not in {"full", "complete"}:
        discrepancies.append("MISSING_FULL_MTM_COVERAGE")
    transfer_count = batch.get("deposits_and_withdrawals_count")
    if transfer_count is None:
        transfer_count = a.get("deposits_and_withdrawals_count")
    if isinstance(transfer_count, int) and transfer_count > 0:
        discrepancies.append("TRANSFER_ADJUSTED_ROI_UNQUALIFIED")
    source_rank = predictive.get("previous_predictive_rank")
    wins = finite(batch.get("win_rate"))
    net_realized = pnl_values["deep"] if pnl_values["deep"] is not None else pnl_values["batch"]
    times = [int(x) for x in (batch.get("first_trade_time"), batch.get("last_trade_time")) if isinstance(x, (int, float)) and x > 0]
    coin_breakdown = batch.get("performance_by_coin") or m.get("performance_by_coin") or []
    if not isinstance(coin_breakdown, list):
        coin_breakdown = []
    coins = sorted(
        (x for x in coin_breakdown if isinstance(x, dict) and finite(x.get("total_realized_pnl")) is not None),
        key=lambda x: abs(finite(x.get("total_realized_pnl")) or 0), reverse=True,
    )
    abs_sum = sum(abs(finite(x.get("total_realized_pnl")) or 0) for x in coins)
    top_pnl_coin = coins[0].get("coin") if coins else None
    top_pnl_concentration = abs(finite(coins[0]["total_realized_pnl"]) or 0) / abs_sum if coins and abs_sum else None
    time_str = datetime.fromtimestamp(int(batch["last_trade_time"]) / 1000, UTC).isoformat() if batch.get("last_trade_time") else None
    trade_count = a.get("total_complete_trades_count") if a.get("total_complete_trades_count") is not None else batch.get("total_complete_trade_count")
    copy_net_label = finite(predictive.get("mean_wallet_net_10bps"))
    return {
        "previous_predictive_rank": source_rank,
        "wallet_id": address,
        "wallet_short": short(address),
        "previous_1h_incremental_vs_control_bps": finite(predictive.get("primary_equal_day_incremental_bps")),
        "previous_1h_wallet_net_copy_bps": copy_net_label,
        "previous_1h_episodes": int(predictive.get("episodes_1h") or 0),
        "previous_1h_utc_days": int(predictive.get("utc_days") or 0),
        "previous_1h_unverified_coverage": int(predictive.get("unverified_coverage_episodes") or 0),
        "previous_identical_signal_group": predictive.get("ex_post_identical_signal_group"),
        "hyperx_period": PERIOD,
        "hyperx_realized_pnl_90d_usd": net_realized,
        "batch_realized_pnl_usd": pnl_values["batch"],
        "get_realized_pnl_usd": pnl_values["get"],
        "deep_realized_pnl_usd": pnl_values["deep"],
        "reported_fee_usd": finite(a.get("total_fee") if a else batch.get("total_fee")),
        "reported_fee_included": all(x is True for x in fee_markers),
        "hyperx_funding_net_90d_usd": finite(a.get("funding_net") if a else batch.get("funding_net")),
        "reported_mtm_pnl_90d_usd": finite(a.get("total_pnl_mtm") if a else batch.get("total_pnl_mtm")),
        "mtm_coverage": mtm_coverage,
        "roi_batch_RAW_UNITS_UNVERIFIED": roi_batch,
        "roi_deep_RAW_UNITS_UNVERIFIED": roi_analysis,
        "roi_coverage": a.get("roi_coverage"),
        "max_drawdown_vendor_UNVERIFIED": finite(a.get("max_account_value_drawdown") if a else batch.get("max_account_value_drawdown")),
        "profit_path_max_drawdown_usd_VENDOR": finite(a.get("profit_max_drawdown")),
        "trades_90d": int(str(trade_count)) if trade_count is not None else None,
        "win_rate_90d": wins,
        "profit_factor_vendor": finite(a.get("profit_factor") if a else batch.get("profit_factor")),
        "pnl_per_volume_vendor": finite(batch.get("pnl_per_volume")),
        "volume_usd_vendor": finite(a.get("volume_usd") if a else batch.get("volume_usd")),
        "top_coin_by_abs_realized_pnl": top_pnl_coin,
        "top_coin_abs_pnl_concentration": top_pnl_concentration,
        "trade_time_start_utc": datetime.fromtimestamp(times[0] / 1000, UTC).isoformat() if times else None,
        "trade_time_last_utc": time_str,
        "hyperx_batch_updated_at_utc_naive": batch.get("updated_at"),
        "withdrawals_deposits_events_vendor": transfer_count,
        "risk_warnings": json.dumps(a.get("risk_warnings") or batch.get("risk_warnings") or [], ensure_ascii=False),
        "endpoint_consistency_status": "CONSISTENT_REALIZED_PNL_AND_COUNTS" if not any(x in discrepancies for x in ("REALIZED_PNL_ENDPOINT_CONFLICT", "MISSING_PNL_ENDPOINT", "TRADE_COUNT_ENDPOINT_CONFLICT", "MISSING_TRADE_COUNT_ENDPOINT")) else "CONFLICT_OR_MISSING",
        "roi_verification": "UNVERIFIED_VENDOR_DENOMINATOR",
        "account_trading_return_verification": "NOT_VERIFIED_MTM_AND_CASH_FLOWS",
        "discrepancies": ";".join(discrepancies),
    }


def report(rows: list[dict[str, Any]], as_of: datetime, issues: dict[str, Any], directory: Path) -> str:
    positive = sum((x["hyperx_realized_pnl_90d_usd"] or 0) > 0 for x in rows)
    zero = sum(x["hyperx_realized_pnl_90d_usd"] is None for x in rows)
    mismatched = sum("ROI_ENDPOINT_CONFLICT" in x["discrepancies"] for x in rows)
    missing_mtm = sum(x["mtm_coverage"].lower() != "full" for x in rows)
    lines = [
        "# HyperX 90-day wallet verification — historical predictive Top 10",
        "",
        f"Verified against live read-only HyperX Data API on {as_of.isoformat()}. Provider period **90d**, approximately {(as_of - timedelta(days=90)).date()} through {as_of.date()} UTC. A rolling 90d interval differs slightly from exact three calendar months.",
        "",
        "## Qualification boundary",
        "",
        f"- **{len(rows)}** requested wallets, {positive} with positive provider-reported 90d realized PnL, {zero} missing PnL.",
        f"- **{mismatched}** wallets with an ROI discrepancy between HyperX's batch and deep analysis endpoints.",
        f"- **{missing_mtm}** wallets without verified complete beginning/end unrealized-PnL changes. Reported realized PnL is NOT full economic PnL.",
        "- These are three routes from one data provider, not an independent on-chain audit. Fee inclusion and trade counts were cross-checked between its routes. ROI denominator, cash transfers, account-value changes and funding inclusion need independent validation.",
        "- This historical 90d period overlaps the earlier wallet selection and prospective testing. It is NOT a new untouched validation of predictability.",
        "",
        "## Wallet performance and endpoint reconciliation",
        "",
        "| Prior predictive rank | Wallet | Realized PnL 90d | Closed trades | Win rate | Fee (reported) | MTM coverage |",
        "|---:|---|---:|---:|---:|---:|---|",
    ]
    for item in sorted(rows, key=lambda x: x["previous_predictive_rank"]):
        p = item["hyperx_realized_pnl_90d_usd"]
        lines.append(f"| {item['previous_predictive_rank']} | `{item['wallet_short']}` | {p:+,.2f} | {item['trades_90d']} | {100*item['win_rate_90d']:.1f}% | {item['reported_fee_usd']:,.2f} | {item['mtm_coverage']} |" if p is not None and item["win_rate_90d"] is not None and item["reported_fee_usd"] is not None else f"| {item['previous_predictive_rank']} | `{item['wallet_short']}` | NA | NA | NA | NA | {item['mtm_coverage']} |")
    lines += ["", "## Highest 90d realized PnL (not copyable alpha)", "",
              "| Wallet | 90d realized PnL | Prior 1h predictive rank |",
              "|---|---:|---:|"]
    for item in sorted(rows, key=lambda x: x["hyperx_realized_pnl_90d_usd"] if x["hyperx_realized_pnl_90d_usd"] is not None else -math.inf, reverse=True):
        p = item["hyperx_realized_pnl_90d_usd"]
        lines.append(f"| `{item['wallet_short']}` | {p:+,.2f} | {item['previous_predictive_rank']} |" if p is not None else f"| `{item['wallet_short']}` | NA | {item['previous_predictive_rank']} |")
    lines += ["", "## Per-wallet quality exclusions", ""]
    for item in sorted(rows, key=lambda x: x["previous_predictive_rank"]):
        lines.append(f"- `{item['wallet_short']}`: {item['discrepancies'] or 'NO_PROVIDER_DISCREPANCIES'}; last trade {item['trade_time_last_utc']}; prior 1h coverage-unverified episodes {item['previous_1h_unverified_coverage']}/{item['previous_1h_episodes']}.")
    lines += ["", "## Interpretation", "",
              "An observed profitable wallet can have poor net follow returns when entry notifications lag, prices are minute-sampled or the wallet trades at an uncopyable frequency. Prior 1h predictive scores measure incremental markouts versus a same-coin momentum control, NOT wallet trading profitability. High win rates alone do not prove economic edge; large realized gains can come from rare large positions.",
              "",
              "HyperX may report 'ROI' ratios using different implicit equity denominators; values that differ across API endpoints are intentionally exported RAW_UNITS_UNVERIFIED, not shown as annualized, cash-flow-adjusted returns. Account drawdown fields are vendor-computed and not qualified where curve coverage/flows are incomplete.",
              "",
              f"Private raw source and cross-check exports: `{directory}`. API endpoint identities, raw response SHA-256, UTC collection timestamps and rate weights are retained in manifest. No credentials are stored.",
              "",
              "Final disposition: **HISTORICAL_REALIZED_PNL_PROVIDER_VERIFIED_ONLY / PREDICTIVE_COPY_SKILL_INCONCLUSIVE**.",
              ""]
    return "\n".join(lines)


def execute(roster: Path, output_root: Path, n: int = MAX_CANDIDATES) -> dict[str, Any]:
    as_of = datetime.now(UTC)
    candidates = read_candidates(roster, n=n)
    ids = [str(x["wallet_id"]) for x in candidates]
    client = ApiClient()
    destination = output_root / ("hyperx-90d-" + as_of.strftime("%Y%m%dT%H%M%SZ"))
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    destination.chmod(0o700)
    print(f"HyperX 90d verification: {n} candidates; auth present={client.auth_configured}; private={destination}", flush=True)
    batch_resp = client.request("/wallet_metrics_query", method="POST", payload={
        "user_address_list": ids, "period": PERIOD, "page": 1, "page_size": 20, "slim": False
    }, weight=2)
    batch_items = batch_resp.get("results")
    if not isinstance(batch_items, list):
        raise TypeError("HyperX batch response missing results")
    if len(batch_items) != n or int(batch_resp.get("total", -1)) != n:
        raise ValueError(f"HyperX returned incomplete batch: {len(batch_items)} / {n}")
    batch_by_wallet = {str(item.get("user_address", "")).lower(): item for item in batch_items if isinstance(item, dict)}
    if set(batch_by_wallet) != set(ids):
        raise ValueError("unexpected HyperX returned wallet IDs")
    private_write(destination / "hyperx_batch_90d_raw.json", json.dumps(batch_resp, ensure_ascii=False, indent=2) + "\n")
    print("HyperX batch: all 10 received; running independent-route internal crosschecks", flush=True)
    all_raw: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for i, prior in enumerate(candidates, 1):
        address = str(prior["wallet_id"])
        q = client.request(f"/wallet_metrics/{address}?period={PERIOD}", weight=2)
        analysis = client.request(f"/wallet_analysis/{address}", method="POST", payload={
            "periods": [PERIOD], "force_flush": False, "save_in_db": False,
            "load_fills_use_db_cache": True, "with_trade_details": False, "minimal": True
        }, weight=5)
        all_raw[address] = {"get_wallet_metrics": q, "deep_wallet_analysis": analysis}
        result = verify_three_views(prior, batch_by_wallet[address], q, analysis)
        rows.append(result)
        flagged = result["discrepancies"] or "NO_FLAGS"
        print(f"verified {i}/{n}: {short(address)} realized={result['hyperx_realized_pnl_90d_usd']} trades={result['trades_90d']} issues={flagged}", flush=True)
        # Checkpoint after every wallet, private and permission restricted.
        private_write(destination / "hyperx_crosschecks_raw.json", json.dumps(all_raw, indent=2, ensure_ascii=False) + "\n")
        write_csv(destination / "wallet_90d_verified.csv", rows)
    issues = {
        "addresses_requested": n,
        "all_batch_matched": True,
        "cross_endpoint_consistency_ok": sum(x["endpoint_consistency_status"] == "CONSISTENT_REALIZED_PNL_AND_COUNTS" for x in rows),
        "positive_realized_pnl": sum((x["hyperx_realized_pnl_90d_usd"] or 0) > 0 for x in rows),
        "roi_endpoint_discrepancies": sum("ROI_ENDPOINT_CONFLICT" in x["discrepancies"] for x in rows),
        "mtm_incomplete": sum(x["mtm_coverage"].lower() != "full" for x in rows),
    }
    private_write(destination / "report.md", report(rows, as_of, issues, destination))
    private_write(destination / "manifest.json", json.dumps({
        "as_of_utc": as_of.isoformat(),
        "three_month_definition": "HyperX rolling 90d, not exact three calendar months",
        "provider": BASE, "read_only": True, "n_wallets": n,
        "authentication_supplied": client.auth_configured,
        "request_records": client.request_records,
        "rate_weight_used": client.weight_used,
        "source_historical_predictive_leaderboard": str(roster),
        "wallet_list_not_committed": True,
        "issues": issues,
        "source_provider_not_independent_chain_verification": True,
    }, indent=2) + "\n")
    print(json.dumps({
        "directory": str(destination), "issues": issues,
        "results": [{"wallet": x["wallet_short"], "pnl": x["hyperx_realized_pnl_90d_usd"],
                    "trades": x["trades_90d"], "win_rate": x["win_rate_90d"],
                    "flags": x["discrepancies"]} for x in rows]
    }, indent=2), flush=True)
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--n", type=int, default=MAX_CANDIDATES)
    args = parser.parse_args()
    execute(args.roster, args.out_root, args.n)


if __name__ == "__main__":
    main()
