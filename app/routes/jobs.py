from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.audio import (
    ACCEPTED_FORMATS,
    get_duration,
    has_audio_stream,
    normalize_to_wav,
)
from app.config import EXPORTS_DIR, get_settings
from app.events import ProgressEvent, get_bus
from app.jobs import get_store
from app.models import Job, JobPhase, JobStatus
from app.pipeline import run_pipeline, run_polish_only, spawn

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=Job, status_code=status.HTTP_201_CREATED)
async def create_job(file: UploadFile = File(...)) -> Job:
    s = get_settings()
    store = get_store()

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ACCEPTED_FORMATS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported format '{ext}'. Allowed: {sorted(ACCEPTED_FORMATS)}",
        )

    job = store.create(original_filename=file.filename or f"audio{ext}")
    work_dir = store.dir(job.id)
    original_path = work_dir / f"original{ext}"

    max_bytes = s.max_audio_mb * 1024 * 1024
    total = 0
    try:
        with original_path.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        f"File exceeds {s.max_audio_mb} MB limit",
                    )
                f.write(chunk)

        if not await has_audio_stream(original_path):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "No audio stream found in file",
            )

        duration = await get_duration(original_path)

        store.update(
            job.id,
            status=JobStatus.RUNNING,
            phase=JobPhase.INGEST,
            percent=50,
            message=f"Uploaded {total / 1024 / 1024:.1f} MB; normalizing…",
            duration_seconds=duration,
        )

        normalized_path = work_dir / "normalized.wav"
        await normalize_to_wav(original_path, normalized_path)

        updated = store.update(
            job.id,
            status=JobStatus.RUNNING,
            phase=JobPhase.INGEST,
            percent=100,
            message=f"Ingest complete — {duration:.1f}s normalized to 16 kHz mono WAV",
        )

        # Kick off transcribe → diarize → (polish → export, coming soon) in background.
        spawn(run_pipeline(job.id))
        return updated

    except HTTPException:
        store.cleanup(job.id)
        raise
    except Exception as e:
        store.update(job.id, status=JobStatus.ERROR, error=str(e))
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))


@router.get("/{job_id}", response_model=Job)
async def get_job(job_id: str) -> Job:
    job = get_store().get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


@router.get("/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    if get_store().get(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    bus = get_bus()

    async def gen():
        async for event in bus.subscribe(job_id):
            yield event.to_sse()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{job_id}/transcript")
async def get_transcript(job_id: str) -> JSONResponse:
    store = get_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    transcript_path = store.dir(job_id) / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Transcript not ready yet — subscribe to /events to wait for completion",
        )
    return JSONResponse(json.loads(transcript_path.read_text()))


@router.get("/{job_id}/polished")
async def get_polished(job_id: str) -> JSONResponse:
    store = get_store()
    if store.get(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    polished_path = store.dir(job_id) / "polished.json"
    if not polished_path.exists():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Polished transcript not ready yet",
        )
    return JSONResponse(json.loads(polished_path.read_text()))


@router.post("/{job_id}/repolish", response_model=Job)
async def repolish(job_id: str) -> Job:
    store = get_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if not (store.dir(job_id) / "transcript.json").exists():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Transcript not ready yet — can't re-polish",
        )
    get_bus().reset(job_id)
    updated = store.update(
        job_id,
        status=JobStatus.RUNNING,
        phase=JobPhase.POLISH,
        percent=0,
        message="Re-polishing…",
        error=None,
    )
    spawn(run_polish_only(job_id))
    return updated


@router.post("/{job_id}/pause", response_model=Job)
async def pause_job(job_id: str) -> Job:
    store = get_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status != JobStatus.RUNNING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Can only pause a running job (status was {job.status.value})",
        )
    store.control(job_id).pause()
    updated = store.update(job_id, status=JobStatus.PAUSED, message="Paused")
    get_bus().publish(job_id, ProgressEvent(
        phase=(updated.phase or JobPhase.TRANSCRIBE).value,
        percent=updated.percent,
        message="Paused — click Resume to continue",
        status="paused",
    ))
    return updated


@router.post("/{job_id}/resume", response_model=Job)
async def resume_job(job_id: str) -> Job:
    store = get_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status != JobStatus.PAUSED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Can only resume a paused job (status was {job.status.value})",
        )
    store.control(job_id).resume()
    updated = store.update(job_id, status=JobStatus.RUNNING, message="Resuming…")
    get_bus().publish(job_id, ProgressEvent(
        phase=(updated.phase or JobPhase.TRANSCRIBE).value,
        percent=updated.percent,
        message="Resumed",
        status="running",
    ))
    return updated


@router.post("/{job_id}/cancel", response_model=Job)
async def cancel_job(job_id: str) -> Job:
    store = get_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Job already finished (status: {job.status.value})",
        )
    # Signal the worker; it will publish the terminal 'cancelled' event itself.
    store.control(job_id).cancel()
    return store.update(job_id, message="Stopping…")


@router.get("/{job_id}/download/{kind}")
async def download_export(job_id: str, kind: str) -> FileResponse:
    if kind not in {"md", "pdf"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "kind must be 'md' or 'pdf'",
        )
    store = get_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    filename = job.export_md_filename if kind == "md" else job.export_pdf_filename
    if not filename:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{kind.upper()} not yet exported",
        )
    path = EXPORTS_DIR / filename
    if not path.exists():
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Export file missing on disk: {filename}",
        )
    media_type = "text/markdown" if kind == "md" else "application/pdf"
    return FileResponse(path, media_type=media_type, filename=filename)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str) -> None:
    store = get_store()
    if store.get(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    store.cleanup(job_id)
