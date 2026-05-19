from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

from anthropic import AsyncAnthropic

from app.config import PROJECT_ROOT, get_settings
from app.models import JobCancelled, PolishedSection, PolishedTranscript
from app.transcribe import Segment

if TYPE_CHECKING:
    from app.jobs import JobControl

logger = logging.getLogger(__name__)

# Smaller than the previous 60k single-shot budget — we always chunk now so
# the rolling-context loop fires at least once even on short audio, and so
# context gets refreshed every ~20 min of audio. Each chunk's Claude output
# also stays well under the 16k-token cap.
MAX_CHARS_PER_CHUNK = 30_000

POLISH_PROMPT_PATH = PROJECT_ROOT / "prompts" / "polish.md"


# ── Tool schemas ─────────────────────────────────────────────────────────

CHUNK_TOOL = {
    "name": "submit_chunk",
    "description": (
        "Submit the polished sections for this chunk AND updated running notes "
        "that will be passed as context to the next chunk."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "Polished sections covering this chunk only.",
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {"type": "string"},
                        "timestamp": {"type": "string"},
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
            "running_notes_update": {
                "type": "object",
                "description": (
                    "Cumulative notes after processing this chunk. These are passed "
                    "to the next chunk as context, so include everything established "
                    "so far — not just what's new in this chunk."
                ),
                "properties": {
                    "topic_summary": {
                        "type": "string",
                        "description": "1–3 sentences capturing everything discussed across all chunks so far.",
                    },
                    "key_terms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Proper nouns, technical terms, acronyms, product names, "
                            "and any other vocabulary likely to be misrecognized by ASR. "
                            "Union of prior terms + new ones introduced in this chunk."
                        ),
                    },
                    "speaker_notes": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Map of speaker label → short description, e.g. "
                            "{'SPEAKER_00': 'host, asking questions', "
                            "'SPEAKER_01': 'guest, Dr. Smith, neuroscientist'}."
                        ),
                    },
                    "open_threads": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Questions raised but not yet answered, or topics teed up "
                            "but not yet explored. Help the next chunk recognize when "
                            "an ongoing topic is being resumed vs. starting fresh."
                        ),
                    },
                },
                "required": ["topic_summary", "key_terms", "speaker_notes", "open_threads"],
            },
        },
        "required": ["sections", "running_notes_update"],
    },
}

STITCH_TOOL = {
    "name": "submit_stitch",
    "description": "Generate the final title and summary for the full transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["title", "summary"],
    },
}


# ── Running context that compounds across chunks ─────────────────────────


@dataclass
class RunningNotes:
    topic_summary: str = ""
    key_terms: list[str] = field(default_factory=list)
    speaker_notes: dict[str, str] = field(default_factory=dict)
    open_threads: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.topic_summary
            or self.key_terms
            or self.speaker_notes
            or self.open_threads
        )

    def to_context_block(self) -> str:
        if self.is_empty():
            return ""
        parts: list[str] = []
        if self.topic_summary:
            parts.append(f"Topic so far: {self.topic_summary}")
        if self.key_terms:
            parts.append(
                "Key terms / proper nouns (use these spellings): "
                + ", ".join(self.key_terms)
            )
        if self.speaker_notes:
            speakers = "; ".join(f"{k}: {v}" for k, v in self.speaker_notes.items())
            parts.append(f"Speakers: {speakers}")
        if self.open_threads:
            parts.append("Open threads: " + "; ".join(self.open_threads))
        return "\n".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────────


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


# ── Claude calls ─────────────────────────────────────────────────────────


