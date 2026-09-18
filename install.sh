#!/usr/bin/env bash
# Installs the Jev claim-check Stop hook for Claude Code.
#
# Usage:
#   ./install.sh                installs for this user, all projects (~/.claude/settings.json)
#   ./install.sh --project      installs for the current project only (./.claude/settings.json)
#   ./install.sh --dir=PATH     copy the hook to PATH instead of ~/.claude/hooks/jev
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${JEV_HOOK_DIR:-$HOME/.claude/hooks/jev}"
SCOPE="user"
SETTINGS="$HOME/.claude/settings.json"

for arg in "$@"; do
  case "$arg" in
    --project) SCOPE="project"; SETTINGS=".claude/settings.json" ;;
    --dir=*) TARGET_DIR="${arg#--dir=}" ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$TARGET_DIR"
cp "$SCRIPT_DIR/stop_verify.py" "$TARGET_DIR/stop_verify.py"
chmod +x "$TARGET_DIR/stop_verify.py"

if [ -z "${TYPESAFE_API_KEY:-}" ] && [ ! -f "$TARGET_DIR/.env" ]; then
  echo "Get a key at https://typesafe.ai if you don't have one."
  read -rsp "TypeSafe API key: " key; echo
  umask 077
  printf 'TYPESAFE_API_KEY=%s\n' "$key" > "$TARGET_DIR/.env"
  echo "Saved to $TARGET_DIR/.env (mode 600, not readable by other users)"
else
  echo "Using TYPESAFE_API_KEY from the environment or an existing .env — leaving it as is."
fi

mkdir -p "$(dirname "$SETTINGS")"
python3 - "$SETTINGS" "$TARGET_DIR/stop_verify.py" <<'PY'
import json, sys, pathlib
settings_path, hook_path = sys.argv[1], sys.argv[2]
p = pathlib.Path(settings_path)
data = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
data.setdefault("hooks", {}).setdefault("Stop", [])
cmd = f'python3 "{hook_path}"'
if any(cmd in json.dumps(h) for h in data["hooks"]["Stop"]):
    print(f"Already registered in {settings_path}")
else:
    data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": cmd, "timeout": 60}]})
    p.write_text(json.dumps(data, indent=2))
    print(f"Registered the Stop hook in {settings_path}")
PY

echo
echo "Checking the API key works..."
if python3 - "$TARGET_DIR" <<'PY'
import sys, pathlib, urllib.request, json
sys.path.insert(0, sys.argv[1])
import stop_verify as s
try:
    r = s.jev({"x": "the sky is blue"}, {"q": {"type": "noul", "instructions": "Does the state claim the sky is blue?"}})
    print("OK — Jev responded:", r["q"]["noul"])
except Exception as e:
    print("FAILED:", e); sys.exit(1)
PY
then
  echo
  echo "Installed ($SCOPE scope). Runs at the end of every matching Claude Code turn from now on."
  echo "Disable for one shell with:  JEV_HOOK=off"
  echo "Remove everything with:      ./uninstall.sh $( [ "$SCOPE" = project ] && echo --project )"
else
  echo "Install finished but the API key check failed — see the error above." >&2
  exit 1
fi
