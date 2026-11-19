#!/usr/bin/env bash
set -euo pipefail

PLUGIN_PREFIX="QDB_ARTIFACTS"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS_PY="${PLUGIN_DIR}/artifacts.py"

# Git Bash, the shell of the hooks on the Windows agents.
on_windows() {
  [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || -n "${SYSTEMROOT:-}" ]]
}

# The Python the agent image names for its plugins, and no other:
# QDB_CICD_AGENT_PLUGIN_PYTHON. The Windows image names a Windows path
# (C:\Python3.13-64\python.exe), which cygpath turns into the form Git Bash runs.
agent_python() {
  local named="${QDB_CICD_AGENT_PLUGIN_PYTHON:-}"
  if [[ -n "${named}" ]] && on_windows; then
    cygpath -u "${named}"
  else
    printf '%s\n' "${named}"
  fi
}

# The version of a Python, as "3.13.1", or nothing when it does not run. Written
# without a newline: Python on Windows ends a line with CR LF, and the command
# substitution that reads the version strips the LF only.
python_version() {
  "$1" -c 'import platform, sys; sys.stdout.write(platform.python_version())' 2>/dev/null || true
}

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

# The gate steps of enter and resume read pipeline documents, which needs
# PyYAML. No other step reads one, so no other job on any platform installs it.
step_reads_documents() {
  plugin_has_any PLAN RESUME
}

# Every job of one plugin commit on a host shares this checkout, and the
# post-checkout of each makes its venv again. The steps that read documents
# keep a venv of their own, so that no other job takes PyYAML from under them.
if step_reads_documents; then
  VENV_DIR="${PLUGIN_DIR}/.venv-snapshot"
else
  VENV_DIR="${PLUGIN_DIR}/.venv"
fi

# Venv layout differs between platforms:
#   Linux/macOS: .venv/bin/python3, .venv/bin/pip
#   Windows:     .venv/Scripts/python.exe, .venv/Scripts/pip.exe
if on_windows; then
  VENV_BIN="${VENV_DIR}/Scripts"
else
  VENV_BIN="${VENV_DIR}/bin"
fi

# One call per hook phase: "pre-command" or "post-command". What a phase does is
# decided in lib/cli.py from the step's options, not here.
run_artifacts_py() {
  "${VENV_BIN}/python" "${ARTIFACTS_PY}" "$@"
}
