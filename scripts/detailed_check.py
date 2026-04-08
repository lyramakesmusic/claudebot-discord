"""Show detailed process info for all claudebot python processes."""
import subprocess
import os

MY_PID = os.getpid()

r = subprocess.run(
    ['wmic', 'process', 'where', 'Name like "python%"',
     'get', 'ProcessId,CommandLine,ParentProcessId', '/format:csv'],
    capture_output=True, text=True
)

print(f"Self PID: {MY_PID}")
print("=" * 100)

for line in r.stdout.splitlines():
    line = line.strip()
    if not line or line.startswith('Node'):
        continue
    if 'claudebot' not in line.lower():
        continue

    # CSV format: Node,CommandLine,ParentProcessId,ProcessId
    parts = line.split(',')
    pid = parts[-1]
    ppid = parts[-2]
    cmd = ','.join(parts[1:-2])  # command might have commas

    if cmd and len(cmd) > 120:
        cmd = cmd[:120] + '...'

    marker = " <-- SELF" if str(MY_PID) == pid else ""
    print(f"PID {pid:>6}  PPID {ppid:>6}  {cmd}{marker}")

print()
print("Lockfiles:")
lock_dir = r'C:\Users\Lyra\Documents\claudebot\data'
for f in sorted(os.listdir(lock_dir)):
    if f.endswith('.lock'):
        with open(os.path.join(lock_dir, f)) as fh:
            print(f"  {f}: PID {fh.read().strip()}")
