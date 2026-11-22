#!/bin/sh
# Create .venv with a native Python (arm64 on Apple Silicon, so OCR doesn't run under Rosetta).
set -e
cd "$(dirname "$0")"
PY=3.13
if [ "$(uname -s)" = "Darwin" ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  PY=cpython-3.13-macos-aarch64-none
fi
rm -rf .venv
uv venv --python "$PY" .venv
uv pip install --python .venv/bin/python -r requirements.txt
echo "Ready. Run: .venv/bin/python -m krillionhelper"
