"""Private, read-only PM-01 cohort source reconciliation.

Public source code contains aliases only. Private address maps are loaded at
runtime and never serialized into results, logs or Git artifacts. HyperX vendor
roster completeness is explicitly not whole-account or prospective coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import json
import re

from .pm_portfolio import Coverage, WalletSource, validate_36_wallet_cohort


ORIGINAL_SIX = ("W044", "W048", "W023", "W031", "W015", "W026")
WAVE_ONE = ("W035", "W045", "IX-003", "EQ-001", "W032", "W043", "W030", "W047", "CM-005", "CO-001")
ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")
CACHE = "slow_pod_data_v1/hyperx_fills_cache_v1/derived/current_cache_manifest.json"


@dataclass(frozen=True)
class CohortEntry:
    alias: str
    cohort: str
    address: str  # PRIVATE. Never emit as a report field.
    address_sha256: str
    historical_state: str
    vendor_page_count: int
    vendor_fill_count: int

    @property
    def wallet(self) -> WalletSource:
        return WalletSource(self.alias, self.cohort, Coverage.HISTORICAL_ONLY)


def _load(root: Path, relative: str) -> dict:
    data = json.loads((root / relative).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected source object: {relative}")
    return data


def _private_map(root: Path, relative: str) -> dict[str, str]:
    path = root / relative
    if path.stat().st_mode & 0o077:
        raise ValueError(f"private source permissions too broad: {relative}")
    raw = _load(root, relative)
    mapping: dict[str, str] = {}
    for alias, address in raw.items():
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise ValueError(f"invalid private address for alias {alias}")
        mapping[alias] = address.lower()
    return mapping


def _resolved_identity(root: Path) -> dict[str, str]:
    """Match the original extended HyperX adapter's canonical collision policy."""
    baseline = _private_map(
        root, "ml4t_100_wallet_20_market_benchmark_v1/PRIVATE_alias_mapping.json"
    )
    atlas = _private_map(root, "multi_instrument_atlas_v1/PRIVATE_wallet_alias_mapping.json")
    seen = {v: k for k, v in baseline.items()}
    mapping = dict(baseline)
    for alias, addr in atlas.items():
        if addr not in seen:
            mapping[alias] = addr
            seen[addr] = alias
    for name in (
        "PRIVATE_VENDOR_SCREEN_ALIAS_MAPPING_20261030.json",
        "PRIVATE_NANSEN_HYPERX_UNSCREENED_ALIAS_MAPPING_20261030.json",
    ):
        for alias, addr in _private_map(root, name).items():
            if alias in mapping and mapping[alias] != addr:
                raise ValueError(f"conflicting address identity for {alias}")
            if addr in seen and seen[addr] != alias:
                continue
            mapping[alias] = addr
            seen[addr] = alias
    return mapping


