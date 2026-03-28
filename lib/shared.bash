#!/bin/bash
set -euo pipefail

PLUGIN_PREFIX="QDB_ARTIFACTS"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PLUGIN_DIR}/.venv"
ARTIFACTS_PY="${PLUGIN_DIR}/lib/artifacts.py"

# Detect Python 3 — on Linux it's "python3", on Windows it's "python".
if command -v python3 &>/dev/null; then
  PYTHON3_CMD="python3"
elif command -v python &>/dev/null && python -c "import sys; sys.exit(0 if sys.version_info >= (3,) else 1)" 2>/dev/null; then
  PYTHON3_CMD="python"
else
  PYTHON3_CMD=""
fi

# Venv layout differs between platforms:
#   Linux/macOS: .venv/bin/python3, .venv/bin/pip
#   Windows:     .venv/Scripts/python.exe, .venv/Scripts/pip.exe
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || -n "${SYSTEMROOT:-}" ]]; then
  VENV_BIN="${VENV_DIR}/Scripts"
else
  VENV_BIN="${VENV_DIR}/bin"
fi

plugin_read_config() {
  local var="BUILDKITE_PLUGIN_${PLUGIN_PREFIX}_${1}"
  local default="${2:-}"
  echo "${!var:-$default}"
}

plugin_read_list() {
  local prefix="BUILDKITE_PLUGIN_${PLUGIN_PREFIX}_${1}"
  local i=0
  local parameter="${prefix}_${i}"
  if [[ -n "${!parameter:-}" ]]; then
    while [[ -n "${!parameter:-}" ]]; do
      echo "${!parameter}"
      i=$((i+1))
      parameter="${prefix}_${i}"
    done
  elif [[ -n "${!prefix:-}" ]]; then
    echo "${!prefix}"
  fi
}

run_artifacts_py() {
  "${VENV_BIN}/python" "${ARTIFACTS_PY}" "$@"
}
