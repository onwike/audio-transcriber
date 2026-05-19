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

    # ─── Warm-load models off the event loop ─────────────────────────
    # Only reached if preflight cleared. First run downloads ~3 GB.
    from app.transcribe import load_diarizer, load_whisper

    logger.info("Warming up models (first run downloads model weights — may take several minutes)…")
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
