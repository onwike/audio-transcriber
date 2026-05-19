from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


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
    title: str | None = None  # populated when polish completes
    export_md_filename: str | None = None
    export_pdf_filename: str | None = None


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
