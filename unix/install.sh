#!/usr/bin/env bash
# Installs vault-ingest into a private Python environment (.venv) next to this folder. macOS and Linux.
# Run from a terminal:   bash unix/install.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

find_python() {
  for py in python3.13 python3.12 python3.11 python3; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$py"; return 0
    fi
  done
  return 1
}

if ! py="$(find_python)"; then
  echo "Python 3.11 or newer was not found."
  case "$(uname -s)" in
    Darwin) echo "Install it with 'brew install python@3.12' or from https://www.python.org/downloads/" ;;
    *)      echo "Install it with your package manager, e.g. 'sudo apt install python3.12 python3.12-venv python3-tk'" ;;
  esac
  exit 1
fi
if ! "$py" -c 'import tkinter' 2>/dev/null; then
  echo "Note: tkinter is missing, so the client picker popup cannot open."
  case "$(uname -s)" in
    Darwin) echo "      brew install python-tk@3.12 (match your Python version)" ;;
    *)      echo "      sudo apt install python3-tk  (or your distro's tk package)" ;;
  esac
fi

echo "Creating .venv with $py ..."
"$py" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[dev]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - open it and fill in VAULT_PATH and the AI settings."
fi

if [ "$(uname -s)" = "Linux" ]; then
  missing=""
  for tool in pactl notify-send; do command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"; done
  if [ -n "$missing" ]; then
    echo "Optional tools not found:$missing"
    echo "  pactl (pulseaudio-utils / pipewire-pulse) lets the companion see which app uses the microphone;"
    echo "  notify-send (libnotify-bin) shows notifications."
  fi
fi

echo "Running self-tests..."
if .venv/bin/python -m pytest -q; then echo "Installed OK."; fi
