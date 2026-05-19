# Audio Transcriber

A local webpage that ingests audio files and produces a polished transcript as both PDF and Markdown.

Local Whisper transcription (free) → optional pyannote speaker diarization → Claude API polish (cleanup, paragraphs, section headers, title, summary) → clean print-ready PDF via WeasyPrint.

```
Drop audio → polished PDF + MD in your browser, ~$0.30 per 10 hours of audio at default settings.
```

---

## Prerequisites

- **macOS or Linux** — Apple Silicon supported, GPU optional
- **Python 3.10+** — the codebase uses PEP 604 union syntax. macOS's default `python3` is 3.9 (Xcode CLT); install a newer one with `brew install python@3.12`.
- **Homebrew packages:** `ffmpeg`, `pango`
- **Anthropic API key** with non-zero credit at [console.anthropic.com](https://console.anthropic.com/settings/billing) (separate billing from any Claude.ai subscription)
- **Hugging Face account + token** for speaker diarization (optional — set `ENABLE_DIARIZATION=false` to skip)
- **~5 GB free disk** for one-time model weight downloads

---

## Setup

```bash
brew install ffmpeg pango
cp .env.example .env
# Edit .env — paste your ANTHROPIC_API_KEY and HUGGINGFACE_TOKEN
./run.sh
```

For diarization, before first run, accept the gating terms on both:

- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

Open http://127.0.0.1:7860 in your browser. First launch downloads ~3 GB of model weights and takes 5–10 minutes; subsequent launches start in seconds.

---

## How it works

1. **Upload** — multipart streamed to disk in 1 MB chunks (won't OOM on multi-GB files)
2. **Ingest** — ffmpeg normalizes any input to 16 kHz mono WAV
3. **Transcribe** — faster-whisper runs in a thread executor with live progress
4. **Diarize** *(optional)* — pyannote identifies speakers; the dominant speaker per Whisper segment is assigned by overlap
5. **Polish** — Claude is called with a cached system prompt; structured output via forced `tool_use` returns `{title, summary, sections[{header, paragraphs[]}]}`. Long transcripts (>~1 hr) are split on silence gaps and re-stitched.
6. **Export** — Markdown template → CommonMark HTML → WeasyPrint PDF with a serif title page, page numbers, and justified body text.

The pipeline is one background task; the UI subscribes to a Server-Sent Events stream and replays history on (re)connect, so refreshing mid-job doesn't lose progress.

---

## Customizing the models

Edit `.env`. All three Claude tiers work — the polish task is well within Haiku's capability for typical recordings.

```ini
# Cheapest, default. Excellent for clean studio audio.
CLAUDE_MODEL=claude-haiku-4-5-20251001
# Better at nuanced section boundaries and subtle ASR fixes (~5× cost).
# CLAUDE_MODEL=claude-sonnet-4-6-20251001
# Highest quality (~40× Haiku cost). Worth it for ambiguous, multi-speaker, technical content.
# CLAUDE_MODEL=claude-opus-4-7

# Whisper tier (local, free):
WHISPER_MODEL=large-v3  # ~3 GB, best accuracy
# WHISPER_MODEL=medium  # ~1.5 GB, ~2× faster, near-large quality on clean audio
# WHISPER_MODEL=small   # ~500 MB, much faster, noticeable accuracy drop

ENABLE_DIARIZATION=true  # set to false to skip speaker labels entirely
```

---

## Cost reference (per 10 hours of audio)

| Component | Cost |
|---|---|
| faster-whisper (local) | $0 |
| pyannote diarization (local) | $0 |
| Claude Haiku 4.5 polish | ~$0.30 |
| Claude Sonnet 4.6 polish | ~$2.50 |
| Claude Opus 4.7 polish | ~$13 |

Prompt caching on the polish system prompt cuts re-polish runs by ~90%.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web UI |
| POST | `/jobs` | Upload audio (multipart), kicks off pipeline |
| GET | `/jobs/{id}` | Job state |
| GET | `/jobs/{id}/events` | SSE progress stream |
| GET | `/jobs/{id}/transcript` | Raw transcript JSON (segments + word timestamps) |
| GET | `/jobs/{id}/polished` | Polished transcript JSON |
| POST | `/jobs/{id}/repolish` | Re-run polish + export from saved transcript |
| GET | `/jobs/{id}/download/{md\|pdf}` | Download exported file |
| DELETE | `/jobs/{id}` | Clean up tempdir |
| GET | `/health` | Liveness check |

---

## Testing checklist

Try these after first launch:

- **30-second voice memo** — verify upload, fast transcription, single-section output
- **45-minute lecture (one speaker)** — verify section headers appear at topic shifts
- **30-minute two-speaker interview** — verify diarization produces SPEAKER_00 / SPEAKER_01 labels
- **Accented or technical-vocabulary audio** — verify Claude's ASR fixes preserve meaning, mark ambiguous words with `[?]`
- **Multi-hour audio (>1 hr)** — verify chunking + stitching produces a single coherent title and one summary

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ANTHROPIC_API_KEY is not set` at startup | Copy `.env.example` to `.env` and fill in your key |
| `ffmpeg not found on PATH` | `brew install ffmpeg` |
| `WeasyPrint unavailable: libgobject-2.0-0` (or similar `OSError`) | `brew install pango` |
| `ENABLE_DIARIZATION=true but HUGGINGFACE_TOKEN is missing` | Add token to `.env`; accept gating at the two pyannote URLs |
| pyannote `Pipeline.from_pretrained` returns `None` | You haven't accepted the model gating terms yet — visit both URLs while logged into HF |
| `credit_balance_too_low` from Anthropic | Load funds at [console.anthropic.com → Plans & Billing](https://console.anthropic.com/settings/billing) |
| Slow transcription on Apple Silicon | Expected — CTranslate2 doesn't fully support MPS yet. Drop to `WHISPER_MODEL=medium` for ~2× speedup with minimal quality loss. |
| Browser shows "Connection lost" mid-job | SSE reconnects automatically; only `status=error` events actually fail the job |

---

## Project layout

```
audio-transcriber/
├── .env / .env.example         # config (.env gitignored)
├── requirements.txt
├── run.sh                      # one-command launcher
├── prompts/
│   └── polish.md               # Claude system prompt (cached)
└── app/
    ├── main.py                 # FastAPI app + lifespan + warm-load
    ├── config.py               # pydantic-settings
    ├── models.py               # Pydantic schemas
    ├── jobs.py                 # in-memory JobStore + per-job tempdir
    ├── events.py               # SSE pub/sub
    ├── audio.py                # ffmpeg / ffprobe wrappers
    ├── transcribe.py           # faster-whisper + pyannote
    ├── polish.py               # Claude API + tool_use + chunking
    ├── export.py               # MD render + WeasyPrint PDF
    ├── pipeline.py             # background orchestrator
    ├── routes/jobs.py          # HTTP API
    ├── templates/index.html
    ├── static/{app.css,app.js,print.css}
    ├── exports/                # generated MD/PDF (gitignored)
    └── temp/                   # per-job working dirs (gitignored)
```
