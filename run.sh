#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# 1. Find a Python ≥ 3.10 (codebase uses PEP 604 union syntax).
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
    major=${ver%.*}
    minor=${ver#*.}
    if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 10 ]; then
      PYTHON="$candidate"
      PYTHON_VER="$ver"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "ERROR: No Python ≥ 3.10 found on PATH."
  echo "  This codebase uses modern union syntax (str | None) which needs Python 3.10+."
  echo "  Install via Homebrew:"
  echo "      brew install python@3.12"
  echo "  Then re-run ./run.sh"
  exit 1
fi

# 2. If an existing .venv was built with an older Python, recreate it.
if [ -d .venv ]; then
  venv_ver=$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
  venv_major=${venv_ver%.*}
  venv_minor=${venv_ver#*.}
  if [ "${venv_major:-0}" -lt 3 ] || { [ "${venv_major:-0}" -eq 3 ] && [ "${venv_minor:-0}" -lt 10 ]; }; then
    echo "→ Existing .venv uses Python ${venv_ver} (need ≥ 3.10) — recreating…"
    rm -rf .venv
  fi
fi

# 3. Create venv if missing.
if [ ! -d .venv ]; then
  echo "→ Creating Python virtual environment with ${PYTHON} (${PYTHON_VER})…"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# 4. Install deps.
if [ ! -f .venv/.installed ]; then
  echo "→ Installing dependencies (first run takes ~5 min)…"
  pip install --quiet --upgrade pip
  pip install -r requirements.txt
  touch .venv/.installed
fi

# 5. .env
if [ ! -f .env ]; then
  echo "ERROR: .env not found."
  echo "  Run: cp .env.example .env"
  echo "  Then edit .env to add ANTHROPIC_API_KEY and HUGGINGFACE_TOKEN."
  exit 1
fi

# 6. System binaries.
for bin in ffmpeg ffprobe; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: $bin not found. Install with:  brew install ffmpeg"
    exit 1
  fi
done

# 7. Launch.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-7860}"
echo
echo "→ Audio Transcriber starting at http://${HOST}:${PORT}"
echo "  First launch downloads ~3 GB of Whisper + pyannote weights — be patient."
echo

exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
