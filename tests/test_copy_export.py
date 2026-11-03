"""DI7 deterministic bounded archive and tamper detection."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ensemble.copy_export import export_opportunity_partition, verify_opportunity_partition


def test_export_is_sealed_hashed_and_bounded(tmp_path:Path)->None:
    db=tmp_path/"in.db"
    with closing(sqlite3.connect(db)) as connection,connection:
        connection.execute(
            "CREATE TABLE opportunity_decisions(first_ledger_seq INTEGER,payload_json TEXT)"
        )
        for i in range(1,6):
            connection.execute(
                "INSERT INTO opportunity_decisions VALUES (?,?)",
                (i,json.dumps({"opportunity_id":f"copy:{i}","first_ledger_seq":i})),
            )
    out=tmp_path/"part.ndjson.gz"
    m=export_opportunity_partition(source_db=db,output=out,first_seq=1,limit=2)
    assert m["row_count"]==2
    assert m["last_seq_inclusive"]==3
    assert verify_opportunity_partition(out)["rows"]==2
    with pytest.raises(FileExistsError):
        export_opportunity_partition(source_db=db,output=out,first_seq=0)
    with out.open("ab") as file:
        file.write(b"tampered")
    with pytest.raises(ValueError,match="hash mismatch"):
        verify_opportunity_partition(out)
