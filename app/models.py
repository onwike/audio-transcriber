from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class JobCancelled(Exception):
    """Raised at a worker checkpoint to abort the current pipeline cleanly."""


class JobPhase(str, Enum):
    INGEST = "ingest"
    TRANSCRIBE = "transcribe"
    POLISH = "polish"
    EXPORT = "export"


class SpeakerHint(BaseModel):
    name: str
    description: str = ""


class PhaseRun(BaseModel):
    """When a pipeline phase started and how long it ran.

    duration_seconds is None while the phase is still in progress (or if the
    pipeline crashed mid-phase). UI should treat None as 'unknown'.
    """
    started_at: datetime
    duration_seconds: float | None = None


class Job(BaseModel):
    id: str
    status: JobStatus = JobStatus.PENDING
    phase: JobPhase | None = None
    percent: int = 0
    message: str = ""
    error: str | None = None
    original_filename: str
    duration_seconds: float | None = None
    created_at: datetime
    whisper_model: str | None = None
    polish_model: str | None = None  # Claude model used for the polish pass
    expected_speakers: int | None = None  # constrains pyannote min/max
    speaker_hints: list[SpeakerHint] = Field(default_factory=list)
    title: str | None = None  # populated when polish completes
    export_md_filename: str | None = None
    export_pdf_filename: str | None = None
    phase_runs: dict[str, PhaseRun] = Field(default_factory=dict)  # per-phase timing
    completed_at: datetime | None = None  # when the pipeline finished (any outcome)


class PolishedParagraph(BaseModel):
    timestamp: str
    speaker: str | None = None
    text: str


class PolishedSection(BaseModel):
    header: str
    timestamp: str
    paragraphs: list[PolishedParagraph]


class PolishedTranscript(BaseModel):
    title: str
    summary: str
    sections: list[PolishedSection]
