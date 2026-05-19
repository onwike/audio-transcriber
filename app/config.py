from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-4-5-20251001"

    whisper_model: str = "large-v3"
    whisper_compute_type: str = "int8"
    whisper_device: str = "auto"

    enable_diarization: bool = True
    huggingface_token: str | None = None

    host: str = "127.0.0.1"
    port: int = 7860
    max_audio_mb: int = 2048


@lru_cache
def get_settings() -> Settings:
    return Settings()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = PROJECT_ROOT / "app" / "exports"
TEMP_DIR = PROJECT_ROOT / "app" / "temp"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
