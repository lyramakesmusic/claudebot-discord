# Kill all Python processes (claudebot supervisor + bot + selfbot)
Write-Host "Killing all Python processes..."
Get-Process python*,pythonw* -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# Restart the supervisor (headless)
Write-Host "Starting supervisor..."
Start-Process -FilePath "python" -ArgumentList "run.py" -WorkingDirectory "C:\Users\Lyra\Documents\claudebot" -WindowStyle Hidden

Start-Sleep -Seconds 3

# Verify
Write-Host "`nRunning Python processes:"
Get-Process python* -ErrorAction SilentlyContinue | Format-Table Id,StartTime -AutoSize
