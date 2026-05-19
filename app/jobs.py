from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.config import TEMP_DIR
from app.models import Job


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._dirs: dict[str, Path] = {}

    def create(self, original_filename: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        work_dir = TEMP_DIR / job_id
        work_dir.mkdir(parents=True, exist_ok=False)
        job = Job(
            id=job_id,
            original_filename=original_filename,
            created_at=datetime.now(timezone.utc),
        )
        self._jobs[job_id] = job
        self._dirs[job_id] = work_dir
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def dir(self, job_id: str) -> Path:
        return self._dirs[job_id]

    def update(self, job_id: str, **fields) -> Job:
        job = self._jobs[job_id]
        updated = job.model_copy(update=fields)
        self._jobs[job_id] = updated
        return updated

    def cleanup(self, job_id: str) -> None:
        work_dir = self._dirs.pop(job_id, None)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        self._jobs.pop(job_id, None)


@lru_cache
def get_store() -> JobStore:
    return JobStore()
