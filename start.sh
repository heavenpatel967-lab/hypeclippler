#!/usr/bin/env bash
# Linux / macOS launcher. Needs python3 (3.10+) and ffmpeg installed.
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "Install ffmpeg first (apt install ffmpeg / brew install ffmpeg)"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q --disable-pip-version-check -r requirements.txt
exec python run.py
