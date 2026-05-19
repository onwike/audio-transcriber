from __future__ import annotations

import logging
from typing import Any

import httpx
from anthropic import AsyncAnthropic, APIError, APIStatusError

logger = logging.getLogger(__name__)

# All gated HF models that pyannote.audio's speaker-diarization-3.1 pipeline
# pulls in. Missing any one of these will fail mid-pipeline-load with a cryptic
# 401 — list them all here so the preflight reports them in a single pass.
REQUIRED_HF_MODELS: list[str] = [
    "pyannote/speaker-diarization-3.1",
    "pyannote/segmentation-3.0",
    "pyannote/speaker-diarization-community-1",
]


class PreflightFailure(Exception):
    """Hard, user-actionable preflight failure — startup aborts."""


class PreflightWarning(Exception):
    """Soft, transient preflight issue — startup proceeds, user is informed.

    Use this for upstream service flakes (5xx, network, rate limits) where
    the configuration is correct but the dependency is momentarily unhealthy.
    Local-only work (history, re-polish later, downloads) keeps working.
    """


# ── Individual checks ────────────────────────────────────────────────────


async def check_anthropic(api_key: str, model: str) -> None:
    """Validate the Anthropic key + chosen model + non-zero credit with a tiny call.

    Distinguishes configurational failures (bad key, model not found, no credit)
    from transient upstream issues (5xx, network, rate limit). Transient failures
    raise PreflightWarning so startup can proceed — the user can still browse
    history and re-polish later.
    """
    if not api_key:
        raise PreflightFailure(
            "ANTHROPIC_API_KEY is empty. Set it in .env "
            "(get a key at https://console.anthropic.com/settings/keys)."
        )

    # max_retries=5 to ride out short 529 (overloaded) waves — same as the
    # polish client. Default of 2 was killing preflight during Anthropic blips.
    client = AsyncAnthropic(api_key=api_key, max_retries=5)
    try:
        await client.messages.create(
            model=model,
            max_tokens=4,
            messages=[{"role": "user", "content": "ok"}],
        )
    except APIStatusError as e:
        status = getattr(e, "status_code", None)
        body = str(e)

        # ── Configurational failures: abort startup ─────────────────────
        if status == 401 or "authentication" in body.lower():
            raise PreflightFailure(
                "ANTHROPIC_API_KEY rejected by Anthropic. "
                "Generate a new key at https://console.anthropic.com/settings/keys "
                "and paste it into .env."
            )
        if "credit_balance_too_low" in body or "billing" in body.lower():
            raise PreflightFailure(
                "Anthropic credit balance too low to make API calls. "
                "Load funds at https://console.anthropic.com/settings/billing "
                "(Haiku 4.5 uses ~$0.30 per 10 hours of audio)."
            )
        if status == 404 or "not_found" in body.lower():
            raise PreflightFailure(
                f"Claude model '{model}' is not available for your account. "
                "Check CLAUDE_MODEL in .env — see "
                "https://docs.anthropic.com/en/docs/about-claude/models for valid IDs."
            )

        # ── Transient upstream issues: warn, allow startup ──────────────
        if status == 429:
            raise PreflightWarning(
                "Anthropic API rate-limited during preflight (HTTP 429). "
                "Startup will proceed; polish runs may be throttled momentarily. "
                "Existing transcripts remain accessible."
            )
        if status is not None and 500 <= status < 600:
            raise PreflightWarning(
                f"Anthropic API returned HTTP {status} during preflight "
                "(likely transient — the API was overloaded). Startup will proceed; "
                "new polish runs may fail until the API recovers. Existing "
                "transcripts, history, and re-polish remain accessible — re-polish "
                "the new ones once Anthropic recovers."
            )

        # Unknown status — abort to be safe
        raise PreflightFailure(
            f"Anthropic API rejected the test call: HTTP {status} — {body[:200]}"
        )

    except APIError as e:
        # Network/connectivity layer — treat as transient so local work continues
        raise PreflightWarning(
            f"Couldn't reach Anthropic API during preflight: {e}. Startup will "
            "proceed; transcript history and downloads remain accessible. "
            "Check connectivity if new polish runs fail."
        )


