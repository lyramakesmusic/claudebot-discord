"""Kill all claudebot-related python processes except this one, then report."""
import os
import subprocess
import sys

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# List all python processes
r = subprocess.run(
    ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/format:csv"],
    capture_output=True, text=True, creationflags=CREATE_FLAGS,
)

my_pid = os.getpid()
killed = []
found = []

for line in r.stdout.splitlines():
    line = line.strip()
    if not line or line.startswith("Node"):
        continue
    found.append(line)
    if "claudebot" not in line and "run.py" not in line and "bot.py" not in line:
        continue
    # csv: Node,CommandLine,ProcessId
    parts = line.rsplit(",", 1)
    try:
        pid = int(parts[-1].strip())
    except (ValueError, IndexError):
        continue
    if pid == my_pid:
        continue
    try:
        os.kill(pid, 9)
        killed.append(pid)
        print(f"  killed PID {pid}")
    except OSError as e:
        print(f"  failed to kill PID {pid}: {e}")

if killed:
    print(f"\nKilled {len(killed)} processes")
else:
    print("No claudebot processes found")
    if "--verbose" in sys.argv:
        print(f"\nAll python processes ({len(found)}):")
        for l in found[:20]:
            print(f"  {l}")
