"""PM-02 public research contract: all tests use synthetic PM-01/forward fixtures."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from ensemble.pm_manager import digest, rank

ASOF = datetime(2026, 10, 31, tzinfo=UTC)
ALIASES = ("A", "B")


def replay():
    return {"schema": "pm01_fill_only_replay_v2", "wallet_count": 36,
            "never_claim_prospective_alpha": True,
            "rows": [{"wallet": a,"cohort":"original","latency_minutes":lag,
                      "oracle_hindsight":True,"known_at":"UNKNOWN_VENDOR_HISTORICAL_ORACLE",
                      "status":"HISTORICAL_HINDSIGHT_SIMULATION_ONLY",
                      "pnl":"1000","matched_long_only_market_control_pnl":"1000",
                      "incremental_vs_matched_long_control":"0"}
                     for a in ALIASES for lag in (5,15,60)]}


def samples(wallet, follower=2.0, control=0.0, count=80, clone=False):
    begin=ASOF-timedelta(days=count)
    return [{"episode_id":f"shared-{i}" if clone else f"{wallet}-{i}",
             "event_at":(begin+timedelta(days=i)).isoformat(),
             "known_at":(begin+timedelta(days=i,minutes=1)).isoformat(),
             "outcome_known_at":(begin+timedelta(days=i,minutes=2)).isoformat(),
             "verified_first_known":True,"oracle_hindsight":False,
             "matched_control":True,"independent_source_digest":"sourcehash",
             "market_source_digest":"marketsha","follower_net_r":follower+(((i*7919)%97)-48)/100,
             "control_net_r":control,"tail_loss_r":0.1,"turnover_r":0.2}
            for i in range(count)]


def test_historical_profit_never_qualifies():
    r=rank(replay(),ALIASES,ASOF.isoformat())
    assert r["qualified_count"]==0
    assert [s["state"] for s in r["five_seats"]]==["EMPTY"]*5
    assert all(c["prospective"]["incremental_lcb_r"] is None for c in r["cards"])
    assert digest({k:v for k,v in r.items() if k!="sha256"})==r["sha256"]


def test_follower_loses_despite_profitable_historical_source():
    r=rank(replay(),ALIASES,ASOF.isoformat(),{"A":samples("A",-1)})
    assert r["cards"][0]["prospective"]["qualified"] is False
    assert "INCREMENTAL_ALPHA_NOT_CONFIRMED" in r["cards"][0]["prospective"]["reasons"]


def test_market_only_explains_all_follower_profit():
    r=rank(replay(),ALIASES,ASOF.isoformat(),{"A":samples("A",2,2.15)})
    assert r["cards"][0]["prospective"]["qualified"] is False
    assert r["five_seats"][0]["state"]=="EMPTY"


def test_future_outcome_not_visible_and_deterministic():
    future=samples("A")
    future[0]["outcome_known_at"]=(ASOF+timedelta(days=3)).isoformat()
    a=rank(replay(),ALIASES,ASOF.isoformat(),{"A":future})
    b=rank(replay(),ALIASES,ASOF.isoformat(),{"A":list(reversed(future))})
    assert a==b
    assert a["cards"][0]["prospective"]["n"]==79


def test_conflicting_revision_rejected():
    rs=samples("A")
    changed=deepcopy(rs[0])
    changed["follower_net_r"]=50.0
    with pytest.raises(ValueError,match="CONFLICTING_EVIDENCE_REVISION"):
        rank(replay(),ALIASES,ASOF.isoformat(),{"A":rs+[changed]})


def test_invalid_clock_order_is_rejected():
    rs=samples("A")
    rs[0]["known_at"]=(ASOF-timedelta(days=90)).isoformat()
    with pytest.raises(ValueError,match="INVALID_EVIDENCE_CLOCK_ORDER"):
        rank(replay(),ALIASES,ASOF.isoformat(),{"A":rs})


def test_stale_and_insufficient_samples():
    older=samples("A",count=9)
    for row in older:
        for key in ("event_at", "known_at", "outcome_known_at"):
            row[key] = (datetime.fromisoformat(row[key])-timedelta(days=5)).isoformat()
    r=rank(replay(),ALIASES,ASOF.isoformat(),{"A":older})
    reasons=r["cards"][0]["prospective"]["reasons"]
    assert "INSUFFICIENT_EPISODES" in reasons
    assert "STALE_PROSPECTIVE_EVIDENCE" in reasons


def test_clone_managers_consume_one_seat():
    x=samples("A",clone=True,count=200)
    y=deepcopy(x)
    r=rank(replay(),ALIASES,ASOF.isoformat(),{"A":x,"B":y})
    assert r["dependence_clusters"]==[["A","B"]]
    assert len([s for s in r["five_seats"] if s["wallet"]])==1


def test_unmatched_histories_fail_closed_on_dependence():
    r=rank(replay(),ALIASES,ASOF.isoformat(),{"A":samples("A"),"B":samples("B")})
    assert len([s for s in r["five_seats"] if s["wallet"]])<=1


def test_missing_market_and_source_conflicts_fail_closed():
    for field in ("missing_market","source_conflict"):
        rows=samples("A")
        rows[0][field]=True
        with pytest.raises(ValueError,match="CONFLICTING_OR_MISSING"):
            rank(replay(),ALIASES,ASOF.isoformat(),{"A":rows})


def test_oracle_fixtures_cannot_become_prospective():
    rows=samples("A")
    for r in rows:
        r["oracle_hindsight"]=True
    r=rank(replay(),ALIASES,ASOF.isoformat(),{"A":rows})
    assert r["qualified_count"]==0