async def _polish_chunk(
    formatted: str,
    chunk_idx: int,
    total_chunks: int,
    notes: RunningNotes,
) -> tuple[list[PolishedSection], RunningNotes]:
    """Polish one chunk with rolling context. Returns sections + updated notes."""
    s = get_settings()

    context = notes.to_context_block()
    if context:
        user_content = (
            f"This is chunk {chunk_idx} of {total_chunks}. Earlier chunks established the "
            f"context below — use it to correct misrecognized names/terms in this chunk, "
            f"maintain consistent speaker attribution, and continue ongoing topics gracefully.\n\n"
            f"<previous_context>\n{context}\n</previous_context>\n\n"
            f"<transcript>\n{formatted}\n</transcript>\n\n"
            f"Call submit_chunk with the polished sections AND the updated cumulative "
            f"running_notes_update (carry forward everything from previous_context, plus "
            f"anything new from this chunk)."
        )
    else:
        user_content = (
            f"This is chunk {chunk_idx} of {total_chunks} (the first one — no prior context). "
            f"Polish the transcript per the rules. Then in running_notes_update, capture "
            f"the foundational context (topic, key terms, speakers, open threads) that "
            f"subsequent chunks will build on.\n\n"
            f"<transcript>\n{formatted}\n</transcript>\n\n"
            f"Call submit_chunk."
        )

    response = await _client().messages.create(
        model=s.claude_model,
        max_tokens=16384,
        system=[{
            "type": "text",
            "text": _system_prompt(),
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[CHUNK_TOOL],
        tool_choice={"type": "tool", "name": "submit_chunk"},
        messages=[{"role": "user", "content": user_content}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_chunk":
            sections = [PolishedSection.model_validate(s) for s in block.input["sections"]]
            update = block.input["running_notes_update"]
            new_notes = RunningNotes(
                topic_summary=update.get("topic_summary", ""),
                key_terms=list(update.get("key_terms") or []),
                speaker_notes=dict(update.get("speaker_notes") or {}),
                open_threads=list(update.get("open_threads") or []),
            )
            return sections, new_notes

    raise RuntimeError(
        "Claude did not call submit_chunk — got: "
        + ", ".join(b.type for b in response.content)
    )


async def _final_stitch(notes: RunningNotes) -> tuple[str, str]:
    """Generate a title + 2-3 sentence summary from accumulated running notes."""
    s = get_settings()
    context = notes.to_context_block() or "(no context accumulated)"
    response = await _client().messages.create(
        model=s.claude_model,
        max_tokens=1024,
        tools=[STITCH_TOOL],
        tool_choice={"type": "tool", "name": "submit_stitch"},
        messages=[{"role": "user", "content": (
            "Below are the cumulative running notes built up across all chunks of a "
            "long transcript. Synthesize a single factual, descriptive title (under 80 "
            "chars, no clickbait) and a neutral 2–3 sentence summary covering the whole "
            "transcript. Call submit_stitch.\n\n"
            f"{context}"
        )}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_stitch":
            return block.input["title"], block.input["summary"]
    raise RuntimeError("Claude did not call submit_stitch")


# ── Public API ───────────────────────────────────────────────────────────


async def polish(
    segments: list[Segment],
    on_progress: Optional[Callable[[int, str], None]] = None,
    control: Optional["JobControl"] = None,
) -> PolishedTranscript:
    """Polish the transcript with rolling-context chunking.

    Always chunks (even short audio gets one iteration), so Claude's reasoning
    about names/terms/speakers compounds across the whole recording — this
    materially raises quality when using smaller Whisper models that produce
    more ASR errors.
    """
    cb = on_progress or (lambda pct, msg: None)

    def check_cancel() -> None:
        if control is not None and control.is_cancelled():
            raise JobCancelled()

    check_cancel()
    chunks = _split_segments(segments, MAX_CHARS_PER_CHUNK)
    if not chunks:
        return PolishedTranscript(title="(empty)", summary="No content.", sections=[])

    cb(5, f"Polishing {len(chunks)} chunk(s) with rolling context…")

    all_sections: list[PolishedSection] = []
    notes = RunningNotes()
    for i, chunk in enumerate(chunks, start=1):
        check_cancel()
        msg = (
            f"Polishing chunk {i}/{len(chunks)}"
            + (f" (carrying {len(notes.key_terms)} term(s) forward)…" if not notes.is_empty() else "…")
        )
        cb(int(5 + (i - 1) / len(chunks) * 85), msg)
        sections, notes = await _polish_chunk(
            _format_segments(chunk), chunk_idx=i, total_chunks=len(chunks), notes=notes
        )
        all_sections.extend(sections)

    check_cancel()
    cb(92, "Generating final title & summary from accumulated context…")
    title, summary = await _final_stitch(notes)
    check_cancel()

    result = PolishedTranscript(title=title, summary=summary, sections=all_sections)
    cb(100, f"Polished: «{result.title}»")
    return result


def save_polished(path: Path, polished: PolishedTranscript) -> None:
    path.write_text(polished.model_dump_json(indent=2))


def load_polished(path: Path) -> PolishedTranscript:
    return PolishedTranscript.model_validate_json(path.read_text())
