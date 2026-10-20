[CmdletBinding()]
param(
    [string]$Source = (Join-Path (Split-Path -Parent $PSScriptRoot) '.artifacts/publish'),
    [string]$Destination = (Join-Path $env:ProgramFiles 'PCMonitor'),
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this installer from an Administrator PowerShell so CPU sensors and ETW capture can work.' }
$sourcePath = (Resolve-Path -LiteralPath $Source).Path
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath 'PCMonitor.exe')) -or -not (Test-Path -LiteralPath (Join-Path $sourcePath 'presentmon.exe'))) { throw 'Build with scripts/Build.ps1 -Publish first.' }
if ((Get-FileHash -LiteralPath (Join-Path $sourcePath 'presentmon.exe')).Hash.ToLowerInvariant() -ne '9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191') { throw 'PresentMon verification failed.' }
$oldNative = Get-ScheduledTask -TaskName 'PCMonitorNative' -ErrorAction SilentlyContinue
if ($oldNative) {
    $existingExe = $oldNative.Actions.Execute
    if (Test-Path -LiteralPath $existingExe) { & $existingExe --command exit | Out-Null; Start-Sleep -Seconds 2 }
    Stop-ScheduledTask -TaskName PCMonitorNative -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -Path (Join-Path $sourcePath '*') -Destination $Destination -Recurse -Force
$exe = Join-Path $Destination 'PCMonitor.exe'
# Retire the Python observer before stopping the app, so it cannot relaunch it.
foreach ($name in @('PCMonitorWatchdog','PCMonitor')) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        $legacyBackup = Join-Path $env:LOCALAPPDATA 'PCMonitor/LegacyTasks'
        New-Item -ItemType Directory -Path $legacyBackup -Force | Out-Null
        Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $legacyBackup ($name + '.xml'))
        Disable-ScheduledTask -TaskName $name | Out-Null; Stop-ScheduledTask -TaskName $name
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $Destination
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName PCMonitorNative -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Description 'Native PC Monitor tray and supervised hardware worker' -Force | Out-Null
if (-not $NoStart) { Start-ScheduledTask -TaskName PCMonitorNative }
Write-Output ('Installed: ' + $exe)
