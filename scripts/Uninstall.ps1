[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName PCMonitorNative -ErrorAction SilentlyContinue
if ($task) {
    $exe = $task.Actions.Execute
    if (Test-Path -LiteralPath $exe) { & $exe --command exit | Out-Null; Start-Sleep -Seconds 2 }
    Stop-ScheduledTask -TaskName PCMonitorNative -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName PCMonitorNative -Confirm:$false
}
Write-Output 'Autostart removed. Installed files, settings, and logs were preserved.'
