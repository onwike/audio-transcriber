from __future__ import annotations

import asyncio
import logging
from typing import Coroutine

from app.config import get_settings
from app.events import ProgressEvent, get_bus
from app.export import export_md_and_pdf
from app.jobs import get_store
from app.models import JobPhase, JobStatus
from app.polish import polish, save_polished
from app.transcribe import (
    diarize_and_assign,
    load_transcript,
    save_transcript,
    transcribe,
)

logger = logging.getLogger(__name__)

# Single-user app: only one pipeline runs at a time.
_pipeline_lock = asyncio.Lock()
_running_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine) -> asyncio.Task:
    """Fire-and-forget task with a strong reference kept until completion."""
    task = asyncio.create_task(coro)
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)
    return task


async def run_pipeline(job_id: str) -> None:
    bus = get_bus()
    store = get_store()
    settings = get_settings()
    work_dir = store.dir(job_id)
    normalized = work_dir / "normalized.wav"
    transcript_path = work_dir / "transcript.json"

    def emit(phase: JobPhase, percent: int, message: str, status: str = "running") -> None:
        store.update(job_id, phase=phase, percent=percent, message=message)
        bus.publish(job_id, ProgressEvent(
            phase=phase.value, percent=percent, message=message, status=status,
        ))

    async with _pipeline_lock:
        try:
            # ── Transcribe ──────────────────────────────────────────────
            emit(JobPhase.TRANSCRIBE, 0, "Loading audio into Whisper…")

            def on_progress(p: float) -> None:
                # Reserve last 5% for diarization
                pct = int(p * 95) if settings.enable_diarization else int(p * 100)
                emit(JobPhase.TRANSCRIBE, pct, f"Transcribing… {int(p * 100)}%")

            segments, info = await transcribe(normalized, on_progress=on_progress)
            lang = info.get("language") or "unknown"
            emit(
                JobPhase.TRANSCRIBE,
                95 if settings.enable_diarization else 100,
                f"Transcribed {len(segments)} segments (language: {lang})",
            )

            # ── Diarize (optional) ──────────────────────────────────────
            if settings.enable_diarization:
                emit(JobPhase.TRANSCRIBE, 96, "Identifying speakers…")
                segments = await diarize_and_assign(normalized, segments)
                speakers = sorted({s.speaker for s in segments if s.speaker})
                emit(
                    JobPhase.TRANSCRIBE,
                    100,
                    f"Found {len(speakers)} speaker(s)"
                    + (f": {', '.join(speakers)}" if speakers else ""),
                )

            save_transcript(transcript_path, segments, info)

            # ── Polish ──────────────────────────────────────────────────
            def on_polish(pct: int, msg: str) -> None:
                emit(JobPhase.POLISH, pct, msg)

            emit(JobPhase.POLISH, 0, "Polishing transcript with Claude…")
            polished = await polish(segments, on_progress=on_polish)
            save_polished(work_dir / "polished.json", polished)
            emit(JobPhase.POLISH, 100, f"Polished: «{polished.title}»")

            # ── Export to MD + PDF ──────────────────────────────────────
            emit(JobPhase.EXPORT, 0, "Rendering Markdown + PDF…")
            md_path, pdf_path = await asyncio.to_thread(export_md_and_pdf, polished)
            store.update(
                job_id,
                export_md_filename=md_path.name,
                export_pdf_filename=pdf_path.name,
            )
            emit(JobPhase.EXPORT, 100, f"Exported {md_path.name} and {pdf_path.name}")

            store.update(job_id, status=JobStatus.DONE)
            bus.publish(job_id, ProgressEvent(
                phase=JobPhase.EXPORT.value,
                percent=100,
                message=f"Done — «{polished.title}»",
                status="done",
            ))

        except Exception as e:
            logger.exception("Pipeline failed for job %s", job_id)
            store.update(job_id, status=JobStatus.ERROR, error=str(e))
            bus.publish(job_id, ProgressEvent(
                phase=(store.get(job_id).phase or JobPhase.TRANSCRIBE).value,
                percent=0,
                message="Pipeline failed",
                status="error",
                error=str(e),
            ))


async def run_polish_only(job_id: str) -> None:
    """Re-run polish from saved transcript.json without re-running Whisper."""
    bus = get_bus()
    store = get_store()
    work_dir = store.dir(job_id)
    transcript_path = work_dir / "transcript.json"

    def emit(phase: JobPhase, percent: int, message: str, status: str = "running") -> None:
        store.update(job_id, phase=phase, percent=percent, message=message)
        bus.publish(job_id, ProgressEvent(
            phase=phase.value, percent=percent, message=message, status=status,
        ))

    async with _pipeline_lock:
        try:
            segments, _info = load_transcript(transcript_path)

            def on_polish(pct: int, msg: str) -> None:
                emit(JobPhase.POLISH, pct, msg)

            emit(JobPhase.POLISH, 0, "Re-polishing transcript with Claude…")
            polished = await polish(segments, on_progress=on_polish)
            save_polished(work_dir / "polished.json", polished)
            emit(JobPhase.POLISH, 100, f"Polished: «{polished.title}»")

            emit(JobPhase.EXPORT, 0, "Re-rendering Markdown + PDF…")
            md_path, pdf_path = await asyncio.to_thread(export_md_and_pdf, polished)
            store.update(
                job_id,
                export_md_filename=md_path.name,
                export_pdf_filename=pdf_path.name,
            )
            emit(JobPhase.EXPORT, 100, f"Exported {md_path.name} and {pdf_path.name}")

            store.update(job_id, status=JobStatus.DONE)
            bus.publish(job_id, ProgressEvent(
                phase=JobPhase.EXPORT.value,
                percent=100,
                message=f"Done — «{polished.title}»",
                status="done",
            ))

        except Exception as e:
            logger.exception("Re-polish failed for job %s", job_id)
            store.update(job_id, status=JobStatus.ERROR, error=str(e))
            bus.publish(job_id, ProgressEvent(
                phase=JobPhase.POLISH.value,
                percent=0,
                message="Re-polish failed",
                status="error",
                error=str(e),
            ))
