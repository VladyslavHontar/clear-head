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
# match on the command field: json.dumps escapes cmd's quotes, so a substring test never matched
kept = [h for h in stop if not any(hook.get("command") == cmd for hook in h.get("hooks", []))]
removed = len(stop) - len(kept)
data.setdefault("hooks", {})["Stop"] = kept
p.write_text(json.dumps(data, indent=2))
print(f"Removed {removed} hook entry from {settings_path}" if removed == 1 else
      f"Removed {removed} hook entries from {settings_path}")
PY

PID=$(lsof -ti tcp:"${KEV_PORT:-8009}" 2>/dev/null || true)
if [ -n "$PID" ] && ps -o command= -p "$PID" | grep -q "kev.serve"; then
  kill "$PID" && echo "Stopped the Kev server (pid $PID)"
fi

if [ -d "$TARGET_DIR" ]; then
  read -rp "Also delete $TARGET_DIR (script, logs, API key, Kev checkout if any)? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    rm -rf "$TARGET_DIR"
    echo "Removed $TARGET_DIR"
  fi
fi
