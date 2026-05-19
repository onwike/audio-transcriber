#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# 1. venv
if [ ! -d .venv ]; then
  echo "→ Creating Python virtual environment…"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# 2. deps
if [ ! -f .venv/.installed ]; then
  echo "→ Installing dependencies (first run takes ~5 min)…"
  pip install --quiet --upgrade pip
  pip install -r requirements.txt
  touch .venv/.installed
fi

# 3. .env
if [ ! -f .env ]; then
  echo "ERROR: .env not found."
  echo "  Run: cp .env.example .env"
  echo "  Then edit .env to add ANTHROPIC_API_KEY and HUGGINGFACE_TOKEN."
  exit 1
fi

# 4. system binaries
for bin in ffmpeg ffprobe; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: $bin not found. Install with:  brew install ffmpeg"
    exit 1
  fi
done

# 5. friendly URL
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-7860}"
echo
echo "→ Audio Transcriber starting at http://${HOST}:${PORT}"
echo "  First launch downloads ~3 GB of Whisper + pyannote weights — be patient."
echo

exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
