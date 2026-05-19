from __future__ import annotations

import shutil
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.config import TEMP_DIR
from app.models import Job


class JobControl:
    """Per-job cooperative pause/cancel signaling.

    `pause` and `cancel` are threading events because the work that honors
    them (faster-whisper segment iteration) runs in a thread executor.
    The asyncio side just calls .pause()/.resume()/.cancel().
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # set = running; cleared = paused

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def wait_if_paused(self) -> None:
        """Block synchronously until resumed. Returns immediately if cancelled."""
        while not self._pause.is_set() and not self._cancel.is_set():
            # Short timeout so cancel is observed within ~200ms even mid-pause.
            self._pause.wait(timeout=0.2)

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.set()  # wake any paused waiter so it can observe the cancel

    def pause(self) -> None:
        if not self._cancel.is_set():
            self._pause.clear()

    def resume(self) -> None:
        self._pause.set()


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._dirs: dict[str, Path] = {}
        self._controls: dict[str, JobControl] = {}

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
        self._controls[job_id] = JobControl()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def dir(self, job_id: str) -> Path:
        return self._dirs[job_id]

    def control(self, job_id: str) -> JobControl:
        return self._controls[job_id]

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
        self._controls.pop(job_id, None)


@lru_cache
def get_store() -> JobStore:
    return JobStore()
