"""Fix the Windows scheduled task to use venv pythonw.exe instead of system Python."""
import subprocess

TASK_NAME = "claudebot"
VENV_PYTHONW = r"C:\Users\Lyra\Documents\claudebot\.venv\Scripts\pythonw.exe"
RUN_PY = r"C:\Users\Lyra\Documents\claudebot\run.py"
WORKING_DIR = r"C:\Users\Lyra\Documents\claudebot"

# Update the task action
cmd = [
    "schtasks", "/Change",
    "/TN", TASK_NAME,
    "/TR", f'"{VENV_PYTHONW}" "{RUN_PY}"',
]

print(f"Updating scheduled task '{TASK_NAME}'...")
print(f"  Executable: {VENV_PYTHONW}")
print(f"  Script: {RUN_PY}")
r = subprocess.run(cmd, capture_output=True, text=True)
print(r.stdout.strip() or r.stderr.strip())
