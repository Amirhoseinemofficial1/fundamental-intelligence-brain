#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

validate_env_file() {
  local file="$1"
  local bad=0 line_no=0 raw trimmed key value
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    line_no=$((line_no+1))
    raw="${raw%$'\r'}"
    trimmed="${raw#"${raw%%[![:space:]]*}"}"
    [[ -z "$trimmed" || "${trimmed:0:1}" == "#" ]] && continue
    if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*: ]]; then
      key="${BASH_REMATCH[1]}"
      echo "ERROR: $file line $line_no uses ':' after $key. Use ${key}='value' (KEY=VALUE), never KEY: value." >&2
      bad=1
      continue
    fi
    if [[ ! "$trimmed" =~ ^(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*= ]]; then
      echo "ERROR: $file line $line_no is not a KEY=VALUE assignment or comment. Value is not printed for secret safety." >&2
      bad=1
    fi
  done < "$file"
  (( bad == 0 )) || return 2
}

load_env_file_literal() {
  local file="$1" raw trimmed assignment key value
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    raw="${raw%$'\r'}"
    trimmed="${raw#"${raw%%[![:space:]]*}"}"
    [[ -z "$trimmed" || "${trimmed:0:1}" == "#" ]] && continue
    trimmed="${trimmed#export }"
    key="${trimmed%%=*}"
    key="${key//[[:space:]]/}"
    value="${trimmed#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 && "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ ${#value} -ge 2 && "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    fi
    # export receives one already-parsed argument; shell syntax inside value is inert.
    export "$key=$value"
  done < "$file"
}

if [[ -f "$ROOT/.env" ]]; then
  validate_env_file "$ROOT/.env"
  load_env_file_literal "$ROOT/.env"
fi
export V52_PACKAGE_ROOT="$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && "$(command -v python3)" -c 'import sys' >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1 && "$(command -v python)" -c 'import sys' >/dev/null 2>&1; then
  PY="$(command -v python)"
else
  echo "an executable Python interpreter was not found" >&2
  exit 127
fi
