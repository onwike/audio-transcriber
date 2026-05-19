from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

from app.models import JobCancelled

if TYPE_CHECKING:
    from app.jobs import JobControl

logger = logging.getLogger(__name__)

# Models the user can pick between in the UI. Order matters — it's the
# display order in the picker.
AVAILABLE_WHISPER_MODELS: list[str] = [
    "small",
    "medium",
    "large-v3-turbo",
    "large-v3",
]

_models: dict[str, object] = {}  # name → WhisperModel
_diarizer = None  # pyannote.audio.Pipeline


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[dict] = field(default_factory=list)


def predownload_whisper_models(names: list[str]) -> None:
    """Ensure model snapshots are present in the HF cache.

    Uses faster_whisper.utils.download_model so we don't have to maintain a
    short-name → HF repo map ourselves (different orgs publish different
    models — `large-v3-turbo` lives at mobiuslabsgmbh, not Systran). The
    library's internal map is canonical.

    Doesn't load into RAM — that happens lazily on first transcribe.
    download_model is idempotent: cached weights skip the network.
    """
    from faster_whisper.utils import download_model

    failures: list[tuple[str, str]] = []
    for name in names:
        logger.info("Pre-downloading Whisper model '%s'…", name)
        try:
            download_model(name)
        except Exception as e:
            # Don't crash startup — fall back to lazy-load on first use.
            logger.warning(
                "Pre-download failed for '%s' (%s). Will retry lazily on first use.",
                name, e,
            )
            failures.append((name, str(e)))

    if failures:
        logger.warning(
            "%d Whisper model(s) failed to pre-download: %s",
            len(failures), ", ".join(n for n, _ in failures),
        )
    else:
        logger.info("All Whisper model weights cached on disk")


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def get_or_load_whisper(name: str, device: str, compute_type: str):
    """Return a WhisperModel for `name`, loading into RAM on first use."""
    cached = _models.get(name)
    if cached is not None:
        return cached

    from faster_whisper import WhisperModel

    resolved_device = _resolve_device(device)
    logger.info("Loading faster-whisper '%s' on %s (%s)…", name, resolved_device, compute_type)
    model = WhisperModel(name, device=resolved_device, compute_type=compute_type)
    _models[name] = model
    logger.info("faster-whisper '%s' ready", name)
    return model


def load_whisper(model_size: str, device: str, compute_type: str) -> None:
    """Warm-load the default model. Kept as a stable name for lifespan callers."""
    get_or_load_whisper(model_size, device, compute_type)


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
    model,
    wav_path: Path,
    on_progress: Callable[[float], None],
    control: Optional["JobControl"] = None,
) -> tuple[list[Segment], dict]:
    if model is None:
        raise RuntimeError("Whisper model not loaded")
    segments_iter, info = model.transcribe(
        str(wav_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )
    total = info.duration or 0.0
    segments: list[Segment] = []
    for seg in segments_iter:
        # Cooperative pause/cancel checkpoint, evaluated once per segment.
        if control is not None:
            control.wait_if_paused()
            if control.is_cancelled():
                raise JobCancelled()
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
    model_name: str,
    on_progress: Optional[Callable[[float], None]] = None,
    control: Optional["JobControl"] = None,
) -> tuple[list[Segment], dict]:
    """Run Whisper transcription off-loop with the named model.

    The model is loaded into RAM on first use; cached for subsequent jobs.
    Progress callback fires on the event loop thread.
    """
    from app.config import get_settings
    s = get_settings()

    # Loading the model is itself blocking — run in the executor so we
    # don't pin the event loop on a cold cache hit.
    loop = asyncio.get_running_loop()
    model = await loop.run_in_executor(
        None, get_or_load_whisper, model_name, s.whisper_device, s.whisper_compute_type
    )

    user_cb = on_progress or (lambda p: None)

    def thread_safe_cb(p: float) -> None:
        loop.call_soon_threadsafe(user_cb, p)

    return await loop.run_in_executor(
        None, _sync_transcribe, model, wav_path, thread_safe_cb, control
    )


def _sync_diarize(wav_path: Path, expected_speakers: int | None = None):
    if _diarizer is None:
        raise RuntimeError("Diarizer not loaded")
    kwargs: dict = {}
    if expected_speakers and expected_speakers > 0:
        # Pinning both to the same count forces pyannote to that exact number.
        kwargs["min_speakers"] = expected_speakers
        kwargs["max_speakers"] = expected_speakers
    return _diarizer(str(wav_path), **kwargs)


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
    expected_speakers: int | None = None,
) -> list[Segment]:
    """Run pyannote and assign the dominant speaker (by overlap) to each Whisper segment.

    If expected_speakers is provided, pyannote is constrained to exactly that
    count — materially better than auto-detection on short or noisy audio.
    """
    if _diarizer is None:
        return segments

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_diarize, wav_path, expected_speakers)
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
