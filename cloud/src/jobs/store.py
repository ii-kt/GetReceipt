from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import UUID

from .models import (
    BatchJob,
    BatchJobState,
    Challenge,
    JobEvent,
    assert_public_payload,
    datetime_to_text,
    utc_now,
)


class JobStoreError(RuntimeError):
    pass


class JobNotFoundError(JobStoreError):
    pass


class VersionConflictError(JobStoreError):
    pass


class IdempotencyConflictError(JobStoreError):
    pass


_UNSET = object()


class SQLiteJobStore:
    """Durable job metadata with owner checks and optimistic concurrency.

    Challenge responses are intentionally outside this store. In particular,
    this schema has no column capable of holding a plaintext OTP response.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.path = str(path)
        self._clock = clock
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SQLiteJobStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    target_month TEXT NOT NULL,
                    service_ids_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    completed_json TEXT NOT NULL,
                    current_service TEXT,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_json TEXT,
                    result_json TEXT,
                    UNIQUE(owner, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS challenges (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    challenge_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES batch_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS challenges_job_created
                    ON challenges(job_id, created_at, id);

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES batch_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS events_job_id
                    ON job_events(job_id, id);
                """
            )

    @staticmethod
    def _default_idempotency_key(target_month: str, service_ids: Sequence[str]) -> str:
        return f"{target_month}:{','.join(service_ids)}"

    def create_job(
        self,
        *,
        owner: str,
        target_month: str,
        service_ids: Sequence[str],
        idempotency_key: str | None = None,
    ) -> BatchJob:
        job = BatchJob.new(
            owner=owner,
            target_month=target_month,
            service_ids=tuple(service_ids),
            now=self._clock(),
        )
        key = str(
            idempotency_key
            if idempotency_key is not None
            else self._default_idempotency_key(job.target_month, job.service_ids)
        ).strip()
        if not key:
            raise ValueError("idempotency_key is required")

        with self._transaction() as connection:
            existing_row = connection.execute(
                """
                SELECT * FROM batch_jobs
                WHERE owner = ? AND idempotency_key = ?
                """,
                (job.owner, key),
            ).fetchone()
            if existing_row is not None:
                existing = self._job_from_row(existing_row)
                if (
                    existing.target_month != job.target_month
                    or existing.service_ids != job.service_ids
                ):
                    raise IdempotencyConflictError(
                        "idempotency key already belongs to a different request"
                    )
                return existing

            self._insert_job(connection, job, key)
            self._insert_event(
                connection,
                job.id,
                "job_created",
                {
                    "target_month": job.target_month,
                    "service_ids": list(job.service_ids),
                },
                job.created_at,
            )
        return job

    def get_job(self, job_id: UUID | str, *, owner: str) -> BatchJob:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM batch_jobs WHERE id = ? AND owner = ?",
                (str(job_id), str(owner).strip()),
            ).fetchone()
        if row is None:
            raise JobNotFoundError("job was not found")
        return self._job_from_row(row)

    def find_active_job(self, *, owner: str, target_month: str) -> BatchJob | None:
        active_states = (
            BatchJobState.QUEUED.value,
            BatchJobState.RUNNING.value,
            BatchJobState.WAITING_FOR_CHALLENGE.value,
        )
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM batch_jobs
                WHERE owner = ? AND target_month = ?
                  AND state IN (?, ?, ?)
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (str(owner).strip(), str(target_month), *active_states),
            ).fetchone()
        return self._job_from_row(row) if row is not None else None

    def claim_next(self, *, owner: str) -> BatchJob | None:
        """Atomically claim the oldest queued personal-account job."""

        timestamp = self._clock()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM batch_jobs
                WHERE owner = ? AND state = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (str(owner).strip(), BatchJobState.QUEUED.value),
            ).fetchone()
            if row is None:
                return None
            existing = self._job_from_row(row)
            result = existing.result if isinstance(existing.result, Mapping) else {}
            failed_service_ids = {
                str(value)
                for value in result.get("failed_service_ids") or ()
                if str(value) in existing.service_ids
            }
            current = next(
                (
                    service_id
                    for service_id in existing.service_ids
                    if service_id not in existing.completed
                    and service_id not in failed_service_ids
                ),
                None,
            )
            updated = replace(
                existing,
                state=BatchJobState.RUNNING,
                current=current,
                version=existing.version + 1,
                updated_at=timestamp,
                error=None,
            )
            cursor = connection.execute(
                """
                UPDATE batch_jobs
                SET state = ?, current_service = ?, version = ?,
                    updated_at = ?, error_json = NULL
                WHERE id = ? AND owner = ? AND version = ? AND state = ?
                """,
                (
                    updated.state.value,
                    updated.current,
                    updated.version,
                    datetime_to_text(updated.updated_at),
                    str(updated.id),
                    updated.owner,
                    existing.version,
                    BatchJobState.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError("queued job was claimed by another worker")
            self._insert_event(
                connection,
                updated.id,
                "job_claimed",
                {"current_service_id": updated.current},
                updated.updated_at,
            )
        return updated

    def recover_incomplete_jobs(self, *, owner: str) -> int:
        """Requeue jobs whose live browser was lost with a worker process.

        Challenge responses are process-local, so a restarted worker must issue
        a new provider challenge rather than pretending the old page survived.
        """

        timestamp = self._clock()
        recovered = 0
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM batch_jobs
                WHERE owner = ? AND state IN (?, ?)
                ORDER BY created_at, id
                """,
                (
                    str(owner).strip(),
                    BatchJobState.RUNNING.value,
                    BatchJobState.WAITING_FOR_CHALLENGE.value,
                ),
            ).fetchall()
            for row in rows:
                existing = self._job_from_row(row)
                cursor = connection.execute(
                    """
                    UPDATE batch_jobs
                    SET state = ?, current_service = NULL, version = ?,
                        updated_at = ?, error_json = NULL
                    WHERE id = ? AND owner = ? AND version = ?
                    """,
                    (
                        BatchJobState.QUEUED.value,
                        existing.version + 1,
                        datetime_to_text(timestamp),
                        str(existing.id),
                        existing.owner,
                        existing.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise VersionConflictError("incomplete job changed during recovery")
                self._insert_event(
                    connection,
                    existing.id,
                    "worker_recovered_job",
                    {"reason_code": "LIVE_BROWSER_LOST"},
                    timestamp,
                )
                recovered += 1
        return recovered

    def compare_and_set(
        self,
        job_id: UUID | str,
        *,
        owner: str,
        expected_version: int,
        state: BatchJobState | str | object = _UNSET,
        completed: Sequence[str] | object = _UNSET,
        current: str | None | object = _UNSET,
        error: Mapping[str, Any] | None | object = _UNSET,
        result: Mapping[str, Any] | None | object = _UNSET,
        event_type: str = "job_updated",
        event_payload: Mapping[str, Any] | None = None,
    ) -> BatchJob:
        payload = dict(event_payload or {})
        assert_public_payload(payload, label="event.payload")
        timestamp = self._clock()

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM batch_jobs WHERE id = ? AND owner = ?",
                (str(job_id), str(owner).strip()),
            ).fetchone()
            if row is None:
                raise JobNotFoundError("job was not found")

            existing = self._job_from_row(row)
            if existing.version != expected_version:
                raise VersionConflictError(
                    f"expected version {expected_version}, found {existing.version}"
                )

            updated = replace(
                existing,
                state=existing.state if state is _UNSET else BatchJobState(state),
                completed=(
                    existing.completed
                    if completed is _UNSET
                    else tuple(str(item) for item in completed)
                ),
                current=existing.current if current is _UNSET else current,
                error=(
                    existing.error
                    if error is _UNSET
                    else (None if error is None else dict(error))
                ),
                result=(
                    existing.result
                    if result is _UNSET
                    else (None if result is None else dict(result))
                ),
                version=existing.version + 1,
                updated_at=timestamp,
            )
            cursor = connection.execute(
                """
                UPDATE batch_jobs
                SET state = ?, completed_json = ?, current_service = ?,
                    version = ?, updated_at = ?, error_json = ?, result_json = ?
                WHERE id = ? AND owner = ? AND version = ?
                """,
                (
                    updated.state.value,
                    self._json(list(updated.completed)),
                    updated.current,
                    updated.version,
                    datetime_to_text(updated.updated_at),
                    self._optional_json(updated.error),
                    self._optional_json(updated.result),
                    str(updated.id),
                    updated.owner,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError("job was updated by another caller")
            self._insert_event(
                connection,
                updated.id,
                event_type,
                payload,
                updated.updated_at,
            )
        return updated

    def add_challenge(self, challenge: Challenge, *, owner: str) -> Challenge:
        """Persist public challenge metadata, never a challenge response."""

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT id FROM batch_jobs WHERE id = ? AND owner = ?",
                (str(challenge.job_id), str(owner).strip()),
            ).fetchone()
            if row is None:
                raise JobNotFoundError("job was not found")
            try:
                connection.execute(
                    """
                    INSERT INTO challenges(id, job_id, challenge_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(challenge.id),
                        str(challenge.job_id),
                        self._json(challenge.to_dict()),
                        datetime_to_text(challenge.created_at),
                    ),
                )
            except sqlite3.IntegrityError:
                existing_row = connection.execute(
                    "SELECT challenge_json FROM challenges WHERE id = ?",
                    (str(challenge.id),),
                ).fetchone()
                if existing_row is None:
                    raise
                existing = Challenge.from_dict(json.loads(existing_row["challenge_json"]))
                if existing != challenge:
                    raise IdempotencyConflictError(
                        "challenge id already belongs to different metadata"
                    )
                return existing
            self._insert_event(
                connection,
                challenge.job_id,
                "challenge_created",
                {
                    "challenge_id": str(challenge.id),
                    "challenge_type": challenge.type.value,
                    "expires_at": datetime_to_text(challenge.expires_at),
                },
                challenge.created_at,
            )
        return challenge

    def get_challenge(self, challenge_id: UUID | str, *, owner: str) -> Challenge:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT challenges.challenge_json
                FROM challenges
                JOIN batch_jobs ON batch_jobs.id = challenges.job_id
                WHERE challenges.id = ? AND batch_jobs.owner = ?
                """,
                (str(challenge_id), str(owner).strip()),
            ).fetchone()
        if row is None:
            raise JobNotFoundError("challenge was not found")
        return Challenge.from_dict(json.loads(row["challenge_json"]))

    def list_challenges(self, job_id: UUID | str, *, owner: str) -> list[Challenge]:
        self.get_job(job_id, owner=owner)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT challenge_json FROM challenges
                WHERE job_id = ?
                ORDER BY created_at, id
                """,
                (str(job_id),),
            ).fetchall()
        return [Challenge.from_dict(json.loads(row["challenge_json"])) for row in rows]

    def append_event(
        self,
        job_id: UUID | str,
        *,
        owner: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> JobEvent:
        safe_payload = dict(payload or {})
        assert_public_payload(safe_payload, label="event.payload")
        timestamp = self._clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT id FROM batch_jobs WHERE id = ? AND owner = ?",
                (str(job_id), str(owner).strip()),
            ).fetchone()
            if row is None:
                raise JobNotFoundError("job was not found")
            event_id = self._insert_event(
                connection,
                UUID(str(job_id)),
                event_type,
                safe_payload,
                timestamp,
            )
        return JobEvent(
            id=event_id,
            job_id=UUID(str(job_id)),
            event_type=event_type,
            payload=safe_payload,
            created_at=timestamp,
        )

    def list_events(self, job_id: UUID | str, *, owner: str) -> list[JobEvent]:
        self.get_job(job_id, owner=owner)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, job_id, event_type, payload_json, created_at
                FROM job_events
                WHERE job_id = ?
                ORDER BY id
                """,
                (str(job_id),),
            ).fetchall()
        return [
            JobEvent.from_dict(
                {
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
            )
            for row in rows
        ]

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _optional_json(cls, value: Any | None) -> str | None:
        return None if value is None else cls._json(value)

    @classmethod
    def _insert_job(
        cls,
        connection: sqlite3.Connection,
        job: BatchJob,
        idempotency_key: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO batch_jobs(
                id, owner, idempotency_key, target_month, service_ids_json,
                state, completed_json, current_service, version, created_at,
                updated_at, error_json, result_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(job.id),
                job.owner,
                idempotency_key,
                job.target_month,
                cls._json(list(job.service_ids)),
                job.state.value,
                cls._json(list(job.completed)),
                job.current,
                job.version,
                datetime_to_text(job.created_at),
                datetime_to_text(job.updated_at),
                cls._optional_json(job.error),
                cls._optional_json(job.result),
            ),
        )

    @classmethod
    def _insert_event(
        cls,
        connection: sqlite3.Connection,
        job_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> int:
        normalized_event_type = str(event_type).strip()
        if not normalized_event_type:
            raise ValueError("event_type is required")
        assert_public_payload(payload, label="event.payload")
        cursor = connection.execute(
            """
            INSERT INTO job_events(job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(job_id),
                normalized_event_type,
                cls._json(dict(payload)),
                datetime_to_text(created_at),
            ),
        )
        return int(cursor.lastrowid)

    @classmethod
    def _job_from_row(cls, row: sqlite3.Row) -> BatchJob:
        return BatchJob.from_dict(
            {
                "id": row["id"],
                "owner": row["owner"],
                "target_month": row["target_month"],
                "service_ids": json.loads(row["service_ids_json"]),
                "state": row["state"],
                "completed": json.loads(row["completed_json"]),
                "current": row["current_service"],
                "version": row["version"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
            }
        )
