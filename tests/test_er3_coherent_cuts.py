"""ER3 qualification: coherent cuts, interrupt coalescing, and bounded flow influence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.coherent_cut import InterruptCoalescer, aligned_minute_cut
from ensemble.conviction import ConvictionStatus, RelativeConviction
from ensemble.ensemble_evidence import build_ensemble_evidence
from ensemble.independence import IndependenceArtifact, IndependenceCluster
from ensemble.wallet_evidence import EvidenceCause, build_wallet_evidence, derive_intent_event

T0 = datetime(2026, 10, 6, 12, 0, 37, tzinfo=UTC)
INSTRUMENT = "hyperliquid:mainnet:perp:default:BTC"


def test_aligned_cut_is_deterministic_and_carries_all_revisions():
    kwargs = {
        "instrument_id": INSTRUMENT,
        "event_time": T0,
        "knowledge_time": T0 + timedelta(seconds=2),
        "watermark": "ledger:42",
        "source_revision": 42,
        "normalization_revision": "norm:7",
        "independence_revision": "ind:3",
        "feature_revision": "feat:4",
        "universe_revision": "uni:5",
    }
    a = aligned_minute_cut(**kwargs)
    b = aligned_minute_cut(**kwargs)
    assert a == b
    assert a.event_time.second == 0
    assert a.cut_id == b.cut_id
    assert a.watermark == "ledger:42"


def test_coalescer_replaces_obsolete_pending_contribution():
    coalescer = InterruptCoalescer(timedelta(seconds=3))
    coalescer.request(
        instrument_id=INSTRUMENT,
        cluster_id="cluster:A",
        requested_at=T0,
        evidence_id="open",
        revision=1,
    )
    coalescer.request(
        instrument_id=INSTRUMENT,
        cluster_id="cluster:A",
        requested_at=T0 + timedelta(seconds=1),
        evidence_id="closed",
        revision=2,
    )
    assert not coalescer.ready(T0 + timedelta(seconds=2))
    ready = coalescer.ready(T0 + timedelta(seconds=3))
    assert len(ready) == 1
    assert ready[0].latest_evidence_id == "closed"
    assert ready[0].latest_revision == 2
    assert not coalescer.pending()


def test_wallet_flow_is_bounded_before_cluster_budget_but_raw_flow_is_preserved():
    event = derive_intent_event(
        event_id="huge",
        expert_id="A",
        coin=INSTRUMENT,
        quantity_before=Decimal(0),
        quantity_after=Decimal(100),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        event_time=T0 - timedelta(seconds=20),
        known_at=T0 - timedelta(seconds=20),
    )
    assert event is not None
    wallet = build_wallet_evidence(
        expert_id="A",
        coin=INSTRUMENT,
        quantity=Decimal(100),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=1,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
        intent_events=[event],
    )
    raw = next(x for x in wallet.intent_flow if x.horizon == "1m")
    assert raw.delta_bias == Decimal(100)

    independence = IndependenceArtifact(
        revision="ind",
        feature_schema_version="v1",
        training_cutoff=T0 - timedelta(days=1),
        effective_from=T0 - timedelta(hours=1),
        update_schedule="weekly",
        threshold=Decimal("0.2"),
        clusters=(
            IndependenceCluster(
                cluster_id="cluster:A",
                members=("A",),
                budget=Decimal("0.25"),
                status="INFERRED",
            ),
        ),
        unknown_experts=(),
        effective_breadth=Decimal(1),
        pair_metrics=(),
    )
    conviction = RelativeConviction(
        expert_id="A",
        coin=INSTRUMENT,
        raw_bias=wallet.raw_bias,
        magnitude_percentile=Decimal(1),
        signed_percentile=Decimal(1),
        status=ConvictionStatus.VALID,
        support=30,
        artifact_revision="conv",
    )
    ensemble = build_ensemble_evidence(
        evidence_id="ens",
        coin=INSTRUMENT,
        wallets=[wallet],
        independence=independence,
        convictions={"A": conviction},
        feature_revision="feat",
        universe_revision="uni",
        as_of=T0,
        knowledge_time=T0,
    )
    bounded = next(x for x in ensemble.flow_evidence if x.horizon == "1m")
    assert bounded.observed == Decimal("0.25")
    assert raw.delta_bias == Decimal(100)


def test_missing_wallet_mass_is_not_renormalized_to_survivor():
    eligible = build_wallet_evidence(
        expert_id="A",
        coin=INSTRUMENT,
        quantity=Decimal(1),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=1,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
    )
    missing = build_wallet_evidence(
        expert_id="B",
        coin=INSTRUMENT,
        quantity=Decimal(1),
        valuation_price=Decimal(100),
        equity=None,
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=1,
        current_cause=EvidenceCause.EQUITY_CHANGE,
    )
    independence = IndependenceArtifact(
        revision="ind",
        feature_schema_version="v1",
        training_cutoff=T0 - timedelta(days=1),
        effective_from=T0 - timedelta(hours=1),
        update_schedule="weekly",
        threshold=Decimal("0.2"),
        clusters=(
            IndependenceCluster("cluster:A", ("A",), Decimal("0.5"), "INFERRED"),
            IndependenceCluster(
                "newcomer:B", ("B",), Decimal("0.5"), "UNKNOWN_SIMILARITY"
            ),
        ),
        unknown_experts=("B",),
        effective_breadth=Decimal(2),
        pair_metrics=(),
    )
    ensemble = build_ensemble_evidence(
        evidence_id="ens",
        coin=INSTRUMENT,
        wallets=[eligible, missing],
        independence=independence,
        convictions={},
        feature_revision="feat",
        universe_revision="uni",
        as_of=T0,
        knowledge_time=T0,
    )
    assert ensemble.state_evidence == Decimal("0.5")
    assert ensemble.state_missing_mass == Decimal("0.5")
