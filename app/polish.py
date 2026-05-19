from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from anthropic import AsyncAnthropic

from app.config import PROJECT_ROOT, get_settings
from app.models import PolishedTranscript
from app.transcribe import Segment

logger = logging.getLogger(__name__)

# ~1 hour of audio per chunk — keeps Claude's output well under the 16k-token cap.
MAX_CHARS_PER_CHUNK = 60_000

POLISH_PROMPT_PATH = PROJECT_ROOT / "prompts" / "polish.md"

POLISH_TOOL = {
    "name": "submit_polish",
    "description": "Submit the polished transcript with title, summary, and structured sections.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Factual descriptive title, under 80 characters.",
            },
            "summary": {
                "type": "string",
                "description": "Neutral 2–3 sentence summary of the transcript.",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {"type": "string"},
                        "timestamp": {
                            "type": "string",
                            "description": "Starting timestamp (mm:ss or h:mm:ss).",
                        },
                        "paragraphs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "timestamp": {"type": "string"},
                                    "speaker": {"type": ["string", "null"]},
                                    "text": {"type": "string"},
                                },
                                "required": ["timestamp", "text"],
                            },
                        },
                    },
                    "required": ["header", "timestamp", "paragraphs"],
                },
            },
        },
        "required": ["title", "summary", "sections"],
    },
}

STITCH_TOOL = {
    "name": "submit_stitch",
    "description": "Provide a unified title and summary for a multi-chunk transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["title", "summary"],
    },
}


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


def _format_segments(segments: list[Segment]) -> str:
    lines: list[str] = []
    for seg in segments:
        ts = _format_timestamp(seg.start)
        speaker = f"({seg.speaker}) " if seg.speaker else ""
        lines.append(f"[{ts}] {speaker}{seg.text}")
    return "\n".join(lines)


def _split_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Split into chunks, snapping each boundary to the largest silence gap in the trailing 20%."""
    if not segments:
        return []
    chunks: list[list[Segment]] = []
    cursor = 0
    while cursor < len(segments):
        end = cursor
        chars = 0
        while end < len(segments):
            cost = len(segments[end].text) + 30
            if chars + cost > max_chars and end > cursor:
                break
            chars += cost
            end += 1
        if end >= len(segments):
            chunks.append(segments[cursor:])
            break
        refine_start = max(cursor + 1, cursor + int((end - cursor) * 0.8))
        best_idx = end
        best_gap = -1.0
        for i in range(refine_start, end):
            gap = segments[i + 1].start - segments[i].end if i + 1 < len(segments) else 0.0
            if gap > best_gap:
                best_gap = gap
                best_idx = i + 1
        chunks.append(segments[cursor:best_idx])
        cursor = best_idx
    return chunks


def _system_prompt() -> str:
    return POLISH_PROMPT_PATH.read_text()


def _client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=get_settings().anthropic_api_key)


async def _polish_single(formatted: str) -> PolishedTranscript:
    s = get_settings()
    response = await _client().messages.create(
        model=s.claude_model,
        max_tokens=16384,
        system=[{
            "type": "text",
            "text": _system_prompt(),
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[POLISH_TOOL],
        tool_choice={"type": "tool", "name": "submit_polish"},
        messages=[{
            "role": "user",
            "content": (
                "Polish the following ASR transcript per the system rules. "
                "Submit your result by calling the submit_polish tool. "
                "Do not produce any other text.\n\n"
                f"<transcript>\n{formatted}\n</transcript>"
            ),
        }],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_polish":
            return PolishedTranscript.model_validate(block.input)
    raise RuntimeError(
        "Claude did not call submit_polish — got: "
        + ", ".join(b.type for b in response.content)
    )


async def _stitch(parts: list[PolishedTranscript]) -> PolishedTranscript:
    if len(parts) == 1:
        return parts[0]
    s = get_settings()
    bullets = "\n".join(f"- ({p.title}) {p.summary}" for p in parts)
    response = await _client().messages.create(
        model=s.claude_model,
        max_tokens=1024,
        tools=[STITCH_TOOL],
        tool_choice={"type": "tool", "name": "submit_stitch"},
        messages=[{"role": "user", "content": (
            "Below are chunk-level titles and summaries for one long transcript. "
            "Synthesize one overarching title (under 80 chars) and a neutral 2–3 sentence "
            "summary covering the whole transcript. Call submit_stitch.\n\n" + bullets
        )}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_stitch":
            return PolishedTranscript(
                title=block.input["title"],
                summary=block.input["summary"],
                sections=[sec for p in parts for sec in p.sections],
            )
    raise RuntimeError("Claude did not call submit_stitch")


async def polish(
    segments: list[Segment],
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> PolishedTranscript:
    cb = on_progress or (lambda pct, msg: None)
    formatted = _format_segments(segments)

    if len(formatted) <= MAX_CHARS_PER_CHUNK:
        cb(20, "Calling Claude (Opus 4.7)…")
        result = await _polish_single(formatted)
        cb(100, f"Polished: «{result.title}»")
        return result

    chunks = _split_segments(segments, MAX_CHARS_PER_CHUNK)
    cb(5, f"Transcript exceeds single-call budget; split into {len(chunks)} chunks")
    parts: list[PolishedTranscript] = []
    for i, chunk in enumerate(chunks):
        cb(int(5 + (i / len(chunks)) * 85), f"Polishing chunk {i + 1}/{len(chunks)}…")
        parts.append(await _polish_single(_format_segments(chunk)))

    cb(92, "Stitching chunk titles & summaries…")
    result = await _stitch(parts)
    cb(100, f"Polished: «{result.title}»")
    return result


def save_polished(path: Path, polished: PolishedTranscript) -> None:
    path.write_text(polished.model_dump_json(indent=2))


def load_polished(path: Path) -> PolishedTranscript:
    return PolishedTranscript.model_validate_json(path.read_text())
