"""Test that the mutex-based lockfile actually prevents duplicates."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.lockfile import acquire_lock

name = "test_mutex_check"
print(f"Attempt 1: acquire_lock('{name}')...")
result1 = acquire_lock(name)
print(f"  Result: {result1}")

print(f"Attempt 2: acquire_lock('{name}') (should be False)...")
result2 = acquire_lock(name)
print(f"  Result: {result2}")

if result1 and not result2:
    print("\nMutex works correctly!")
elif result1 and result2:
    print("\nBUG: Mutex allowed double acquisition in same process!")
else:
    print(f"\nUnexpected: {result1}, {result2}")

# Now test cross-process
import subprocess
test_script = f'''
import sys, os
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
from shared.lockfile import acquire_lock
result = acquire_lock("{name}")
print(f"Child process: acquire_lock result = {{result}}")
sys.exit(0 if not result else 1)  # exit 0 = correctly blocked, exit 1 = bug
'''

print("\nCross-process test...")
r = subprocess.run([sys.executable, '-c', test_script], capture_output=True, text=True)
print(f"  stdout: {r.stdout.strip()}")
print(f"  exit code: {r.returncode}")
if r.returncode == 0:
    print("  Cross-process mutex works correctly!")
else:
    print("  BUG: Second process was not blocked by mutex!")
    if r.stderr:
        print(f"  stderr: {r.stderr.strip()[:200]}")
