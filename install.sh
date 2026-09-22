#!/usr/bin/env bash
# Installs the Jev claim-check Stop hook for Claude Code.
#
# Usage:
#   ./install.sh                installs for this user, all projects (~/.claude/settings.json)
#   ./install.sh --project      installs for the current project only (./.claude/settings.json)
#   ./install.sh --dir=PATH     copy the hook to PATH instead of ~/.claude/hooks/jev
#   ./install.sh --laya         use Laya (local, free, Apache-2.0) instead of Jev — no API key,
#                                runs entirely on this machine. Starts laya_server.py in the
#                                background; needs `pip install laya` (~400MB-1.6GB of weights,
#                                depending on checkpoint). See README's Laya section for the
#                                latency tradeoff this backend has vs. Jev's cloud call.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${JEV_HOOK_DIR:-$HOME/.claude/hooks/jev}"
SCOPE="user"
SETTINGS="$HOME/.claude/settings.json"
BACKEND="jev"

for arg in "$@"; do
  case "$arg" in
    --project) SCOPE="project"; SETTINGS=".claude/settings.json" ;;
    --dir=*) TARGET_DIR="${arg#--dir=}" ;;
    --laya) BACKEND="laya" ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$TARGET_DIR"
cp "$SCRIPT_DIR/stop_verify.py" "$TARGET_DIR/stop_verify.py"
chmod +x "$TARGET_DIR/stop_verify.py"

if [ "$BACKEND" = "laya" ]; then
  cp "$SCRIPT_DIR/laya_server.py" "$TARGET_DIR/laya_server.py"
  umask 077
  # persisted the same way as the API key, for the same reason: a shell `export` may not reach
  # a hook launched by a different Claude Code session
  grep -q '^VERIFIER_BACKEND=' "$TARGET_DIR/.env" 2>/dev/null \
    && sed -i.bak '/^VERIFIER_BACKEND=/d' "$TARGET_DIR/.env" && rm -f "$TARGET_DIR/.env.bak"
  printf 'VERIFIER_BACKEND=laya\n' >> "$TARGET_DIR/.env"

  command -v pip3 >/dev/null || { echo "pip3 is required for --laya" >&2; exit 1; }
  python3 -c "import laya" 2>/dev/null || { echo "Installing laya (this downloads model weights, may take a while)..."; pip3 install -q laya; }

  if curl -s -o /dev/null -m 1 "http://127.0.0.1:${LAYA_PORT:-8787}/health"; then
    echo "laya_server.py already running on port ${LAYA_PORT:-8787}."
  else
    echo "Starting laya_server.py in the background (loads the model once, stays warm)..."
    nohup python3 "$TARGET_DIR/laya_server.py" > "$TARGET_DIR/laya_server.log" 2>&1 &
    disown
    for i in $(seq 1 60); do
      curl -s -o /dev/null -m 1 "http://127.0.0.1:${LAYA_PORT:-8787}/health" && break
      sleep 1
    done
    curl -s -o /dev/null -m 1 "http://127.0.0.1:${LAYA_PORT:-8787}/health" \
      || { echo "laya_server.py didn't come up — check $TARGET_DIR/laya_server.log" >&2; exit 1; }
    echo "laya_server.py is up. It won't survive a reboot — see README to keep it running."
  fi
elif [ -z "${TYPESAFE_API_KEY:-}" ] && [ ! -f "$TARGET_DIR/.env" ]; then
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
echo "Checking the $BACKEND backend works..."
if python3 - "$TARGET_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import stop_verify as s
try:
    r = s.judge({"x": "the sky is blue"}, {"q": {"type": "noul", "instructions": "Does the state claim the sky is blue?"}})
    print(f"OK — {s.VERIFIER_BACKEND} responded:", r["q"]["noul"])
except Exception as e:
    print("FAILED:", e); sys.exit(1)
PY
then
  echo
  echo "Installed ($SCOPE scope, $BACKEND backend). Runs at the end of every matching Claude Code turn from now on."
  echo "Disable for one shell with:  JEV_HOOK=off"
  echo "Remove everything with:      ./uninstall.sh $( [ "$SCOPE" = project ] && echo --project )"
else
  echo "Install finished but the $BACKEND check failed — see the error above." >&2
  exit 1
fi
