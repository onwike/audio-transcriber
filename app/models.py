from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"  # waiting for the pipeline lock — another job is running
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


class ErrorSource(str, Enum):
    """Where an error came from — drives attribution + the right help links."""
    ANTHROPIC = "anthropic"
    HUGGINGFACE = "huggingface"
    FFMPEG = "ffmpeg"
    WEASYPRINT = "weasyprint"
    NETWORK = "network"
    USER = "user"           # bad input file, wrong format, etc.
    INTERNAL = "internal"   # our bug — fallback


class ErrorLink(BaseModel):
    label: str
    url: str


class JobError(BaseModel):
    """Structured error attribution shown in the UI.

    `raw` carries the original technical message for debugging; `title`/`detail`
    are what the user reads; `links` point to provider status pages, docs, or
    config files relevant to the failure mode.
    """
    source: ErrorSource = ErrorSource.INTERNAL
    title: str
    detail: str
    links: list[ErrorLink] = Field(default_factory=list)
    raw: str | None = None


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
    error_details: JobError | None = None  # structured error (populated on new jobs)


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
