$pids = @(1634800,2051352,2504420,1842680,1257884,591464)
foreach ($pid in $pids) {
    try { Stop-Process -Id $pid -Force -ErrorAction Stop; Write-Host "Killed $pid" } catch { Write-Host "PID $pid already gone" }
}
