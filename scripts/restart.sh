#!/bin/zsh
# Full restart: kill every claudebot process, then bring the supervisor chain back.
# Under launchd (com.lyra.claudebot) the agent is kickstarted; otherwise run.py is
# relaunched detached.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LABEL="com.lyra.claudebot"
UID_="$(id -u)"

echo "Killing claudebot processes..."
"$ROOT/.venv/bin/python" "$ROOT/scripts/nuke_and_restart.py" --kill
sleep 2

if launchctl print "gui/$UID_/$LABEL" >/dev/null 2>&1; then
  echo "Kickstarting $LABEL via launchd..."
  launchctl kickstart -k "gui/$UID_/$LABEL"
else
  echo "Starting run.py detached..."
  nohup "$ROOT/.venv/bin/python" "$ROOT/run.py" >/dev/null 2>&1 &
  disown
fi
sleep 3
echo "Running claudebot processes:"
pgrep -fl "claudebot/(run|bot|codex_bot|run_claire)\.py|claudebot/supervisor/supervisor\.py|claudebot/selfbot/self\.py" || echo "(none yet)"
