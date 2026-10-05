"""Job checkpoints and bounded resume runner (Contracts C1, C7, Q16 Task 3).

Allows long-running evaluations to checkpoint progress and resume within budget limits.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluation_jobs (
    job_id             TEXT    PRIMARY KEY,
    status             TEXT    NOT NULL,  -- RUNNING, CHECKPOINTED, COMPLETED, CANCELLED
    budget_units       INTEGER NOT NULL,
    consumed_units     INTEGER NOT NULL DEFAULT 0,
    current_step       INTEGER NOT NULL DEFAULT 0,
    total_steps        INTEGER NOT NULL,
    checkpoint_json    TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);
"""


@dataclass(slots=True, frozen=True)
class EvaluationJob:
    job_id: str
    status: str
    budget_units: int
    consumed_units: int
    current_step: int
    total_steps: int
    checkpoint_data: dict[str, Any]
    updated_at: datetime

    @property
    def remaining_budget(self) -> int:
        return max(0, self.budget_units - self.consumed_units)


class JobStore:
    """Persistent SQLite-backed job checkpoint store."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute("PRAGMA journal_mode = WAL")
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def create_job(self, job_id: str, total_steps: int, budget_units: int) -> EvaluationJob:
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO evaluation_jobs (
                    job_id, status, budget_units, consumed_units, current_step,
                    total_steps, checkpoint_json, updated_at
                ) VALUES (?, 'CHECKPOINTED', ?, 0, 0, ?, '{}', ?)
                """,
                (job_id, budget_units, total_steps, now),
            )
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError(f"Job {job_id} could not be retrieved after creation")
        return job

    def get_job(self, job_id: str) -> EvaluationJob | None:
        cur = self.conn.execute("SELECT * FROM evaluation_jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        return EvaluationJob(
            job_id=row["job_id"],
            status=row["status"],
            budget_units=int(row["budget_units"]),
            consumed_units=int(row["consumed_units"]),
            current_step=int(row["current_step"]),
            total_steps=int(row["total_steps"]),
            checkpoint_data=json.loads(row["checkpoint_json"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def resume_and_execute(
        self,
        job_id: str,
        cost_per_step: int = 10,
        additional_budget: int = 0,
    ) -> EvaluationJob:
        """Resume an interrupted evaluation from its checkpoint within remaining budget (Q16 Task 3)."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        total_budget = job.budget_units + additional_budget
        consumed = job.consumed_units
        step = job.current_step
        total = job.total_steps
        data = dict(job.checkpoint_data)

        while step < total and (consumed + cost_per_step) <= total_budget:
            step += 1
            consumed += cost_per_step
            data[f"step_{step}"] = f"completed_at_unit_{consumed}"

        new_status = "COMPLETED" if step >= total else "CHECKPOINTED"
        now = datetime.now(UTC).isoformat()

        with self.conn:
            self.conn.execute(
                """
                UPDATE evaluation_jobs SET
                    status = ?,
                    budget_units = ?,
                    consumed_units = ?,
                    current_step = ?,
                    checkpoint_json = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (new_status, total_budget, consumed, step, json.dumps(data), now, job_id),
            )

        updated_job = self.get_job(job_id)
        if updated_job is None:
            raise RuntimeError(f"Job {job_id} could not be retrieved after execution")
        return updated_job

    def cancel_job(self, job_id: str) -> EvaluationJob:
        """Cancel an evaluation job."""
        now = datetime.now(UTC).isoformat()
        with self.conn:
            self.conn.execute(
                "UPDATE evaluation_jobs SET status = 'CANCELLED', updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")
        return job
