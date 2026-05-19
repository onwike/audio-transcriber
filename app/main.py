from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import EXPORTS_DIR, PROJECT_ROOT, get_settings
from app.preflight import format_preflight_report, run_preflight
from app.routes.jobs import router as jobs_router

STATIC_DIR = PROJECT_ROOT / "app" / "static"
TEMPLATES_DIR = PROJECT_ROOT / "app" / "templates"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app")


class _SuppressPollingFilter(logging.Filter):
    """Drop the noisy GET /jobs (history polling) and GET /static/ access
    log lines. The history view polls every 3 seconds while jobs are in
    flight, which floods the terminal with otherwise-meaningless 200s.
    Errors, POSTs, and one-shot endpoints (/jobs/{id}, /events, etc.) are
    still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Match GET /jobs HTTP/* (no path segment after /jobs) and GET /static/*
        if '"GET /jobs HTTP/' in msg:
            return False
        if '"GET /static/' in msg:
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_SuppressPollingFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()

    # ─── Cheap static checks (no network, no downloads) ──────────────
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError(
            "ffmpeg/ffprobe not found on PATH. Install with: brew install ffmpeg"
        )

    # ─── Preflight: credentials, gating, native libs ─────────────────
    # Runs before any heavy model download so the user sees every
    # actionable issue in one pass instead of fix-one-then-retry cycles.
    logger.info("Running preflight checks (Anthropic, HuggingFace, WeasyPrint)…")
    errors = await run_preflight(s)
    if errors:
        logger.error(format_preflight_report(errors))
        raise RuntimeError(
            f"Preflight failed: {len(errors)} issue(s). See report above."
        )
    logger.info("Preflight passed ✓")

    # ─── Rebuild job index from disk (history) ───────────────────────
    from app.jobs import get_store

    loaded = get_store().load_from_disk()
    if loaded:
        logger.info("Loaded %d historical job(s) from disk", loaded)

    # ─── Pre-download all togglable Whisper models ───────────────────
    # Snapshot weights to disk so the user can pick any model in the UI
    # without waiting on a mid-pipeline download. Lazy-loaded into RAM
    # on first use. Idempotent — re-runs are fast if cache is warm.
    from app.transcribe import (
        AVAILABLE_WHISPER_MODELS,
        load_diarizer,
        load_whisper,
        predownload_whisper_models,
    )

    logger.info(
        "Pre-downloading Whisper models: %s (first run only — ~6.6 GB total, may take several minutes)…",
        ", ".join(AVAILABLE_WHISPER_MODELS),
    )
    await asyncio.to_thread(predownload_whisper_models, AVAILABLE_WHISPER_MODELS)

    # Warm-load the default into RAM so the first transcription is fast.
    logger.info("Warm-loading default Whisper model into RAM: %s", s.whisper_model)
    await asyncio.to_thread(load_whisper, s.whisper_model, s.whisper_device, s.whisper_compute_type)

    if s.enable_diarization:
        await asyncio.to_thread(load_diarizer, s.huggingface_token)
    logger.info("Models loaded; server ready")

    yield


app = FastAPI(title="Audio Transcriber", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/exports", StaticFiles(directory=EXPORTS_DIR), name="exports")
app.include_router(jobs_router)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