def load_cohort(root: Path) -> tuple[tuple[CohortEntry, ...], dict[str, str]]:
    """Require exact 6+10+20 identities, strong immutable source references."""
    root = root.resolve()
    sources = {
        "wave1": "WAVE_A_INTEGRITY_RESULT_20261030.json",
        "wave2": "WAVE_B_20_TARGETS_20261030.json",
        "wave2_worker": "wave_b_20_acquisition_20261030/status.json",
        "wave2_study": "slow_pod_data_v1/wave_b_five_month_v1/"
        "final20_frozen_1m_replay_v1/current_final20_frozen_study.json",
        "wave2_integrity": "slow_pod_data_v1/wave_b_five_month_v1/"
        "final20_source/final20_independent_integrity-f6d91495da6c9164c4232d.json",
        "w019_new_lit_generation": "slow_pod_data_v1/wave_b_five_month_v1/"
        "w019_lit_new_generation/generation-20261030T120153628646Z-1551b1a/"
        "generation_manifest.json",
        "w019_new_lit_replay": "slow_pod_data_v1/wave_b_five_month_v1/"
        "final20_frozen_1m_replay_v1/w019_extended_validated/"
        "w019_five_month_source_verified_replay-46a930fa069db831c7daa1.json",
        "original": CACHE,
    }
    files = {key: _load(root, name) for key, name in sources.items()}
    fingerprints = {
        key: sha256((root / path).read_bytes()).hexdigest() for key, path in sources.items()
    }
    wa = files["wave1"]
    wb = files["wave2"]
    worker = files["wave2_worker"]
    study = files["wave2_study"]
    integrity = files["wave2_integrity"]
    original = files["original"]
    new_lit = files["w019_new_lit_generation"]
    lit_replay = files["w019_new_lit_replay"]
    if new_lit.get("schema") != "wave_b_w019_lit_fresh_pagination_generation_v1":
        raise ValueError("W019 vendor addendum schema changed")
    if (
        new_lit.get("wallet_alias") != "W019"
        or new_lit.get("coin") != "LIT"
        or new_lit.get("status") != "VENDOR_PAGINATION_EXHAUSTED"
        or new_lit.get("new_generation_pages_persisted") != 3
        or new_lit.get("cross_generation_original_TID_payload_mismatches") != 0
        or new_lit.get("cross_generation_original_TIDs_matched") != 4000
        or new_lit.get("vendor_pages_chronological_and_TIDs_unique") is not True
        or new_lit.get("research_first_known_at") != "UNKNOWN"
    ):
        raise ValueError("W019 fresh LIT source not independently verified")
    if (
        lit_replay.get("LIT_vendor_fully_exhausted_at_June02") is not True
        or lit_replay.get("true_five_month_source_first_known_at") != "UNKNOWN"
        or lit_replay.get("source_new_generation_manifest_sha256")
        != fingerprints["w019_new_lit_generation"]
    ):
        raise ValueError("W019 vendor five-month addendum source lineage mismatch")
    if wb.get("count") != 20 or not isinstance(wb.get("targets"), list):
        raise ValueError("Wave 2 source contract not exactly 20")
    wave2 = tuple(x["alias"] for x in wb["targets"])
    if len(wave2) != 20 or len(set(wave2)) != 20:
        raise ValueError("Wave 2 duplicate/omitted aliases")
    if [row.get("alias") for row in wa.get("wallets", [])] != list(WAVE_ONE):
        raise ValueError("Wave 1 roster drift")
    if worker.get("state") != "WAVE_B_VENDOR_ROSTER_COVERED":
        raise ValueError("Wave 2 worker not terminal")
    if set(worker.get("completed_aliases", [])) != set(wave2):
        raise ValueError("Wave 2 completion mismatch")
    if study.get("excluded_source_alias") != "W019" or study.get("admissible_source_wallets") != 19:
        raise ValueError("Wave 2 five-month exclusions changed: re-audit required")
    if integrity.get("wallet_count_acquired") != 20 or len(integrity.get("wallets", [])) != 20:
        raise ValueError("Wave 2 independent source integrity missing")
    integrity_index = {row["alias"]: row for row in integrity["wallets"]}
    if set(integrity_index) != set(wave2):
        raise ValueError("Wave 2 source report aliases drifted")
    if any(not integrity_index[a].get("vendor_fully_paginated") for a in wave2):
        raise ValueError("Wave 2 vendor pagination incomplete")
    if original.get("schema") != "hyperx_current_cache_v1":
        raise ValueError("original source manifest schema changed")
    if not all(original["wallets"].get(a, {}).get("source_roster_complete") for a in ORIGINAL_SIX):
        raise ValueError("one of the original six is missing the vendor roster")
    wa_index = {row["alias"]: row for row in wa["wallets"]}
    addresses = _resolved_identity(root)
    entries: list[CohortEntry] = []
    for cohort, aliases in (("original", ORIGINAL_SIX), ("wave1", WAVE_ONE), ("wave2", wave2)):
        for alias in aliases:
            address = addresses.get(alias)
            if address is None:
                raise ValueError(f"missing authoritative identity for {alias}")
            if cohort == "original":
                row = original["wallets"][alias]
                if not SHA.fullmatch(str(row.get("sha256", ""))):
                    raise ValueError(f"invalid original source fingerprint: {alias}")
                pages = len(row["source_page_shas"])
                count = int(row["asof_cutoff_source_rows"])
                state = "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY"
            elif cohort == "wave1":
                row = wa_index[alias]
                if not row.get("source_sha_verified") or not row.get("hindsight_only"):
                    raise ValueError(f"unverified Wave 1 source: {alias}")
                if row["status_counts"] != {"ALL_VENDOR_AVAILABLE": row["expected_coin_count"]}:
                    raise ValueError(f"Wave 1 incomplete instrument roster: {alias}")
                pages = int(row["cached_page_count"])
                count = int(row["raw_fill_rows_all_times"])
                state = "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY"
            else:
                progress = worker.get("wallet_progress", {}).get(alias, {})
                row = integrity_index[alias]
                pages = int(row["pages"])
                count = int(row["source_rows_raw"])
                if progress.get("pages_cached", 0) < pages:
                    raise ValueError(f"Wave 2 source page count inconsistency: {alias}")
                state = (
                    "FRESH_LIT_VENDOR_PAGINATION_EXHAUSTED_HISTORICAL_ONLY"
                    if alias == "W019"
                    else "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY"
                )
            entries.append(
                CohortEntry(
                    alias,
                    cohort,
                    address,
                    sha256(address.encode("ascii")).hexdigest(),
                    state,
                    pages,
                    count,
                )
            )
    validate_36_wallet_cohort(entry.wallet for entry in entries)
    if len({e.address for e in entries}) != 36:
        raise ValueError("alias collision: 36 distinct address identities required")
    return tuple(entries), fingerprints
