#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${ROOT_DIR}/.venv-build"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install ".[build]"
"$VENV_DIR/bin/python" -m PyInstaller --onefile --name differ differ.py

OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH_NAME="$(uname -m)"
OUT_NAME="differ-${OS_NAME}-${ARCH_NAME}"

cp "dist/differ" "dist/${OUT_NAME}"
echo "Built binaries:"
echo "  dist/differ"
echo "  dist/${OUT_NAME}"
