#!/usr/bin/env bash
# Installs the Jev claim-check Stop hook for Claude Code.
#
# Usage:
#   ./install.sh                installs for this user, all projects (~/.claude/settings.json)
#   ./install.sh --project      installs for the current project only (./.claude/settings.json)
#   ./install.sh --dir=PATH     copy the hook to PATH instead of ~/.claude/hooks/jev
#   ./install.sh --kev          judge locally with Kev (https://github.com/jaredpalmer/kev, Apache-2.0)
#                                instead of TypeSafe's API: no key, nothing leaves the machine.
#                                Needs `uv` and ~8 GB of RAM for the 4B model (Apple Silicon via
#                                MLX, or CUDA/ROCm). See README for what it catches vs. Jev.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${JEV_HOOK_DIR:-$HOME/.claude/hooks/jev}"
SCOPE="user"
SETTINGS="$HOME/.claude/settings.json"
BACKEND="jev"
KEV_PORT="${KEV_PORT:-8009}"

for arg in "$@"; do
  case "$arg" in
    --project) SCOPE="project"; SETTINGS=".claude/settings.json" ;;
    --dir=*) TARGET_DIR="${arg#--dir=}" ;;
    --kev) BACKEND="kev" ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$TARGET_DIR"
cp "$SCRIPT_DIR/stop_verify.py" "$TARGET_DIR/stop_verify.py"
chmod +x "$TARGET_DIR/stop_verify.py"

if [ "$BACKEND" = "kev" ]; then
  command -v uv >/dev/null || { echo "uv is required for --kev (https://docs.astral.sh/uv/)" >&2; exit 1; }
  command -v git >/dev/null || { echo "git is required for --kev" >&2; exit 1; }
  umask 077
  # persisted like the API key: a shell `export` may not reach a hook launched by another session
  grep -q '^VERIFIER_BACKEND=' "$TARGET_DIR/.env" 2>/dev/null \
    && sed -i.bak '/^VERIFIER_BACKEND=/d' "$TARGET_DIR/.env" && rm -f "$TARGET_DIR/.env.bak"
  printf 'VERIFIER_BACKEND=kev\n' >> "$TARGET_DIR/.env"
  if [ "$KEV_PORT" != 8009 ]; then
    grep -q '^KEV_PORT=' "$TARGET_DIR/.env" 2>/dev/null \
      && sed -i.bak '/^KEV_PORT=/d' "$TARGET_DIR/.env" && rm -f "$TARGET_DIR/.env.bak"
    printf 'KEV_PORT=%s\n' "$KEV_PORT" >> "$TARGET_DIR/.env"
  fi

  cp "$SCRIPT_DIR/kev_serve.sh" "$TARGET_DIR/kev_serve.sh"; chmod +x "$TARGET_DIR/kev_serve.sh"
  [ -d "$TARGET_DIR/kev" ] || git clone -q --depth 1 https://github.com/jaredpalmer/kev "$TARGET_DIR/kev"
  (cd "$TARGET_DIR/kev" && uv sync -q --extra serve)
  if curl -s -o /dev/null -m 2 -X POST "http://127.0.0.1:$KEV_PORT/v1/systemone" -H 'Content-Type: application/json' \
       -d '{"model":"kev-latest","state":"x","questions":{"q":{"type":"noul","instructions":"x?"}}}'; then
    echo "Kev already serving on port $KEV_PORT."
  else
    echo "Starting Kev in the background (first run downloads the ~8 GB kev-4b weights)..."
    KEV_PORT="$KEV_PORT" nohup "$TARGET_DIR/kev_serve.sh" > "$TARGET_DIR/kev_server.log" 2>&1 &
    for i in $(seq 1 240); do
      curl -s -o /dev/null -m 2 -X POST "http://127.0.0.1:$KEV_PORT/v1/systemone" -H 'Content-Type: application/json' \
        -d '{"model":"kev-latest","state":"x","questions":{"q":{"type":"noul","instructions":"x?"}}}' && break
      sleep 5
    done
    curl -s -o /dev/null -m 2 -X POST "http://127.0.0.1:$KEV_PORT/v1/systemone" -H 'Content-Type: application/json' \
      -d '{"model":"kev-latest","state":"x","questions":{"q":{"type":"noul","instructions":"x?"}}}' \
      || { echo "Kev didn't come up — check $TARGET_DIR/kev_server.log" >&2; exit 1; }
    echo "Kev is up. It won't survive a reboot — see README to keep it running."
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
# Kev answers one claim at a time at ~1.5 s each on a laptop GPU (see the PER_CLAIM comment in
# stop_verify.py), so a long answer needs well over Jev's 60 s.
TIMEOUT=$( [ "$BACKEND" = kev ] && echo 180 || echo 60 )
python3 - "$SETTINGS" "$TARGET_DIR/stop_verify.py" "$TIMEOUT" <<'PY'
import json, sys, pathlib
settings_path, hook_path, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3])
p = pathlib.Path(settings_path)
data = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
data.setdefault("hooks", {}).setdefault("Stop", [])
cmd = f'python3 "{hook_path}"'
# match on the command field itself: json.dumps escapes the quotes in cmd, so a substring test
# against the dumped entry never matches and every re-run used to append a duplicate
existing = [hook for h in data["hooks"]["Stop"] for hook in h.get("hooks", []) if hook.get("command") == cmd]
if existing:
    for hook in existing:
        hook["timeout"] = timeout
    p.write_text(json.dumps(data, indent=2))
    print(f"Already registered in {settings_path}; timeout set to {timeout}s")
else:
    data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
    p.write_text(json.dumps(data, indent=2))
    print(f"Registered the Stop hook in {settings_path} (timeout {timeout}s)")
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