async def check_huggingface(token: str, models: list[str]) -> None:
    """Validate HF token + gating-accepted status for every required model."""
    if not token:
        raise PreflightFailure(
            "HUGGINGFACE_TOKEN is empty but ENABLE_DIARIZATION=true. "
            "Either disable diarization in .env, or get a Read token at "
            "https://huggingface.co/settings/tokens and paste it into .env."
        )

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        # 1. Token validity
        try:
            r = await client.get("https://huggingface.co/api/whoami-v2")
        except httpx.HTTPError as e:
            raise PreflightFailure(f"Could not reach huggingface.co: {e}")

        if r.status_code == 401:
            raise PreflightFailure(
                "HUGGINGFACE_TOKEN is invalid or revoked. "
                "Generate a new one at https://huggingface.co/settings/tokens."
            )
        if r.status_code != 200:
            raise PreflightFailure(f"HF whoami check failed: HTTP {r.status_code}")

        # 2. Gating acceptance per model
        not_accepted: list[str] = []
        unreachable: list[tuple[str, int]] = []
        for model in models:
            try:
                r = await client.get(f"https://huggingface.co/api/models/{model}")
            except httpx.HTTPError as e:
                raise PreflightFailure(f"Could not reach huggingface.co/{model}: {e}")
            if r.status_code in (401, 403):
                not_accepted.append(model)
            elif r.status_code == 404:
                # Either gating denies even existence, or the model id moved.
                not_accepted.append(model)
            elif r.status_code != 200:
                unreachable.append((model, r.status_code))

        if not_accepted:
            urls = "\n    ".join(f"https://huggingface.co/{m}" for m in not_accepted)
            raise PreflightFailure(
                "You haven't accepted the gating terms for these pyannote models:\n    "
                f"{urls}\n"
                "  Visit each URL while logged into HF and click "
                "'Agree and access repository'. Acceptance is instant."
            )
        if unreachable:
            tail = ", ".join(f"{m} (HTTP {c})" for m, c in unreachable)
            raise PreflightFailure(f"HF model checks returned unexpected status: {tail}")


def check_weasyprint() -> None:
    """Import WeasyPrint to surface missing Pango/Cairo before first export."""
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError) as e:
        raise PreflightFailure(
            f"WeasyPrint can't load its native dependencies: {e}\n"
            "  On macOS:   brew install pango\n"
            "  On Debian:  apt install libpango-1.0-0 libpangoft2-1.0-0"
        )


# ── Orchestrator ─────────────────────────────────────────────────────────


async def run_preflight(settings: Any) -> tuple[list[str], list[str]]:
    """Run every preflight check. Return (errors, warnings).

    Errors are blocking — startup aborts. Warnings are advisory — the user
    is informed but startup proceeds. We collect everything so the user
    sees every issue in one pass instead of fix-one-then-retry cycles.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        await check_anthropic(settings.anthropic_api_key, settings.claude_model)
    except PreflightFailure as e:
        errors.append(str(e))
    except PreflightWarning as e:
        warnings.append(str(e))

    if settings.enable_diarization:
        try:
            await check_huggingface(settings.huggingface_token or "", REQUIRED_HF_MODELS)
        except PreflightFailure as e:
            errors.append(str(e))
        except PreflightWarning as e:
            warnings.append(str(e))

    try:
        check_weasyprint()
    except PreflightFailure as e:
        errors.append(str(e))
    except PreflightWarning as e:
        warnings.append(str(e))

    return errors, warnings


def format_preflight_report(errors: list[str], warnings: list[str]) -> str:
    """Format collected errors and warnings into a single human-readable banner."""
    banner = "─" * 70
    sections: list[str] = []
    if errors:
        body = "\n\n".join(f"  ✗ {msg}" for msg in errors)
        sections.append(
            f"\n{banner}\n"
            f"  PREFLIGHT FAILED — {len(errors)} blocking issue(s). Fix and restart.\n"
            f"{banner}\n\n"
            f"{body}\n"
        )
    if warnings:
        body = "\n\n".join(f"  ⚠ {msg}" for msg in warnings)
        sections.append(
            f"\n{banner}\n"
            f"  PREFLIGHT WARNINGS — {len(warnings)} transient issue(s). Startup proceeds.\n"
            f"{banner}\n\n"
            f"{body}\n"
        )
    if sections:
        sections.append(banner)
    return "\n".join(sections)
