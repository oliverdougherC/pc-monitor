[CmdletBinding()]
param(
    [string]$Executable = (Join-Path $env:ProgramFiles 'PCMonitor/PCMonitor.exe'),
    [string]$Output = (Join-Path (Split-Path -Parent $PSScriptRoot) '.artifacts/installed-checks.json'),
    [switch]$ExerciseControls
)
$ErrorActionPreference = 'Stop'
function Read-NativeStatus {
    $text = (& $Executable --command status | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Native status command failed.' }
    $text | ConvertFrom-Json
}
function Wait-NativeLight([string]$Light) {
    $until = (Get-Date).AddSeconds(40)
    do {
        Start-Sleep -Milliseconds 200
        $status = Read-NativeStatus
        if ($status.WorkerPid -and $status.LastProgress.Panel -like 'connected*' -and $status.LastProgress.Light -eq $Light) { return $status }
    } while ((Get-Date) -lt $until)
    throw ('Expected connected panel with appearance: ' + $Light)
}
function Measure-NativeRuntime([int]$Seconds) {
    $status = Read-NativeStatus
    $ids = @($status.ProcessId, $status.WorkerPid) + @(Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $status.WorkerPid -and $_.Name -eq 'presentmon.exe' } | Select-Object -ExpandProperty ProcessId)
    $before = Get-Process -Id $ids
    $cpu = ($before | ForEach-Object { $_.CPU } | Measure-Object -Sum).Sum
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Seconds $Seconds
    $after = Get-Process -Id $ids
    $oneCore = 100 * ((($after | ForEach-Object { $_.CPU } | Measure-Object -Sum).Sum) - $cpu) / $clock.Elapsed.TotalSeconds
    [ordered]@{
        Seconds = [math]::Round($clock.Elapsed.TotalSeconds, 2)
        CpuPercentOfOneCore = [math]::Round($oneCore, 3)
        CpuPercentOfMachine = [math]::Round($oneCore / [Environment]::ProcessorCount, 3)
        Processes = @($after | Select-Object Id, ProcessName, @{n='PrivateMiB';e={[math]::Round($_.PrivateMemorySize64/1MB,2)}}, @{n='WorkingMiB';e={[math]::Round($_.WorkingSet64/1MB,2)}}, @{n='Threads';e={$_.Threads.Count}}, HandleCount)
    }
}
$report = [ordered]@{ Time = (Get-Date).ToString('o'); Initial = Read-NativeStatus }
try {
    $expected = if ($report.Initial.LastProgress.Light -eq 'night') { 'night' } else { 'lit' }
    $report.LitStart = Wait-NativeLight $expected
    $report.LitMetrics = Measure-NativeRuntime 30
    $report.LitAfter = Read-NativeStatus
    if ($report.LitAfter.WorkerPid -ne $report.LitStart.WorkerPid -or $report.LitAfter.LastProgress.FramesPresented -le $report.LitStart.LastProgress.FramesPresented) { throw 'No confirmed display progress during lit sampling.' }
    if (-not $report.LitAfter.LastProgress.SampleTime -or ((Get-Date).ToUniversalTime() - ([datetime]$report.LitAfter.LastProgress.SampleTime).ToUniversalTime()).TotalSeconds -gt 5) { throw 'Hardware sample is stale.' }
    if ($ExerciseControls) {
        & $Executable --command blank | Out-Null
        $report.BlankStatus = Wait-NativeLight 'manual'
        $report.BlankMetrics = Measure-NativeRuntime 10
        $report.BlankAfter = Read-NativeStatus
        if ($report.BlankAfter.LastProgress.FramesPresented -ne $report.BlankStatus.LastProgress.FramesPresented) { throw 'Repeated image uploads while blanked.' }
        $clock = [Diagnostics.Stopwatch]::StartNew()
        & $Executable --command unblank | Out-Null
        $report.After = Wait-NativeLight $expected
        if ($report.After.LastProgress.FramesPresented -le $report.BlankAfter.LastProgress.FramesPresented) { throw 'Unblank did not confirm a current frame.' }
        $report.WarmWakeSeconds = [math]::Round($clock.Elapsed.TotalSeconds, 3)
    }
    $report.Passed = $true
} catch { $report.Passed = $false; $report.Error = ($_ | Out-String); throw }
finally {
    if ($ExerciseControls) { & $Executable --command unblank | Out-Null }
    $parent = Split-Path -Parent $Output
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Output -Encoding utf8
}
