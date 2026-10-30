"""PM-02 read-only, fail-closed CIO evidence and manager allocation.

Prospective qualification requires independent first-known follower observations;
PM-01 historical vendor fills are never upgraded by a simulated lag.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite, sqrt
from pathlib import Path
from statistics import mean, pstdev

SCHEMA = "pm02_manager_governance_v1"
# Research policy, deliberately conservative and fixed before forward evidence.
MIN_EPISODES = 60
MIN_EFFECTIVE_EPISODES = 40
MIN_DAYS = 30
MAX_STALENESS_HOURS = 24
MIN_INCREMENTAL_LCB = 0.0
ALPHA_FAMILY = 0.05
MAX_SEATS = 5
TOTAL_R = 1.0
RESERVE_R = 0.5
MAX_MANAGER_R = 0.20
CLONE_CORR = 0.85
MAX_TAIL_SHARED = 0.65
MAX_DRAWDOWN_R = 2.0
MAX_DRIFT_DROP_R = 0.50


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def timestamp(value):
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("NAIVE_KNOWLEDGE_TIMESTAMP")
    return dt.astimezone(UTC)


def source_rows(replay, aliases):
    if replay.get("schema") != "pm01_fill_only_replay_v2" or replay.get("wallet_count") != 36:
        raise ValueError("UNSUPPORTED_PM01_REPLAY")
    if replay.get("never_claim_prospective_alpha") is not True:
        raise ValueError("PM01_PROVENANCE_MISSING")
    grouped = {alias: [] for alias in aliases}
    for row in replay["rows"]:
        alias = row["wallet"]
        if alias not in grouped or row.get("oracle_hindsight") is not True:
            raise ValueError("UNEXPECTED_OR_UNGUARDED_PM01_ROW")
        if row.get("known_at") != "UNKNOWN_VENDOR_HISTORICAL_ORACLE":
            raise ValueError("PM01_KNOWLEDGE_CLAIM_CHANGED")
        grouped[alias].append(row)
    if any(len(rows) != 3 or {r["latency_minutes"] for r in rows} != {5, 15, 60}
           for rows in grouped.values()):
        raise ValueError("INCOMPLETE_PM01_SCENARIOS")
    return grouped


def effective_n(values):
    """Conservative positive serial-dependence Bartlett estimate."""
    n = len(values)
    if n < 3:
        return float(n)
    mu = mean(values)
    den = sum((v - mu) ** 2 for v in values)
    if den <= 0:
        return 1.0
    inflation = 1.0
    for k in range(1, min(10, n - 1) + 1):
        rho = sum((values[i] - mu) * (values[i-k] - mu)
                  for i in range(k, n)) / den
        inflation += 2 * max(0.0, rho) * (1 - k/n)
    return max(1.0, min(float(n), n / inflation))


def corr(a, b):
    if len(a) != len(b) or len(a) < 3:
        return None
    x, y = mean(a), mean(b)
    sx = sum((z-x)**2 for z in a)
    sy = sum((z-y)**2 for z in b)
    if sx <= 0 or sy <= 0:
        return None
    return sum((u-x)*(v-y) for u,v in zip(a,b)) / sqrt(sx*sy)


def _prospective_evidence(rows, asof):
    """Only immutable, time-observable, matched episodic evidence is eligible.

    Each record is net follower and same-path matched control economics in R;
    knowledge and outcome maturity times are separate and mandatory.
    """
    seen = {}
    for r in rows:
        key = str(r["episode_id"])
        event = timestamp(r["event_at"])
        known = timestamp(r["known_at"])
        matured = timestamp(r["outcome_known_at"])
        if known < event or matured < known:
            raise ValueError("INVALID_EVIDENCE_CLOCK_ORDER")
        # Never take a future observation, including a future revision.
        if known > asof or matured > asof:
            continue
        if not r.get("verified_first_known") or r.get("oracle_hindsight") or not r.get("matched_control"):
            continue
        if not r.get("independent_source_digest") or not r.get("market_source_digest"):
            continue
        if r.get("source_conflict") or r.get("missing_market"):
            raise ValueError("CONFLICTING_OR_MISSING_PROSPECTIVE_EVIDENCE")
        if key in seen:
            if canonical(seen[key]) != canonical(r):
                raise ValueError("CONFLICTING_EVIDENCE_REVISION")
            raise ValueError("DUPLICATE_EVIDENCE_EPISODE")
        for field in ("follower_net_r", "control_net_r", "tail_loss_r", "turnover_r"):
            if not isfinite(float(r[field])):
                raise ValueError("NONFINITE_ECONOMICS")
        if float(r["tail_loss_r"]) < 0 or float(r["turnover_r"]) < 0:
            raise ValueError("INVALID_COST_RISK")
        seen[key] = r
    return sorted(seen.values(), key=lambda r: (r["event_at"],r["episode_id"]))


def _score(rows, asof, tests):
    if not rows:
        return {"qualified": False, "reasons": ["NO_VERIFIED_PROSPECTIVE_EPISODES"],
                "n": 0, "effective_n": 0, "incremental_lcb_r": None}
    values = [float(r["follower_net_r"])-float(r["control_net_r"]) for r in rows]
    followers = [float(r["follower_net_r"]) for r in rows]
    neff = effective_n(values)
    std = pstdev(values)
    mu = mean(values)
    # Bonferroni familywise two-sided conservative bound (normal tail).
    # 3.5 sigma is conservative for >=36 hypotheses at alpha=0.05.
    z = 3.5
    lcb = mu * neff/(neff+40) - z*std/sqrt(neff)
    recent = timestamp(rows[-1]["outcome_known_at"])
    distinct_days = len({timestamp(r["event_at"]).date() for r in rows})
    peak = 0.0
    equity = 0.0
    maxdd = 0.0
    for v in followers:
        equity += v
        peak = max(peak,equity)
        maxdd = max(maxdd,peak-equity)
    stale = asof-recent > timedelta(hours=MAX_STALENESS_HOURS)
    drift = len(values)>=20 and mean(values[-10:]) < mean(values[:10])-MAX_DRIFT_DROP_R
    reasons = []
    if len(rows) < MIN_EPISODES: reasons.append("INSUFFICIENT_EPISODES")
    if neff < MIN_EFFECTIVE_EPISODES: reasons.append("INSUFFICIENT_INDEPENDENT_SAMPLE")
    if distinct_days < MIN_DAYS: reasons.append("INSUFFICIENT_CALENDAR_DAYS")
    if stale: reasons.append("STALE_PROSPECTIVE_EVIDENCE")
    if drift: reasons.append("ADVERSE_REGIME_DRIFT")
    if maxdd > MAX_DRAWDOWN_R: reasons.append("DRAWDOWN_LIMIT")
    if lcb <= MIN_INCREMENTAL_LCB: reasons.append("INCREMENTAL_ALPHA_NOT_CONFIRMED")
    return {"qualified": not reasons, "reasons": reasons, "n": len(rows),
            "effective_n": round(neff,6), "calendar_days": distinct_days,
            "incremental_mean_r": round(mu,8), "incremental_lcb_r": round(lcb,8),
            "follower_total_r": round(sum(followers),8),
            "matched_control_total_r": round(sum(float(r["control_net_r"]) for r in rows),8),
            "turnover_r": round(sum(float(r["turnover_r"]) for r in rows),8),
            "worst_episode_r": round(min(followers),8), "max_drawdown_r": round(maxdd,8),
            "stale": stale, "drift": drift}


def rank(replay, aliases, asof, prospective=None, history=None):
    at=timestamp(asof)
    grouped=source_rows(replay,aliases)
    prospective=prospective or {}
    history=history or {}
    scores={}
    episodes={}
    for alias in sorted(aliases):
        actual=_prospective_evidence(prospective.get(alias,[]),at)
        episodes[alias]=actual
        score=_score(actual,at,len(aliases))
        historical=[]
        for r in sorted(grouped[alias],key=lambda r:r["latency_minutes"]):
            pnl=r.get("pnl")
            historical.append({
                "latency_minutes":r["latency_minutes"],"status":r["status"],
                "historical_hypothetical_net_pnl":pnl,
                "historical_market_control_pnl":r.get("matched_long_only_market_control_pnl"),
                "historical_incremental_pnl":r.get("incremental_vs_matched_long_control"),
                "costs":{k:r.get(k) for k in ("fees","spread_slippage","funding_assumed")},
                "turnover":r.get("turnover"),"drawdown":r.get("max_drawdown_sampled_at_actions"),
                "blockers":list(r.get("blocked_reasons",[]))+([r["reason"]] if r.get("reason") else []),
            })
        prior=history.get(alias,[])
        previous=prior[-1]["state"] if prior else "CANDIDATE"
        if prior and timestamp(prior[-1]["decided_at"])>=at:
            raise ValueError("NONMONOTONIC_DECISION_HISTORY")
        if score["qualified"]:
            state="SHADOW" if previous=="CANDIDATE" else (
                "INCUBATION" if previous=="SHADOW" else
                "ACTIVE" if previous=="INCUBATION" else previous)
        elif previous in ("ACTIVE","CORE","INCUBATION"):
            state="SUSPENDED" if score.get("stale") or score.get("drift") else "REDUCED"
        else:
            state="CANDIDATE" if previous=="CANDIDATE" else "SUSPENDED"
        scores[alias]={
            "wallet":alias,"evidence_level":"VERIFIED_PROSPECTIVE" if actual else "HISTORICAL_ONLY",
            "source_wallet_performance":{"pnl":"UNKNOWN","account_reconciled":False},
            "historical_follower":historical,"prospective":score,
            "lifecycle":{"previous_state":previous,"state":state,"decided_at":at.isoformat(),
                         "reasons":score["reasons"],"research_only":True},
        }
    qualified=[a for a in sorted(aliases) if scores[a]["prospective"]["qualified"]]
    # Pairwise dependence only from independently verified overlapping episode IDs.
    clusters=[]
    visited=set()
    dependent={}
    for a in qualified:
        if a in visited: continue
        component={a}
        frontier=[a]
        while frontier:
            u=frontier.pop()
            urows={x["episode_id"]:x for x in episodes[u]}
            for b in qualified:
                if b in component: continue
                brows={x["episode_id"]:x for x in episodes[b]}
                common=sorted(set(urows)&set(brows))
                if len(common)<MIN_EFFECTIVE_EPISODES: continue
                x=[float(urows[k]["follower_net_r"])-float(urows[k]["control_net_r"]) for k in common]
                y=[float(brows[k]["follower_net_r"])-float(brows[k]["control_net_r"]) for k in common]
                rho=corr(x,y)
                tailx={k for k in common if float(urows[k]["follower_net_r"])<0}
                taily={k for k in common if float(brows[k]["follower_net_r"])<0}
                tail_share=len(tailx&taily)/max(1,min(len(tailx),len(taily)))
                if rho is None or rho>=CLONE_CORR or tail_share>=MAX_TAIL_SHARED:
                    dependent[(min(u,b),max(u,b))]={"correlation":rho,"shared_tail":tail_share}
                    component.add(b);frontier.append(b)
        visited|=component
        clusters.append(sorted(component))
    # UNKNOWN intermanager dependence is not inferred as independent.
    if len(qualified)>1:
        for i,a in enumerate(qualified):
            for b in qualified[i+1:]:
                common={r["episode_id"] for r in episodes[a]} & {r["episode_id"] for r in episodes[b]}
                if len(common)<MIN_EFFECTIVE_EPISODES:
                    scores[b]["prospective"]["qualified"]=False
                    scores[b]["prospective"]["reasons"].append("UNRESOLVED_CROSS_MANAGER_DEPENDENCE")
    ranked=sorted((a for a in qualified if scores[a]["prospective"]["qualified"]),
                  key=lambda a:(-scores[a]["prospective"]["incremental_lcb_r"],a))
    seats=[]
    used_clusters=set()
    for a in ranked:
        cl=next((i for i,c in enumerate(clusters) if a in c),None)
        if cl in used_clusters: continue
        if len(seats)==MAX_SEATS:break
        used_clusters.add(cl)
        # Partial conservative R budget; risk remaining held centrally.
        seats.append({"wallet":a,"risk_budget_r":MAX_MANAGER_R,
                      "state":scores[a]["lifecycle"]["state"],"research_only":True})
    while len(seats)<MAX_SEATS:
        seats.append({"wallet":None,"risk_budget_r":0.0,"state":"EMPTY","research_only":True})
    used=sum(s["risk_budget_r"] for s in seats)
    report={"schema":SCHEMA,"asof":at.isoformat(),
            "pm01_source_sha256":digest(replay),
            "policy":{"min_episodes":MIN_EPISODES,"min_effective":MIN_EFFECTIVE_EPISODES,
                      "min_days":MIN_DAYS,"max_staleness_hours":MAX_STALENESS_HOURS,
                      "familywise_alpha":ALPHA_FAMILY,"z_conservative":3.5,
                      "clone_corr":CLONE_CORR,"shared_tail_cap":MAX_TAIL_SHARED,
                      "max_drawdown_r":MAX_DRAWDOWN_R,"drift_drop_r":MAX_DRIFT_DROP_R,
                      "max_manager_r":MAX_MANAGER_R,"risk_authority":"PM03_ACCOUNT_SUPERVISOR_ONLY"},
            "cards":[scores[a] for a in sorted(aliases)],
            "qualified_count":len([a for a in aliases if scores[a]["prospective"]["qualified"]]),
            "ranked_candidates":ranked,
            "dependence_clusters":clusters,"dependent_pairs":[{"a":a,"b":b,**v} for (a,b),v in sorted(dependent.items())],
            "five_seats":seats,"reserve_r":round(TOTAL_R-used,8),
            "comparators":{"equal_risk":"NOT_ESTIMABLE_WITHOUT_PROSPECTIVE_PAIRED_PATHS",
                           "shrunk_risk":"NOT_ESTIMABLE_WITHOUT_PROSPECTIVE_PAIRED_PATHS"},
            "live_trading_authorized":False,
            "falsification":["obtain genuine independent first-known signals",
                             "recover complete market and source scope",
                             "measure matched follower alpha after costs",
                             "test independent episodes and correlated tails",
                             "validate prop firm account rules separately"]}
    report["sha256"]=digest(report)
    return report


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--pm01",type=Path,required=True)
    p.add_argument("--asof",required=True)
    p.add_argument("--out",type=Path,required=True)
    args=p.parse_args()
    raw=args.pm01.read_bytes()
    if len(raw)>50_000_000: raise ValueError("OVERSIZED_PM01_ARTIFACT")
    replay=json.loads(raw)
    aliases=sorted({r["wallet"] for r in replay["rows"]})
    from .pm_portfolio import Coverage, WalletSource, validate_36_wallet_cohort
    # PM-01 already validated the authoritative 6/10/20 source cohort.
    counts={r["wallet"]:r["cohort"] for r in replay["rows"]}
    validate_36_wallet_cohort(WalletSource(a,counts[a],Coverage.HISTORICAL_ONLY) for a in aliases)
    report=rank(replay,aliases,args.asof)
    args.out.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest=args.out/("pm02-"+report["sha256"]+".json")
    if dest.exists():
        if dest.read_bytes()!=canonical(report):raise ValueError("HASH_COLLISION_OR_TAMPER")
    else:
        dest.write_bytes(canonical(report))
        dest.chmod(0o600)
    print(json.dumps({"file":str(dest),"sha256":report["sha256"],
                      "qualified":report["qualified_count"],"seats":report["five_seats"]},indent=2))


if __name__=="__main__":
    main()
