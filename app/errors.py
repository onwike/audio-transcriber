"""Classify pipeline exceptions into provider-attributed structured errors.

The UI surfaces these so users know whether the failure is on their side
(config / setup), an upstream provider (Anthropic, HuggingFace), or a local
dependency (ffmpeg, WeasyPrint). Each classification includes the right
documentation / status / fix links.
"""
from __future__ import annotations

from anthropic import APIError, APIStatusError

from app.models import ErrorLink, ErrorSource, JobError

# ── Link constants ──────────────────────────────────────────────────────

ANTHROPIC_STATUS = ErrorLink(
    label="Anthropic status",
    url="https://status.anthropic.com",
)
ANTHROPIC_DOCS = ErrorLink(
    label="Anthropic API docs",
    url="https://docs.anthropic.com/en/api",
)
ANTHROPIC_KEYS = ErrorLink(
    label="Manage API keys",
    url="https://console.anthropic.com/settings/keys",
)
ANTHROPIC_BILLING = ErrorLink(
    label="Anthropic billing",
    url="https://console.anthropic.com/settings/billing",
)
ANTHROPIC_MODELS = ErrorLink(
    label="Available Claude models",
    url="https://docs.anthropic.com/en/docs/about-claude/models",
)
ANTHROPIC_RATE_LIMITS = ErrorLink(
    label="Rate limits explained",
    url="https://docs.anthropic.com/en/api/rate-limits",
)

HF_STATUS = ErrorLink(
    label="HuggingFace status",
    url="https://status.huggingface.co",
)
HF_DOCS = ErrorLink(
    label="HuggingFace docs",
    url="https://huggingface.co/docs",
)
HF_TOKENS = ErrorLink(
    label="Manage HF tokens",
    url="https://huggingface.co/settings/tokens",
)

PYANNOTE_GATING = [
    ErrorLink(
        label="Accept speaker-diarization-3.1 terms",
        url="https://huggingface.co/pyannote/speaker-diarization-3.1",
    ),
    ErrorLink(
        label="Accept segmentation-3.0 terms",
        url="https://huggingface.co/pyannote/segmentation-3.0",
    ),
    ErrorLink(
        label="Accept community-1 terms",
        url="https://huggingface.co/pyannote/speaker-diarization-community-1",
    ),
]

FFMPEG_DOWNLOAD = ErrorLink(
    label="ffmpeg installation",
    url="https://ffmpeg.org/download.html",
)
FFMPEG_FORMATS = ErrorLink(
    label="ffmpeg supported formats",
    url="https://ffmpeg.org/general.html#File-Formats",
)

WEASYPRINT_INSTALL = ErrorLink(
    label="WeasyPrint installation guide",
    url="https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation",
)

REPO_ISSUES = ErrorLink(
    label="Report a bug on GitHub",
    url="https://github.com/onwike/audio-transcriber/issues",
)


# ── Classifier ──────────────────────────────────────────────────────────


