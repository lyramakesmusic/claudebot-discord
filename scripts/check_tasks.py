"""Check Windows scheduled tasks related to claudebot."""
import subprocess
r = subprocess.run(
    ['powershell', '-NoProfile', '-Command',
     'Get-ScheduledTask | Where-Object { $_.TaskName -like "*claude*" -or $_.TaskName -like "*bot*" } | Format-Table TaskName, State, @{Name="Actions";Expression={$_.Actions.Execute + " " + $_.Actions.Arguments}} -AutoSize -Wrap'],
    capture_output=True, text=True
)
print(r.stdout or "(none)")
if r.stderr:
    print("STDERR:", r.stderr[:200])
