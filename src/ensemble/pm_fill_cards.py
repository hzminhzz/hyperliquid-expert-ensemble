"""PM-01 reproducible fill-only wallet evidence cards and qualification gates.

These cards describe the vendor-listed observed portfolio, not an inferred
whole exchange account or any qualified prospective copying strategy.
No scores, ranks, or allocation approvals are produced from hindsight data.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .pm_sources import CohortEntry

LATENCIES = (5, 15, 60)
REQUIRED_EXAMPLES = ("CA-005", "W042", "VS066")
HISTORICAL = "HISTORICAL_HINDSIGHT_SIMULATION_ONLY"


def build_fill_evidence_cards(
    entries: Iterable[CohortEntry],
    rows: list[dict[str, Any]],
    *,
    risk_budget: str,
    study_start: str,
    cutoff: str,
) -> dict[str, Any]:
    """Exactly 36 source identities and exactly three mutually distinct cuts.

    An actual economic follower can be deemed prospective only with a true
    source first-known clock, unbroken source history, reconciled account
    state and independently observed follower execution. Mere scenario P&L
    from a backfilled wallet can never satisfy those gates.
    """
    cohort = tuple(entries)
    if len(cohort) != 36 or len({x.alias for x in cohort}) != 36:
        raise ValueError("PM01_MISSING_OR_DUPLICATED_WALLET_IDENTITIES")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("wallet") not in {entry.alias for entry in cohort}:
            raise ValueError("PM01_UNKNOWN_SOURCE_WALLET")
        grouped[row["wallet"]].append(row)
    if len(rows) != 108 or len(grouped) != 36:
        raise ValueError("PM01_MISSING_SCENARIOS")
    cards = []
    for entry in cohort:
        outcomes = sorted(grouped[entry.alias], key=lambda r: r["latency_minutes"])
        if tuple(r["latency_minutes"] for r in outcomes) != LATENCIES:
            raise ValueError("PM01_NONDETERMINISTIC_OR_MISSING_LATENCY_CASES")
        if any(r["cohort"] != entry.cohort for r in outcomes):
            raise ValueError("PM01_COHORT_IDENTITY_DRIFT")
        historical = []
        prospective = []
        violations = []
        for row in outcomes:
            status = row["status"]
            success = status == HISTORICAL
            if status == "BLOCKED_SOURCE_INCOMPLETE" and row.get("pnl") is not None:
                raise ValueError("PM01_BLOCKED_PNL_IS_NON_NULL")
            if not success and row.get("pnl") is not None:
                raise ValueError("PM01_UNVERIFIED_SCENARIO_HAS_PNL")
            if success and (
                row.get("pnl") is None or row.get("matched_long_only_market_control_pnl") is None
            ):
                raise ValueError("PM01_MISSING_MARKET_CONTROL_FOR_HISTORICAL_RESULT")
            prospective_observation = (
                row.get("oracle_hindsight") is False
                and isinstance(row.get("first_known_at"), str)
                and row.get("first_known_at") != "UNKNOWN"
                and row.get("source_equity_accounting_reconciled") is True
                and isinstance(row.get("actual_follower_fills"), int)
                and row["actual_follower_fills"] > 0
                and success
            )
            prospective.append(prospective_observation)
            if row.get("oracle_hindsight") is not True:
                violations.append("UNEXPECTED_NON_ORACLE_INPUT_NEEDS_SEPARATE_PIT_AUDIT")
            historical.append(
                {
                    "latency_minutes": row["latency_minutes"],
                    "evidence_type": "HISTORICAL_ORACLE_HYPOTHETICAL",
                    "status": status,
                    "blocked_reason": None if success else row.get("reason", status),
                    "blocked_instrument": row.get("instrument") if not success else None,
                    "modeled_follower_pnl": row["pnl"] if success else None,
                    "matched_market_only_pnl": (
                        row["matched_long_only_market_control_pnl"] if success else None
                    ),
                    "incremental_vs_market_only": (
                        row.get("incremental_vs_matched_long_control") if success else None
                    ),
                    "hypothetical_fill_count": row.get("hypothetical_fill_count")
                    if success
                    else None,
                    "sample_window_hours": row.get("replay_window_hours"),
                }
            )
        # A wallet is never "certified" solely by retrospective scenarios.
        has_qualified_forward_evidence = all(prospective) and not violations
        reasons = []
        if not has_qualified_forward_evidence:
            reasons.append("NO_INDEPENDENT_PROSPECTIVE_FOLLOWER_EVIDENCE")
        if outcomes[0].get("known_at", "").startswith("UNKNOWN"):
            reasons.append("NO_HISTORICAL_FIRST_KNOWN_FILL_CLOCK")
        if outcomes[0].get("scope", "").startswith("FROZEN_VENDOR_REPORTED"):
            reasons.append("VENDOR_INSTRUMENT_ROSTER_NOT_WHOLE_EXCHANGE_ACCOUNT")
        if any(r.get("blocked_reasons") for r in outcomes):
            reasons.append("SOURCE_POSITION_OR_PAGINATION_GAPS")
        if any(r["status"] != HISTORICAL for r in outcomes):
            reasons.append("UNEXECUTABLE_HISTORICAL_MARKET_OR_SOURCE_SCENARIO")
        cards.append(
            {
                "alias": entry.alias,
                "cohort": entry.cohort,
                "identity_sha256": entry.address_sha256,
                "source_fills": outcomes[0].get("source_fills"),
                "source_instrument_count": outcomes[0].get("instruments"),
                "historical_first_known_at": None,
                "account_equity_reconciled": all(
                    r.get("source_equity_accounting_reconciled") is True for r in outcomes
                ),
                "source_coverage": "VENDOR_LISTED_HISTORICAL_ONLY",
                "study_start_utc": study_start,
                "frozen_cutoff_utc": cutoff,
                "risk_budget_fraction": risk_budget,
                "latency_scenarios": historical,
                "all_delay_scenarios_historical_only": all(
                    r.get("oracle_hindsight") is True for r in outcomes
                ),
                "prospective_follower_qualified": has_qualified_forward_evidence,
                "research_allocation_status": (
                    "REQUIRES_PIT_AND_EXECUTION_VERIFICATION"
                    if not has_qualified_forward_evidence
                    else "FORWARD_EVIDENCE_PRESENT_NEEDS_SEPARATE_APPROVAL"
                ),
                "qualification_blockers": reasons,
            }
        )
    names = {x["alias"] for x in cards}
    if not set(REQUIRED_EXAMPLES).issubset(names):
        raise ValueError("PM01_EXAMPLE_WALLET_NOT_IN_FROZEN_36")
    return {
        "schema": "pm01_fill_only_36_evidence_cards_v1",
        "wallet_count": len(cards),
        "example_wallets": {
            name: next(card for card in cards if card["alias"] == name)
            for name in REQUIRED_EXAMPLES
        },
        "qualified_prospective_wallets": sum(
            bool(card["prospective_follower_qualified"]) for card in cards
        ),
        "cards": cards,
    }
