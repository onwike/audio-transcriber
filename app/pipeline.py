from __future__ import annotations

import asyncio
import logging
from typing import Coroutine

from app.config import get_settings
from app.events import ProgressEvent, get_bus
from app.export import export_md_and_pdf
from app.jobs import get_store
from app.models import JobCancelled, JobPhase, JobStatus
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
    control = store.control(job_id)
    normalized = work_dir / "normalized.wav"
    transcript_path = work_dir / "transcript.json"

    def emit(phase: JobPhase, percent: int, message: str, status: str = "running") -> None:
        store.update(job_id, phase=phase, percent=percent, message=message)
        bus.publish(job_id, ProgressEvent(
            phase=phase.value, percent=percent, message=message, status=status,
        ))

    def check_cancel() -> None:
        if control.is_cancelled():
            raise JobCancelled()

    async with _pipeline_lock:
        try:
            check_cancel()

            # ── Transcribe ──────────────────────────────────────────────
            store.phase_start(job_id, JobPhase.TRANSCRIBE.value)
            emit(JobPhase.TRANSCRIBE, 0, "Loading audio into Whisper…")

            def on_progress(p: float) -> None:
                # Reserve last 5% for diarization
                pct = int(p * 95) if settings.enable_diarization else int(p * 100)
                emit(JobPhase.TRANSCRIBE, pct, f"Transcribing… {int(p * 100)}%")

            # Use the model the user picked at upload; fall back to env default.
            job_state = store.get(job_id)
            model_name = (job_state.whisper_model if job_state else None) or settings.whisper_model
            segments, info = await transcribe(
                normalized, model_name, on_progress=on_progress, control=control
            )
            check_cancel()
            lang = info.get("language") or "unknown"
            emit(
                JobPhase.TRANSCRIBE,
                95 if settings.enable_diarization else 100,
                f"Transcribed {len(segments)} segments (language: {lang})",
            )

            # ── Diarize (optional, atomic — not pause/cancel checkpoint inside) ──
            if settings.enable_diarization:
                emit(JobPhase.TRANSCRIBE, 96, "Identifying speakers…")
                expected = job_state.expected_speakers if job_state else None
                segments = await diarize_and_assign(
                    normalized, segments, expected_speakers=expected
                )
                check_cancel()
                speakers = sorted({s.speaker for s in segments if s.speaker})
                emit(
                    JobPhase.TRANSCRIBE,
                    100,
                    f"Found {len(speakers)} speaker(s)"
                    + (f": {', '.join(speakers)}" if speakers else ""),
                )

            save_transcript(transcript_path, segments, info)
            check_cancel()
            store.phase_end(job_id, JobPhase.TRANSCRIBE.value)

            # ── Polish ──────────────────────────────────────────────────
            def on_polish(pct: int, msg: str) -> None:
                emit(JobPhase.POLISH, pct, msg)

            store.phase_start(job_id, JobPhase.POLISH.value)
            emit(JobPhase.POLISH, 0, "Polishing transcript with Claude…")
            hints = [h.model_dump() for h in (job_state.speaker_hints if job_state else [])]
            polished = await polish(
                segments,
                on_progress=on_polish,
                control=control,
                speaker_hints=hints or None,
            )
            save_polished(work_dir / "polished.json", polished)
            emit(JobPhase.POLISH, 100, f"Polished: «{polished.title}»")
            check_cancel()
            store.phase_end(job_id, JobPhase.POLISH.value)

            # ── Export to MD + PDF ──────────────────────────────────────
            store.phase_start(job_id, JobPhase.EXPORT.value)
            emit(JobPhase.EXPORT, 0, "Rendering Markdown + PDF…")
            md_path, pdf_path = await asyncio.to_thread(export_md_and_pdf, polished)
            store.update(
                job_id,
                title=polished.title,
                export_md_filename=md_path.name,
                export_pdf_filename=pdf_path.name,
            )
            emit(JobPhase.EXPORT, 100, f"Exported {md_path.name} and {pdf_path.name}")
            store.phase_end(job_id, JobPhase.EXPORT.value)

            store.mark_completed(job_id)
            store.update(job_id, status=JobStatus.DONE)
            bus.publish(job_id, ProgressEvent(
                phase=JobPhase.EXPORT.value,
                percent=100,
                message=f"Done — «{polished.title}»",
                status="done",
            ))

        except JobCancelled:
            logger.info("Pipeline cancelled for job %s", job_id)
            current = store.get(job_id)
            phase = (current.phase if current else JobPhase.TRANSCRIBE) or JobPhase.TRANSCRIBE
            store.mark_completed(job_id)
            store.update(job_id, status=JobStatus.CANCELLED, message="Cancelled by user")
            bus.publish(job_id, ProgressEvent(
                phase=phase.value,
                percent=current.percent if current else 0,
                message="Cancelled",
                status="cancelled",
            ))

        except Exception as e:
            logger.exception("Pipeline failed for job %s", job_id)
            store.mark_completed(job_id)
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
    control = store.control(job_id)
    transcript_path = work_dir / "transcript.json"

    def emit(phase: JobPhase, percent: int, message: str, status: str = "running") -> None:
        store.update(job_id, phase=phase, percent=percent, message=message)
        bus.publish(job_id, ProgressEvent(
            phase=phase.value, percent=percent, message=message, status=status,
        ))

    async with _pipeline_lock:
        try:
            segments, _info = load_transcript(transcript_path)

            # Re-snapshot the polish model — if .env changed since the job was
            # first created, history should reflect what's actually being run now.
            from app.config import get_settings as _gs
            store.update(job_id, polish_model=_gs().claude_model)

            def on_polish(pct: int, msg: str) -> None:
                emit(JobPhase.POLISH, pct, msg)

            store.phase_start(job_id, JobPhase.POLISH.value)
            emit(JobPhase.POLISH, 0, "Re-polishing transcript with Claude…")
            job_state = store.get(job_id)
            hints = [h.model_dump() for h in (job_state.speaker_hints if job_state else [])]
            polished = await polish(
                segments,
                on_progress=on_polish,
                control=control,
                speaker_hints=hints or None,
            )
            save_polished(work_dir / "polished.json", polished)
            emit(JobPhase.POLISH, 100, f"Polished: «{polished.title}»")
            store.phase_end(job_id, JobPhase.POLISH.value)

            store.phase_start(job_id, JobPhase.EXPORT.value)
            emit(JobPhase.EXPORT, 0, "Re-rendering Markdown + PDF…")
            md_path, pdf_path = await asyncio.to_thread(export_md_and_pdf, polished)
            store.update(
                job_id,
                title=polished.title,
                export_md_filename=md_path.name,
                export_pdf_filename=pdf_path.name,
            )
            emit(JobPhase.EXPORT, 100, f"Exported {md_path.name} and {pdf_path.name}")
            store.phase_end(job_id, JobPhase.EXPORT.value)

            store.mark_completed(job_id)
            store.update(job_id, status=JobStatus.DONE)
            bus.publish(job_id, ProgressEvent(
                phase=JobPhase.EXPORT.value,
                percent=100,
                message=f"Done — «{polished.title}»",
                status="done",
            ))

        except JobCancelled:
            logger.info("Re-polish cancelled for job %s", job_id)
            current = store.get(job_id)
            phase = (current.phase if current else JobPhase.POLISH) or JobPhase.POLISH
            store.mark_completed(job_id)
            store.update(job_id, status=JobStatus.CANCELLED, message="Cancelled by user")
            bus.publish(job_id, ProgressEvent(
                phase=phase.value,
                percent=current.percent if current else 0,
                message="Cancelled",
                status="cancelled",
            ))

        except Exception as e:
            logger.exception("Re-polish failed for job %s", job_id)
            store.mark_completed(job_id)
            store.update(job_id, status=JobStatus.ERROR, error=str(e))
            bus.publish(job_id, ProgressEvent(
                phase=JobPhase.POLISH.value,
                percent=0,
                message="Re-polish failed",
                status="error",
                error=str(e),
            ))
