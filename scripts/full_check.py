"""Full process check for claudebot."""
import subprocess

r = subprocess.run(
    ['wmic', 'process', 'where', 'Name like "python%"',
     'get', 'ProcessId,CommandLine', '/format:csv'],
    capture_output=True, text=True
)

print("ALL Python processes with 'claudebot' or 'bot.py' or 'run.py':")
print("=" * 80)
my_pid_str = str(subprocess.os.getpid())
for line in r.stdout.splitlines():
    line = line.strip()
    if not line:
        continue
    low = line.lower()
    if any(kw in low for kw in ['claudebot', 'bot.py', 'run.py', 'self.py']):
        parts = line.split(',')
        pid = parts[-1]
        if pid == my_pid_str:
            continue  # skip ourselves
        # Trim command for readability
        cmd = ','.join(parts[1:-1])
        if len(cmd) > 100:
            cmd = cmd[:100] + '...'
        print(f"  PID {pid:>6}  {cmd}")

print()
print("Lock files:")
import os
lock_dir = r'C:\Users\Lyra\Documents\claudebot\data'
locks = [f for f in os.listdir(lock_dir) if f.endswith('.lock')]
if locks:
    for f in locks:
        path = os.path.join(lock_dir, f)
        with open(path) as fh:
            print(f"  {f}: PID {fh.read().strip()}")
else:
    print("  (none)")
