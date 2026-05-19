from __future__ import annotations

import logging
import shutil
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.config import EXPORTS_DIR, TEMP_DIR
from app.models import Job, JobStatus, PhaseRun

logger = logging.getLogger(__name__)


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
    """In-memory index of jobs, persisted as `job.json` inside each work_dir.

    The index is rebuilt from disk on startup (load_from_disk), so finished
    transcriptions survive server restarts and show up in the History view.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._dirs: dict[str, Path] = {}
        self._controls: dict[str, JobControl] = {}

    def _job_file(self, job_id: str) -> Path:
        return TEMP_DIR / job_id / "job.json"

    def _save_job(self, job: Job) -> None:
        """Persist metadata to disk. Silently skips if the work_dir is gone."""
        path = self._job_file(job.id)
        if not path.parent.exists():
            return
        try:
            path.write_text(job.model_dump_json(indent=2))
        except OSError as e:
            logger.warning("Could not persist job %s: %s", job.id, e)

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
        self._save_job(job)
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
        self._save_job(updated)
        return updated

    def list_all(self) -> list[Job]:
        """All jobs sorted newest-first."""
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def phase_start(self, job_id: str, phase: str) -> None:
        """Record the start of a pipeline phase. Resets duration if re-entered
        (e.g. polish is re-run after a re-polish request)."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        runs = dict(job.phase_runs)
        runs[phase] = PhaseRun(started_at=datetime.now(timezone.utc))
        self.update(job_id, phase_runs=runs)

    def phase_end(self, job_id: str, phase: str) -> None:
        """Record completion of a pipeline phase. No-op if phase wasn't started
        or already has a duration recorded."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        run = job.phase_runs.get(phase)
        if run is None or run.duration_seconds is not None:
            return
        duration = (datetime.now(timezone.utc) - run.started_at).total_seconds()
        runs = dict(job.phase_runs)
        runs[phase] = PhaseRun(started_at=run.started_at, duration_seconds=duration)
        self.update(job_id, phase_runs=runs)

    def mark_completed(self, job_id: str) -> None:
        """Stamp the pipeline-finished time (any outcome — done, error, cancelled)."""
        if self._jobs.get(job_id) is not None:
            self.update(job_id, completed_at=datetime.now(timezone.utc))

    def cleanup(self, job_id: str) -> None:
        """Delete everything related to a job: work_dir + exports + memory."""
        job = self._jobs.get(job_id)
        if job is not None:
            for name in (job.export_md_filename, job.export_pdf_filename):
                if name:
                    (EXPORTS_DIR / name).unlink(missing_ok=True)
        work_dir = self._dirs.pop(job_id, None)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        self._jobs.pop(job_id, None)
        self._controls.pop(job_id, None)

    def load_from_disk(self) -> int:
        """Scan TEMP_DIR for job.json files and rebuild the in-memory index.

        Jobs found in non-terminal states (running, paused, pending) are
        rewritten to 'error' because they were obviously interrupted by a
        restart — they can never resume on their own.

        Returns the count of jobs loaded.
        """
        if not TEMP_DIR.exists():
            return 0
        loaded = 0
        for job_dir in TEMP_DIR.iterdir():
            if not job_dir.is_dir():
                continue
            job_file = job_dir / "job.json"
            if not job_file.exists():
                continue
            try:
                job = Job.model_validate_json(job_file.read_text())
            except Exception as e:
                logger.warning("Skipping malformed %s: %s", job_file, e)
                continue
            if job.status in (JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PENDING):
                job = job.model_copy(update={
                    "status": JobStatus.ERROR,
                    "error": "Server restarted while this job was in flight",
                    "message": "Interrupted by restart",
                })
                try:
                    job_file.write_text(job.model_dump_json(indent=2))
                except OSError:
                    pass
            self._jobs[job.id] = job
            self._dirs[job.id] = job_dir
            self._controls[job.id] = JobControl()
            loaded += 1
        return loaded


@lru_cache
def get_store() -> JobStore:
    return JobStore()
