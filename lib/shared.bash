#!/usr/bin/env bash
set -euo pipefail

PLUGIN_PREFIX="QDB_ARTIFACTS"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PLUGIN_DIR}/.venv"
ARTIFACTS_PY="${PLUGIN_DIR}/artifacts.py"

# Detect Python 3.
#   Linux/macOS: "python3" is on PATH.
#   Windows:     Packer installs Python to C:\Python<ver>-<bits>\ which may not
#                be on PATH. We search known locations as a fallback.
_find_python3() {
  # 1. Agent-provided path (set by packer via QDB_CICD_AGENT_PYTHON3)
  if [[ -n "${QDB_CICD_AGENT_PYTHON3:-}" ]] && [[ -x "${QDB_CICD_AGENT_PYTHON3}" ]]; then
    echo "${QDB_CICD_AGENT_PYTHON3}"
    return
  fi

  # 2. Try standard commands on PATH
  for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null \
       && "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,) else 1)" 2>/dev/null; then
      echo "$cmd"
      return
    fi
  done

  # 3. Windows fallback: scan well-known install directories (newest first)
  if [[ -n "${SYSTEMROOT:-}" ]]; then
    for dir in /c/Python3.*-64 /c/Python3.*-32 /c/Python3*; do
      if [[ -x "${dir}/python.exe" ]] \
         && "${dir}/python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,) else 1)" 2>/dev/null; then
        echo "${dir}/python.exe"
        return
      fi
    done
  fi
}

PYTHON3_CMD="$(_find_python3)"

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

# True when at least one of the named options is set for this step. Tests each
# variable itself: through plugin_read_config, echo would take a value such as
# "-n" for its own option and print nothing.
plugin_has_any() {
  local name var
  for name in "$@"; do
    var="BUILDKITE_PLUGIN_${PLUGIN_PREFIX}_${name}"
    if [[ -n "${!var:-}" ]]; then
      return 0
    fi
  done
  return 1
}

# One call per hook phase: "pre-command" or "post-command". What a phase does is
# decided in lib/cli.py from the step's options, not here.
run_artifacts_py() {
  "${VENV_BIN}/python" "${ARTIFACTS_PY}" "$@"
}
