#!/bin/bash
set -euo pipefail

PLUGIN_PREFIX="QDB_ARTIFACTS"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PLUGIN_DIR}/.venv"
ARTIFACTS_PY="${PLUGIN_DIR}/lib/artifacts.py"

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
  "${VENV_DIR}/bin/python3" "${ARTIFACTS_PY}" "$@"
}
