"""DI7 bounded, hash-verified, immutable research export.

This runs outside the hot ingestion path and NEVER prunes primary event data.
Compressed NDJSON is the dependency-free archive baseline. Native Parquet
requires a separately installed, optional research adapter.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, cast


def export_opportunity_partition(
    *,
    source_db:Path,
    output:Path,
    first_seq:int=0,
    limit:int=50_000,
    format:str="ndjson.gz",
) -> dict[str,Any]:
    if not 1<=limit<=100_000 or first_seq<0:
        raise ValueError("bounded export limits required")
    if format not in ("ndjson.gz","parquet"):
        raise ValueError("unsupported explicit export type")
    if output.exists() or output.with_suffix(output.suffix+".manifest.json").exists():
        raise FileExistsError("immutable export already exists")
    with closing(sqlite3.connect(f"file:{source_db}?mode=ro",uri=True,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute(
            "SELECT first_ledger_seq,payload_json FROM opportunity_decisions "
            "WHERE first_ledger_seq>? ORDER BY first_ledger_seq LIMIT ?",
            (first_seq,limit),
        )
        output.parent.mkdir(parents=True,exist_ok=True)
        count=0
        last_seq=first_seq
        if format=="parquet":
            # Local runtime deliberately has no research dataframe dependency.
            try:
                pl=importlib.import_module("polars")
            except ImportError as exc:
                raise RuntimeError(
                    "PARQUET_ADAPTER_UNAVAILABLE: install optional Polars offline"
                ) from exc
            items=[]
            for record in rows:
                items.append(json.loads(str(record["payload_json"])))
                last_seq=int(record["first_ledger_seq"])
                count+=1
            if not items:
                raise ValueError("export partition has no data")
            pl.from_dicts(items).write_parquet(output,compression="zstd")
        else:
            # The no-external-package path streams, avoiding a large Pandas/Arrow
            # object on the memory-constrained VPS.
            with output.open("xb") as file:  # noqa: SIM117
                with gzip.GzipFile(fileobj=file,mode="wb",filename="",mtime=0) as zipped:
                    with io.TextIOWrapper(zipped,encoding="utf-8") as text:
                        for record in rows:
                            text.write(str(record["payload_json"])+"\n")
                            last_seq=int(record["first_ledger_seq"])
                            count+=1
        if not count:
            output.unlink(missing_ok=True)
            raise ValueError("export partition has no data")
    sha=hashlib.sha256()
    with output.open("rb") as file:
        for block in iter(lambda:file.read(1024*1024),b""):
            sha.update(cast(bytes,block))
    manifest={
        "schema":"DI7-copy-opportunity-export-v1",
        "format":format,"first_seq_exclusive":first_seq,
        "last_seq_inclusive":last_seq,"row_count":count,
        "sha256":sha.hexdigest(),"source_role":"SHADOW_RESEARCH",
        "primary_observations_preserved":True,
        "predictive_qualification":"NOT_GRANTED",
    }
    manifest_path=output.with_suffix(output.suffix+".manifest.json")
    with manifest_path.open("x",encoding="utf-8") as file:
        json.dump(manifest,file,indent=2,sort_keys=True)
    return manifest


def verify_opportunity_partition(output:Path) -> dict[str,Any]:
    path=output.with_suffix(output.suffix+".manifest.json")
    manifest=json.loads(path.read_text(encoding="utf-8"))
    sha=hashlib.sha256()
    with output.open("rb") as file:
        for block in iter(lambda:file.read(1024*1024),b""):
            sha.update(block)
    if sha.hexdigest()!=manifest["sha256"]:
        raise ValueError("archived research partition hash mismatch")
    return {"status":"VERIFIED_IMMUTABLE_BYTES","rows":manifest["row_count"],
            "last_seq":manifest["last_seq_inclusive"],"format":manifest["format"]}
