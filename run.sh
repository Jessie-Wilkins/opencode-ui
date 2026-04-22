#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

UPSTREAM_URL="${OPENCODE_SERVER_URL:-http://127.0.0.1:4096}"
VENV_DIR="${OPENCODE_VENV_DIR:-.venv}"
SERVER_LOG_DIR="${OPENCODE_SERVER_LOG_DIR:-$HOME/.cache/opencode-web}"
SERVER_LOG_FILE="${OPENCODE_SERVER_LOG_FILE:-$SERVER_LOG_DIR/opencode-server.log}"
AUTO_INSTALL_OPENCODE="${OPENCODE_AUTO_INSTALL:-1}"
AUTO_START_SERVER="${OPENCODE_AUTO_START_SERVER:-1}"
SETTINGS_FILE="${OPENCODE_WEB_SETTINGS_FILE:-$PWD/settings.json}"
export OPENCODE_CONFIG="${OPENCODE_CONFIG:-$PWD/opencode.json}"
export OPENCODE_CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$PWD/.opencode}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

load_saved_settings() {
  if [[ -f "$SETTINGS_FILE" ]]; then
    eval "$("$PYTHON_BIN" - "$SETTINGS_FILE" <<'PY'
import json
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
mapping = {
    "OPENCODE_SERVER_URL": data.get("upstream_url"),
    "OPENCODE_SERVER_USERNAME": data.get("upstream_username"),
    "OPENCODE_SERVER_PASSWORD": data.get("upstream_password"),
    "OLLAMA_BASE_URL": data.get("ollama_url"),
    "OPENCODE_MODEL": data.get("default_model"),
    "OPENCODE_PROXY_TIMEOUT": data.get("proxy_timeout"),
    "OPENCODE_SERVER_START_TIMEOUT": data.get("server_start_timeout"),
    "OPENCODE_INSTALL_URL": data.get("install_url"),
    "OPENCODE_INSTALL_VERSION": data.get("install_version"),
    "OPENCODE_AUTO_INSTALL": "1" if data.get("auto_install_opencode", True) else "0",
    "OPENCODE_AUTO_START_SERVER": "1" if data.get("auto_start_server", True) else "0",
}
for key, value in mapping.items():
    if value is None or value == "":
        continue
    print(f"export {key}={shlex.quote(str(value))}")
PY
)"
  fi
}

load_saved_settings

UPSTREAM_URL="${OPENCODE_SERVER_URL:-http://127.0.0.1:4096}"
UPSTREAM_URL="${UPSTREAM_URL%/}"
AUTO_INSTALL_OPENCODE="${OPENCODE_AUTO_INSTALL:-1}"
AUTO_START_SERVER="${OPENCODE_AUTO_START_SERVER:-1}"
OPENCODE_INSTALL_URL="${OPENCODE_INSTALL_URL:-https://opencode.ai/install}"

ensure_python_env() {
  local python_bin="$1"
  local venv_python="$VENV_DIR/bin/python"

  if [[ -x "$venv_python" ]]; then
    "$venv_python" -m pip install --upgrade pip >&2
    "$venv_python" -m pip install -r requirements.txt >&2
    printf '%s\n' "$venv_python"
    return 0
  fi

  if "$python_bin" -m venv "$VENV_DIR" >/dev/null 2>&1; then
    "$venv_python" -m pip install --upgrade pip >&2
    "$venv_python" -m pip install -r requirements.txt >&2
    printf '%s\n' "$venv_python"
    return 0
  fi

  "$python_bin" -m pip install --user --upgrade pip >&2
  "$python_bin" -m pip install --user -r requirements.txt >&2
  printf '%s\n' "$python_bin"
}

ensure_opencode_binary() {
  if command -v opencode >/dev/null 2>&1; then
    if opencode serve --help >/dev/null 2>&1; then
      return 0
    fi
    echo "Installed opencode binary is incompatible with OpenCode Lens. Expected a version with 'opencode serve' support." >&2
  fi

  if [[ "$AUTO_INSTALL_OPENCODE" == "0" ]]; then
    echo "opencode is not installed and auto-install is disabled." >&2
    return 1
  fi

  curl -fsSL "$OPENCODE_INSTALL_URL" | \
    VERSION="${OPENCODE_INSTALL_VERSION:-}" bash

  for candidate in "$HOME/.opencode/bin" "$HOME/.local/bin" "$HOME/bin"; do
    if [[ -x "$candidate/opencode" ]]; then
      export PATH="$candidate:$PATH"
      break
    fi
  done

  if ! command -v opencode >/dev/null 2>&1; then
    return 1
  fi

  opencode serve --help >/dev/null 2>&1
}

upstream_is_local() {
  case "$UPSTREAM_URL" in
    http://127.0.0.1:*|http://localhost:*|https://127.0.0.1:*|https://localhost:*)
      return 0
      ;;
  esac
  return 1
}

upstream_is_reachable() {
  if [[ -n "${OPENCODE_SERVER_PASSWORD:-}" ]]; then
    curl -fsS -u "${OPENCODE_SERVER_USERNAME:-opencode}:${OPENCODE_SERVER_PASSWORD}" "${UPSTREAM_URL}/doc" >/dev/null 2>&1
    return $?
  fi
  curl -fsS "${UPSTREAM_URL}/doc" >/dev/null 2>&1
}

parse_upstream_target() {
  local parsed
  parsed="$("$PYTHON_BIN" -c 'from urllib.parse import urlparse; import sys; u = urlparse(sys.argv[1]); print((u.hostname or "127.0.0.1"), (u.port or 4096))' "$UPSTREAM_URL")"
  # shellcheck disable=SC2206
  local parts=($parsed)
  printf '%s %s\n' "${parts[0]}" "${parts[1]}"
}

start_local_opencode_server() {
  if ! upstream_is_local; then
    return 0
  fi

  if upstream_is_reachable; then
    return 0
  fi

  ensure_opencode_binary

  local target host port
  read -r host port < <(parse_upstream_target)

  if [[ "$port" != "4096" ]]; then
    echo "OpenCode auto-start only supports the default local port 4096; skipping start for ${UPSTREAM_URL}." >&2
    return 0
  fi

  mkdir -p "$SERVER_LOG_DIR"
  touch "$SERVER_LOG_FILE"

  nohup env OPENCODE_CONFIG="$OPENCODE_CONFIG" OPENCODE_CONFIG_DIR="$OPENCODE_CONFIG_DIR" opencode serve --hostname "$host" --port "$port" >>"$SERVER_LOG_FILE" 2>&1 </dev/null &
  local opencode_pid=$!

  for _ in $(seq 1 40); do
    if upstream_is_reachable; then
      return 0
    fi
    if ! kill -0 "$opencode_pid" >/dev/null 2>&1; then
      echo "OpenCode server exited early. See $SERVER_LOG_FILE." >&2
      return 1
    fi
    sleep 0.5
  done

  echo "OpenCode server did not become ready. See $SERVER_LOG_FILE." >&2
  return 1
}

PYTHON_BIN="$(ensure_python_env "$PYTHON_BIN")"

if [[ "$AUTO_START_SERVER" != "0" ]]; then
  start_local_opencode_server
fi

exec "$PYTHON_BIN" -m uvicorn app:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8088}"
