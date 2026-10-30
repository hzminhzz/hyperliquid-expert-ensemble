"""WP4 holdout must not reveal outcomes until the registered assessment cut."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.test_copy_forward import _deliver, _fixture, _record

from ensemble.copy_forward import ForwardStore
from ensemble.copy_forward_assess import assess, save_report


def test_holdout_assessor_refuses_early_peeking_and_never_qualifies_mids(tmp_path):
    frozen,ledger,journal,prices,forward=_fixture(tmp_path)
    cut=_record((frozen,ledger,journal,prices,forward))
    _deliver(forward,cut["cut_id"])
    with pytest.raises(RuntimeError,match="HOLDOUT_LOCKED"):
        assess(frozen=frozen,forward_db=forward,now=frozen.start+timedelta(days=10))
    assert not forward.exists() is False
    assessed=assess(
        frozen=frozen,forward_db=forward,
        now=datetime(2026,12,2,0,0,1,tzinfo=UTC),
    )
    assert assessed["status"]=="BLOCKED_NOT_PREDICTIVE_OR_EXECUTION_QUALIFIED"
    assert assessed["source_coverage_qualified"] is False
    assert assessed["financial_execution"] is False
    assert "INSUFFICIENT_INDEPENDENT_DAYS" in assessed["blockers"]
    assert "NO_VERIFIED_SIDE_SPECIFIC_EXECUTION_OR_FILL_COSTS" in assessed["blockers"]
    out=tmp_path/"private"/"report.json"
    record=save_report(report=assessed,output=out)
    assert record["financial_execution"] is False
    assert out.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_report(report=assessed,output=out)


def test_horizon_revisions_do_not_erase_each_other(tmp_path):
    frozen,ledger,journal,prices,forward=_fixture(tmp_path)
    _record((frozen,ledger,journal,prices,forward))
    with ForwardStore(forward) as store:
        arm=store.db.execute("SELECT arm_id FROM forward_arms").fetchone()[0]
        now=datetime(2026,12,2,0,0,1,tzinfo=UTC)
        for horizon in ("1h","15m","4h"):
            assert store.record_evaluation(
                arm_id=arm,revision="wp4-mid-diagnostic-v1:"+horizon,
                result={"status":"ABSTAIN","horizon":horizon},created_at=now,
            )
        assert store.db.execute(
            "SELECT COUNT(*) FROM forward_evaluations"
        ).fetchone()[0] == 3
        with pytest.raises(ValueError,match="supersedes"):
            store.record_evaluation(
                arm_id=arm,revision="new-revision:1h",
                result={"status":"CENSORED"},created_at=now,
            )
