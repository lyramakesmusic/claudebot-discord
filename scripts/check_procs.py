"""Check running claudebot processes."""
import subprocess

r = subprocess.run(
    ['wmic', 'process', 'where', 'Name like "python%"',
     'get', 'ProcessId,CommandLine', '/format:csv'],
    capture_output=True, text=True
)

counts = {}
for line in r.stdout.splitlines():
    line = line.strip()
    if not line or 'ProcessId' in line:
        continue
    if 'claudebot' not in line.lower():
        continue
    parts = line.split(',')
    pid = parts[-1]
    if 'run.py' in line:
        typ = 'supervisor'
    elif 'self.py' in line:
        typ = 'selfbot'
    elif 'kimi_bot' in line:
        typ = 'kimi'
    elif 'codex_bot' in line:
        typ = 'codex'
    elif 'bot.py' in line:
        typ = 'claude'
    else:
        typ = 'unknown'
    counts.setdefault(typ, []).append(pid)
    print(f"  {typ:12s} PID {pid}")

print()
for typ, pids in counts.items():
    status = "OK" if len(pids) == 1 else f"DUPLICATE ({len(pids)})"
    print(f"{typ}: {status}")
