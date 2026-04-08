"""Test: start two supervisors and verify the second one exits."""
import subprocess
import time
import os
import sys

CLAUDEBOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
venv_python = os.path.join(CLAUDEBOT_DIR, '.venv', 'Scripts', 'python.exe')
run_py = os.path.join(CLAUDEBOT_DIR, 'run.py')

print("Starting supervisor 1 (with visible output)...")
p1 = subprocess.Popen(
    [venv_python, run_py],
    cwd=CLAUDEBOT_DIR,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
print(f"  PID {p1.pid}")

time.sleep(3)

print(f"\nSupervisor 1 alive? {p1.poll() is None}")
if p1.poll() is not None:
    print(f"  Exited with code {p1.poll()}")
    print(f"  stdout: {p1.stdout.read().decode()[:200]}")
    print(f"  stderr: {p1.stderr.read().decode()[:200]}")
    sys.exit(1)

print("\nStarting supervisor 2 (should fail with exit code 78)...")
p2 = subprocess.Popen(
    [venv_python, run_py],
    cwd=CLAUDEBOT_DIR,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
print(f"  PID {p2.pid}")

time.sleep(3)

exit2 = p2.poll()
print(f"\nSupervisor 2 alive? {exit2 is None}")
if exit2 is not None:
    print(f"  Exit code: {exit2}")
    stdout2 = p2.stdout.read().decode()
    stderr2 = p2.stderr.read().decode()
    if stdout2:
        print(f"  stdout: {stdout2[:200]}")
    if stderr2:
        print(f"  stderr: {stderr2[:200]}")
    if exit2 == 78:
        print("  CORRECT: Second supervisor blocked by mutex!")
    else:
        print(f"  UNEXPECTED exit code {exit2}")
else:
    print("  BUG: Second supervisor is still running!")
    p2.kill()

# Clean up
print("\nCleaning up...")
p1.kill()
p1.wait()
print("Done")