def classify_error(e: Exception) -> JobError:
    """Map an exception to a user-actionable JobError with proper attribution.

    The order of checks matters: more specific patterns (Anthropic status codes,
    pyannote keywords) should come before generic fallbacks.
    """
    raw = str(e)

    # ── Anthropic API ────────────────────────────────────────────────
    if isinstance(e, APIStatusError):
        status = getattr(e, "status_code", None)
        body = raw.lower()

        if status == 529 or "overloaded" in body:
            return JobError(
                source=ErrorSource.ANTHROPIC,
                title="Anthropic API is overloaded (provider issue)",
                detail=(
                    "Anthropic's API returned HTTP 529 (overloaded) after retries. "
                    "This is a transient issue on Anthropic's side — not a problem with "
                    "your audio, your config, or this app. Your transcript is saved on "
                    "disk; click Polish to retry once their API recovers (usually "
                    "within a few minutes). Check the status page below for known issues."
                ),
                links=[ANTHROPIC_STATUS, ANTHROPIC_DOCS],
                raw=raw,
            )

        if status == 429:
            return JobError(
                source=ErrorSource.ANTHROPIC,
                title="Anthropic API rate limit hit (provider issue)",
                detail=(
                    "Anthropic rate-limited the request. Heavy concurrent usage or "
                    "rapid sequential polish runs can trigger this. Wait a moment "
                    "and click Polish to retry."
                ),
                links=[ANTHROPIC_RATE_LIMITS, ANTHROPIC_STATUS],
                raw=raw,
            )

        if status == 401 or "authentication" in body:
            return JobError(
                source=ErrorSource.ANTHROPIC,
                title="Anthropic API key rejected (configuration issue)",
                detail=(
                    "Your ANTHROPIC_API_KEY was rejected. The key may be invalid, "
                    "revoked, or for the wrong account. Generate a new key and update "
                    "your .env file, then restart the server."
                ),
                links=[ANTHROPIC_KEYS, ANTHROPIC_DOCS],
                raw=raw,
            )

        if "credit_balance_too_low" in body or "billing" in body:
            return JobError(
                source=ErrorSource.ANTHROPIC,
                title="Anthropic credit balance too low (account issue)",
                detail=(
                    "Your Anthropic account doesn't have enough credit to make API "
                    "calls. Load funds and click Polish to retry. Haiku 4.5 uses "
                    "~$0.30 per 10 hours of audio polish."
                ),
                links=[ANTHROPIC_BILLING],
                raw=raw,
            )

        if status == 404 or "not_found" in body:
            return JobError(
                source=ErrorSource.ANTHROPIC,
                title="Claude model not available (configuration issue)",
                detail=(
                    "The Claude model specified in CLAUDE_MODEL isn't available for "
                    "your account. Check the available models list and update .env."
                ),
                links=[ANTHROPIC_MODELS, ANTHROPIC_DOCS],
                raw=raw,
            )

        # Generic Anthropic 4xx / 5xx
        category = "client error" if (status and 400 <= status < 500) else "server error"
        return JobError(
            source=ErrorSource.ANTHROPIC,
            title=f"Anthropic API {category} (HTTP {status})",
            detail=(
                "Anthropic returned an unexpected error. Your transcript is saved — "
                "try Polish again. If it persists, check the status page and the "
                "raw error detail below."
            ),
            links=[ANTHROPIC_STATUS, ANTHROPIC_DOCS],
            raw=raw,
        )

    if isinstance(e, APIError):
        # Network/transport layer error reaching Anthropic
        return JobError(
            source=ErrorSource.NETWORK,
            title="Couldn't reach Anthropic API (network issue)",
            detail=(
                "A network error prevented contacting Anthropic. Check your internet "
                "connection. Your transcript is saved — try Polish once connectivity "
                "is restored."
            ),
            links=[ANTHROPIC_STATUS],
            raw=raw,
        )

    # ── HuggingFace / pyannote ───────────────────────────────────────
    low = raw.lower()
    if "pyannote" in low or "huggingface" in low or "hugging face" in low or "hf_token" in low:
        if "gated" in low or "agree" in low or "accept" in low or "access" in low:
            return JobError(
                source=ErrorSource.HUGGINGFACE,
                title="Pyannote model access not granted (one-time setup)",
                detail=(
                    "You haven't accepted the gating terms for one of the pyannote "
                    "models. Visit each URL below while logged into HuggingFace and "
                    "click 'Agree and access repository'. Acceptance is instant."
                ),
                links=[*PYANNOTE_GATING, HF_TOKENS],
                raw=raw,
            )
        return JobError(
            source=ErrorSource.HUGGINGFACE,
            title="HuggingFace / pyannote error (provider or setup issue)",
            detail=(
                "The speaker-diarization step failed. Check your HF token is valid "
                "and the pyannote gating terms are accepted (links below). If both "
                "look right, check HuggingFace's status page."
            ),
            links=[HF_TOKENS, *PYANNOTE_GATING, HF_STATUS],
            raw=raw,
        )

    # ── FFmpeg ───────────────────────────────────────────────────────
    if "ffmpeg" in low or "ffprobe" in low:
        return JobError(
            source=ErrorSource.FFMPEG,
            title="ffmpeg failed to process the audio",
            detail=(
                "ffmpeg couldn't decode or normalize your audio file. The file may "
                "be corrupt, in an unusual format, or partially downloaded. Try "
                "re-uploading. If it persists, check that ffmpeg is installed "
                "correctly (`brew install ffmpeg` on macOS)."
            ),
            links=[FFMPEG_DOWNLOAD, FFMPEG_FORMATS],
            raw=raw,
        )

    # ── WeasyPrint ──────────────────────────────────────────────────
    if "weasyprint" in low or "pango" in low or "cairo" in low or "libgobject" in low:
        return JobError(
            source=ErrorSource.WEASYPRINT,
            title="PDF rendering failed (local dependency issue)",
            detail=(
                "WeasyPrint couldn't render the PDF. This almost always means Pango "
                "or Cairo isn't installed. On macOS: `brew install pango`. The "
                "Markdown export is unaffected — you can download it from history."
            ),
            links=[WEASYPRINT_INSTALL],
            raw=raw,
        )

    # ── Bad audio / user input ──────────────────────────────────────
    if "no audio stream" in low or "unsupported format" in low:
        return JobError(
            source=ErrorSource.USER,
            title="Audio file rejected",
            detail=(
                "The uploaded file didn't contain a recognizable audio stream, "
                "or its format isn't supported. Try a different file or re-export "
                "from your source as mp3/wav/m4a."
            ),
            links=[FFMPEG_FORMATS],
            raw=raw,
        )

    # ── Fallback: internal/unknown ──────────────────────────────────
    return JobError(
        source=ErrorSource.INTERNAL,
        title="Internal application error",
        detail=(
            "An unexpected error occurred in the pipeline. This is most likely a "
            "bug in this app rather than an upstream provider issue. Check the "
            "server logs for the full traceback, then file an issue with the raw "
            "detail below."
        ),
        links=[REPO_ISSUES],
        raw=raw,
    )
