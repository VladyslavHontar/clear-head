#!/usr/bin/env bash
# Removes the Jev claim-check Stop hook: unregisters it from Claude Code settings and,
# optionally, deletes the installed copy (script, logs, API key).
#
# Usage:
#   ./uninstall.sh              removes the user-scope install (~/.claude/settings.json)
#   ./uninstall.sh --project    removes the project-scope install (./.claude/settings.json)
set -euo pipefail

TARGET_DIR="${JEV_HOOK_DIR:-$HOME/.claude/hooks/jev}"
SETTINGS="$HOME/.claude/settings.json"

for arg in "$@"; do
  case "$arg" in
    --project) SETTINGS=".claude/settings.json" ;;
    --dir=*) TARGET_DIR="${arg#--dir=}" ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

python3 - "$SETTINGS" "$TARGET_DIR/stop_verify.py" <<'PY'
import json, sys, pathlib
settings_path, hook_path = sys.argv[1], sys.argv[2]
p = pathlib.Path(settings_path)
if not p.exists():
    print(f"{settings_path} does not exist, nothing to unregister.")
    raise SystemExit
data = json.loads(p.read_text())
cmd = f'python3 "{hook_path}"'
stop = data.get("hooks", {}).get("Stop", [])
kept = [h for h in stop if cmd not in json.dumps(h)]
removed = len(stop) - len(kept)
data.setdefault("hooks", {})["Stop"] = kept
p.write_text(json.dumps(data, indent=2))
print(f"Removed {removed} hook entry from {settings_path}" if removed == 1 else
      f"Removed {removed} hook entries from {settings_path}")
PY

if curl -s -o /dev/null -m 1 "http://127.0.0.1:${LAYA_PORT:-8787}/health" 2>/dev/null; then
  PID=$(lsof -ti tcp:"${LAYA_PORT:-8787}" 2>/dev/null || true)
  if [ -n "$PID" ]; then
    kill "$PID" && echo "Stopped laya_server.py (pid $PID)"
  fi
fi

if [ -d "$TARGET_DIR" ]; then
  read -rp "Also delete $TARGET_DIR (script, logs, API key)? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    rm -rf "$TARGET_DIR"
    echo "Removed $TARGET_DIR"
  fi
fi
