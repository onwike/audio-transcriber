from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_model = None  # faster_whisper.WhisperModel
_diarizer = None  # pyannote.audio.Pipeline


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[dict] = field(default_factory=list)


def load_whisper(model_size: str, device: str, compute_type: str) -> None:
    global _model
    if _model is not None:
        return

    from faster_whisper import WhisperModel

    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"

    logger.info("Loading faster-whisper %s on %s (%s)…", model_size, device, compute_type)
    _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    logger.info("faster-whisper ready")


def load_diarizer(token: str) -> None:
    global _diarizer
    if _diarizer is not None:
        return

    import torch
    from pyannote.audio import Pipeline

    logger.info("Loading pyannote/speaker-diarization-3.1…")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=token,
    )
    if pipeline is None:
        raise RuntimeError(
            "pyannote pipeline returned None. Check HUGGINGFACE_TOKEN and that you've accepted "
            "gating at the URLs in .env.example."
        )
    if torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))
    elif torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))
    _diarizer = pipeline
    logger.info("pyannote ready")


def _sync_transcribe(
    wav_path: Path,
    on_progress: Callable[[float], None],
) -> tuple[list[Segment], dict]:
    if _model is None:
        raise RuntimeError("Whisper model not loaded")
    segments_iter, info = _model.transcribe(
        str(wav_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )
    total = info.duration or 0.0
    segments: list[Segment] = []
    for seg in segments_iter:
        segments.append(Segment(
            start=float(seg.start),
            end=float(seg.end),
            text=(seg.text or "").strip(),
            words=[
                {"start": float(w.start), "end": float(w.end), "word": w.word}
                for w in (seg.words or [])
            ],
        ))
        if total > 0:
            on_progress(min(1.0, seg.end / total))
    return segments, {
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
    }


async def transcribe(
    wav_path: Path,
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[list[Segment], dict]:
    """Run Whisper transcription off-loop. Progress callback fires on event loop thread."""
    loop = asyncio.get_running_loop()
    user_cb = on_progress or (lambda p: None)

    def thread_safe_cb(p: float) -> None:
        loop.call_soon_threadsafe(user_cb, p)

    return await loop.run_in_executor(None, _sync_transcribe, wav_path, thread_safe_cb)


def _sync_diarize(wav_path: Path):
    if _diarizer is None:
        raise RuntimeError("Diarizer not loaded")
    return _diarizer(str(wav_path))


def _annotation_from_diarizer_result(result):
    """Extract the pyannote Annotation regardless of pyannote.audio version.

    pyannote < 3.4: pipeline(wav) returns an Annotation directly.
    pyannote >= 3.4: returns a DiarizeOutput dataclass; the Annotation
    lives on `.speaker_diarization` (with `.diarization` and `.output`
    as historical fallbacks).
    """
    if hasattr(result, "itertracks"):
        return result
    for attr in ("speaker_diarization", "diarization", "output"):
        inner = getattr(result, attr, None)
        if inner is not None and hasattr(inner, "itertracks"):
            return inner
    public_attrs = [a for a in dir(result) if not a.startswith("_")]
    raise RuntimeError(
        f"Cannot extract Annotation from pyannote result of type "
        f"{type(result).__name__}. Public attrs: {public_attrs}. "
        "This is likely a pyannote.audio version mismatch — please file an issue "
        "with the type name above."
    )


async def diarize_and_assign(
    wav_path: Path,
    segments: list[Segment],
) -> list[Segment]:
    """Run pyannote and assign the dominant speaker (by overlap) to each Whisper segment."""
    if _diarizer is None:
        return segments

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_diarize, wav_path)
    annotation = _annotation_from_diarizer_result(result)

    turns = [
        (turn.start, turn.end, label)
        for turn, _, label in annotation.itertracks(yield_label=True)
    ]

    for seg in segments:
        overlap: dict[str, float] = {}
        for t_start, t_end, label in turns:
            ov = max(0.0, min(seg.end, t_end) - max(seg.start, t_start))
            if ov > 0:
                overlap[label] = overlap.get(label, 0.0) + ov
        if overlap:
            seg.speaker = max(overlap, key=lambda k: overlap[k])
    return segments


def save_transcript(path: Path, segments: list[Segment], info: dict) -> None:
    path.write_text(json.dumps({
        "info": info,
        "segments": [asdict(s) for s in segments],
    }, indent=2))


def load_transcript(path: Path) -> tuple[list[Segment], dict]:
    data = json.loads(path.read_text())
    return [Segment(**s) for s in data["segments"]], data["info"]
