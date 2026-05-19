from __future__ import annotations

import asyncio
import json
from pathlib import Path

ACCEPTED_FORMATS: frozenset[str] = frozenset({
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".oga",
    ".opus", ".webm", ".mp4", ".aac", ".wma",
})


async def _run(*cmd: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def probe(path: Path) -> dict:
    code, out, err = await _run(
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    )
    if code != 0:
        raise ValueError(f"ffprobe failed: {err.decode(errors='replace').strip()}")
    return json.loads(out or b"{}")


async def get_duration(path: Path) -> float:
    info = await probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"could not read duration from {path.name}") from e


async def has_audio_stream(path: Path) -> bool:
    info = await probe(path)
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


async def normalize_to_wav(src: Path, dst: Path) -> None:
    """Convert any input to 16 kHz mono PCM WAV (faster-whisper's preferred input)."""
    code, _, err = await _run(
        "ffmpeg", "-y", "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(dst),
    )
    if code != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-5:]
        raise ValueError("ffmpeg normalize failed: " + " | ".join(tail))
