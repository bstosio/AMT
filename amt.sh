#!/usr/bin/env bash
# AMT – Automatic Movie Translator (macOS / Linux)
# Pierwsze uruchomienie tworzy środowisko w ~/.amt (wspólne z AMTSB) i instaluje biblioteki.
# Uruchomienie:  bash amt.sh   (albo: bash amt.sh "Film.mkv")
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMT_DIR="${AMT_HOME:-$HOME/.amt}"
PY="$AMT_DIR/venv/bin/python"

if [ ! -x "$PY" ]; then
  PYTHON="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
  if [ -z "$PYTHON" ]; then
    echo "Nie znaleziono Pythona 3. Zainstaluj Python 3.11 lub 3.12:"
    echo "  macOS: https://www.python.org/downloads/  (albo: brew install python@3.12 python-tk@3.12)"
    echo "  Linux: sudo apt install python3 python3-venv python3-tk"
    exit 1
  fi
  if ! "$PYTHON" -c "import tkinter" 2>/dev/null; then
    echo "Brak modułu tkinter (okienka). Linux: sudo apt install python3-tk   macOS/Homebrew: brew install python-tk"
    exit 1
  fi
  echo "Pierwsze uruchomienie – przygotowuję środowisko w $AMT_DIR …"
  mkdir -p "$AMT_DIR"
  "$PYTHON" -m venv "$AMT_DIR/venv"
fi

STAMP="$AMT_DIR/.amt-deps-$(cksum < "$DIR/requirements.txt" | cut -d' ' -f1)"
if [ ! -f "$STAMP" ]; then
  if [ ! -t 1 ] && [ "$(uname)" = "Linux" ]; then   # uruchomione z menu – instalację pokaż w terminalu
    for t in gnome-terminal konsole xfce4-terminal x-terminal-emulator xterm; do
      command -v "$t" >/dev/null 2>&1 || continue
      case "$t" in
        gnome-terminal) exec "$t" -- bash "$DIR/amt.sh" "$@" ;;
        xfce4-terminal) exec "$t" -x bash "$DIR/amt.sh" "$@" ;;
        *) exec "$t" -e bash "$DIR/amt.sh" "$@" ;;
      esac
    done
  fi
  echo "Instaluję biblioteki (kilka minut, tylko raz)…"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$DIR/requirements.txt"
  touch "$STAMP"
fi

if [ "$(uname)" = "Linux" ]; then   # skrót z ikoną w menu aplikacji (i obok skryptu)
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS"
  cat > "$APPS/amt.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AMT – Automatic Movie Translator
Comment=Tłumaczenie filmów na Twoim komputerze
Exec=bash "$DIR/amt.sh" %f
Icon=$DIR/assets/amt_256.png
Terminal=false
Categories=AudioVideo;Video;
MimeType=video/x-matroska;video/mp4;video/x-msvideo;video/quicktime;video/webm;
EOF
  cp "$APPS/amt.desktop" "$DIR/AMT.desktop" 2>/dev/null && chmod +x "$DIR/AMT.desktop" 2>/dev/null || true
fi

exec "$PY" "$DIR/amt.py" "$@"
