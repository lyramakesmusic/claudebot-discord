"""Check lockfile PIDs and their alive status."""
import os
import ctypes

lock_dir = r'C:\Users\Lyra\Documents\claudebot\data'
for f in sorted(os.listdir(lock_dir)):
    if f.endswith('.lock'):
        path = os.path.join(lock_dir, f)
        with open(path) as fh:
            pid = int(fh.read().strip())
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        alive = bool(h)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
        print(f"{f}: PID {pid} - {'ALIVE' if alive else 'DEAD'}")
