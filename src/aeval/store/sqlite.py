"""SQLite trial store: append-only, idempotent, verifiable.

- ``trial_id`` is globally unique (PK);
- (run, suite, task, index) is unique — a second trial with the same
  coordinates is rejected, never silently overwritten;
- grader results are keyed by (trial, grader id, grader version) so a
  regraded result is a NEW row, preserving history;
- raw records are permanent: no update paths exist.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from aeval.contracts import (
    CompletenessRecord,
    GradeResult,
    RequirementBitmap,
    RunManifest,
    TrialRecord,
)

__all__ = ["TrialStore", "StoreConflictError"]
# Atomic unit-of-work note: persist_trial_with_grades is the production
# write path; the single-entity persists remain for migrations
# and tooling.


class StoreConflictError(RuntimeError):
    """A uniqueness constraint violation — never silently merged."""


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class TrialStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- runs -----------------------------------------------------------

    def create_run(self, manifest: RunManifest) -> None:
        try:
            self._conn.execute(
                "INSERT INTO runs (run_id, manifest_json, created_at) VALUES (?,?,?)",
                (
                    manifest.run_id,
                    manifest.model_dump_json(exclude_none=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.execute(
                "INSERT INTO suite_runs (run_id, suite_id, suite_version, overlay_digest) "
                "VALUES (?,?,?,?)",
                (
                    manifest.run_id,
                    manifest.overlay.suite_id,
                    manifest.overlay.suite_version,
                    manifest.overlay.overlay_digest,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StoreConflictError(
                f"run {manifest.run_id!r} already exists — run ids are never reused"
            ) from exc

    def load_run_manifest(self, run_id: str) -> RunManifest:
        """Load the intent manifest as recorded at run creation (trusted)."""
        row = self._conn.execute(
            "SELECT manifest_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"run {run_id!r} is not in the store")
        return RunManifest.model_validate_json(row["manifest_json"])

    def run_intent_digest(self, run_id: str) -> str:
        """The trusted intent digest of a recorded run (seal binding).

        The stored manifest is the copy written at run creation, before
        any trial existed; comparing its intent digest against the
        on-disk manifest at seal time detects rewrites.
        """
        from aeval.bundle.manifest import intent_digest

        row = self._conn.execute(
            "SELECT manifest_json FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"run {run_id!r} is not in the store")
        return intent_digest(json.loads(row["manifest_json"]))

    def list_trial_ids(self, run_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT trial_id FROM trials WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [r["trial_id"] for r in rows]

    # -- trials ---------------------------------------------------------

    def persist_trial(self, record: TrialRecord) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._insert_trial(record)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            # A conflict must leave the store exactly as it was — a
            # half-written prefix that a later commit could flush is a
            # corrupted record.
            self._conn.rollback()
            c = record.coordinates
            raise StoreConflictError(
                f"trial {record.trial_id!r} conflicts with an existing record "
                f"({c.run_id}/{c.suite_id}/{c.task_id}/{c.trial_index}): {exc}"
            ) from exc

    def persist_grades(self, trial_id: str, results: Sequence[GradeResult]) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._insert_grades(trial_id, results)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            # Roll back the whole batch: a mid-batch conflict must not
            # leave earlier rows behind.
            self._conn.rollback()
            raise StoreConflictError(
                f"grade batch for trial {trial_id!r} rolled back — a grade "
                f"result already exists: {exc}"
            ) from exc

    def persist_trial_with_grades(
        self, record: TrialRecord, results: Sequence[GradeResult]
    ) -> None:
        """Persist the trial and its grades as ONE atomic transaction.

        Either the full record with every grade lands, or nothing does.
        A conflict anywhere rolls the whole unit back; no prefix of a
        trial or of its grade list can survive.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._insert_trial(record)
            self._insert_grades(record.trial_id, results)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            c = record.coordinates
            raise StoreConflictError(
                f"trial {record.trial_id!r} + grades rolled back — conflict "
                f"({c.run_id}/{c.suite_id}/{c.task_id}/{c.trial_index}): {exc}"
            ) from exc

    def _insert_trial(self, record: TrialRecord) -> None:
        c = record.coordinates
        self._conn.execute(
            """INSERT INTO trials (
                trial_id, run_id, suite_id, suite_version, task_id,
                trial_index, stop_reason, baseline_ok, requirements_json,
                observed_model_json, budget_json, adapter_json, claim_json,
                artifacts_json, transcript_extra_json, fork_json,
                versions_json, verdict, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.trial_id,
                c.run_id,
                c.suite_id,
                c.suite_version,
                c.task_id,
                c.trial_index,
                record.stop_reason,
                1 if record.baseline_ok else 0,
                json.dumps(record.requirements.to_dict()),
                (
                    record.observed_model.model_dump_json(exclude_none=True)
                    if record.observed_model
                    else None
                ),
                record.budget.model_dump_json(exclude_none=True) if record.budget else None,
                record.adapter.model_dump_json(exclude_none=True) if record.adapter else None,
                record.claim.model_dump_json(exclude_none=True) if record.claim else None,
                json.dumps(
                    {k: v.model_dump() for k, v in record.artifacts.items()}
                ),
                json.dumps(record.transcript_extra) if record.transcript_extra else None,
                record.fork.model_dump_json(exclude_none=True) if record.fork else None,
                record.versions.model_dump_json(exclude_none=True) if record.versions else None,
                record.verdict,
                record.created_at.isoformat(),
            ),
        )

    def _insert_grades(self, trial_id: str, results: Sequence[GradeResult]) -> None:
        for r in results:
            self._conn.execute(
                """INSERT INTO rubric_results (
                    trial_id, grader_id, grader_version, layer, veto,
                    score_json, status, reasons_json, coverage_json,
                    metrics_json, produced_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trial_id,
                    r.grader_id,
                    r.grader_version,
                    r.layer,
                    1 if r.veto else 0,
                    r.score.model_dump_json(exclude_none=True),
                    r.status,
                    json.dumps(r.reasons),
                    r.coverage.model_dump_json(exclude_none=True) if r.coverage else None,
                    (
                        json.dumps([m.model_dump() for m in r.metrics])
                        if r.metrics
                        else None
                    ),
                    r.produced_at.isoformat(),
                ),
            )

    # -- reads ----------------------------------------------------------

    def load_trial(self, trial_id: str) -> TrialRecord:
        row = self._conn.execute(
            "SELECT * FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if row is None:
            raise KeyError(trial_id)
        return self._record_from_row(row)

    def list_trials(self, run_ids: Sequence[str]) -> list[TrialRecord]:
        placeholders = ",".join("?" for _ in run_ids)
        rows = self._conn.execute(
            f"SELECT * FROM trials WHERE run_id IN ({placeholders}) "
            "ORDER BY suite_id, task_id, trial_index",
            tuple(run_ids),
        ).fetchall()
        return [self._record_from_row(r) for r in rows]

    def _record_from_row(self, row: sqlite3.Row) -> TrialRecord:
        from aeval.contracts import (
            AdapterSpec,
            ArtifactRef,
            BudgetSnapshot,
            ClaimCheck,
            ForkLineage,
            ObservedModel,
            TrialCoordinates,
            VersionsBundle,
        )

        requirements = RequirementBitmap(**json.loads(row["requirements_json"]))
        return TrialRecord(
            trial_id=row["trial_id"],
            coordinates=TrialCoordinates(
                run_id=row["run_id"],
                suite_id=row["suite_id"],
                suite_version=row["suite_version"],
                task_id=row["task_id"],
                trial_index=row["trial_index"],
            ),
            stop_reason=row["stop_reason"],
            baseline_ok=bool(row["baseline_ok"]),
            requirements=requirements,
            observed_model=(
                ObservedModel.model_validate_json(row["observed_model_json"])
                if row["observed_model_json"]
                else None
            ),
            budget=BudgetSnapshot.model_validate_json(row["budget_json"]) if row["budget_json"] else None,
            adapter=AdapterSpec.model_validate_json(row["adapter_json"]) if row["adapter_json"] else None,
            claim=ClaimCheck.model_validate_json(row["claim_json"]) if row["claim_json"] else None,
            artifacts={
                k: ArtifactRef.model_validate(v)
                for k, v in json.loads(row["artifacts_json"]).items()
            },
            transcript_extra=json.loads(row["transcript_extra_json"]) if row["transcript_extra_json"] else None,
            fork=ForkLineage.model_validate_json(row["fork_json"]) if row["fork_json"] else None,
            versions=VersionsBundle.model_validate_json(row["versions_json"]) if row["versions_json"] else None,
            grades=self._grades_of(row["trial_id"]),
            verdict=row["verdict"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _grades_of(self, trial_id: str) -> list[GradeResult]:
        from aeval.contracts import CoverageSummary, MetricOutcome, Score

        rows = self._conn.execute(
            "SELECT * FROM rubric_results WHERE trial_id = ? ORDER BY produced_at",
            (trial_id,),
        ).fetchall()
        out: list[GradeResult] = []
        for r in rows:
            out.append(
                GradeResult(
                    grader_id=r["grader_id"],
                    grader_version=r["grader_version"],
                    layer=r["layer"],
                    veto=bool(r["veto"]),
                    score=Score.model_validate_json(r["score_json"]),
                    status=r["status"],
                    reasons=json.loads(r["reasons_json"]),
                    coverage=(
                        CoverageSummary.model_validate_json(r["coverage_json"])
                        if r["coverage_json"]
                        else None
                    ),
                    metrics=(
                        [
                            MetricOutcome.model_validate(m)
                            for m in json.loads(r["metrics_json"])
                        ]
                        if "metrics_json" in r.keys() and r["metrics_json"]
                        else None
                    ),
                    produced_at=datetime.fromisoformat(r["produced_at"]),
                )
            )
        return out
