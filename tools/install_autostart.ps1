<#
.SYNOPSIS
Install (or remove) the elevated autostart that gives the panel its full telemetry.

.DESCRIPTION
Two features need Administrator and nothing else in this app does:
  * the PresentMon ETW session -> fps, frametime, 1%/0.1% low, and the
    "foreground process is actually presenting" game detection;
  * LibreHardwareMonitor's ring0 access -> CPU Tctl temp, package power,
    per-core clocks.
Run without it and the layout honestly renders "--" in those slots.

The task is registered Interactive + Highest, i.e. *inside* your logged-in
desktop session with admin rights. That combination matters: a service or a
non-interactive task cannot see the foreground window, so game detection would
silently stop working. LogonType Interactive keeps the window-station access.

The app owns one COM port, so any already-running instance is stopped first --
otherwise the new one dies on "Cannot open COM port". Nothing is stopped, and
nothing is registered, until tools\env_check.py --strict has said the installed
dependencies can actually do what this task is for; -SkipChecks registers anyway.

.EXAMPLE
  # one elevated window, does everything including the first start
  Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command', "& { & '$(Get-Location)\tools\install_autostart.ps1' } "

.EXAMPLE
  .\tools\install_autostart.ps1 -Remove    # unregister and stop (elevated too)

.EXAMPLE
  .\tools\install_autostart.ps1 -SkipChecks  # register on an incomplete environment
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$NoStart,
    [switch]$SkipChecks
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$TaskName = 'PCMonitor'
$Pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
# The console interpreter of the same venv: env_check.py has to ask the questions of the
# interpreter the scheduled task will run, not of whichever python is on PATH.
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Main = Join-Path $Root 'main.py'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-AppInstances {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match 'main\.py' } |
        ForEach-Object {
            "  stopping pid $($_.ProcessId) ($($_.Name))"
            Stop-Process -Id $_.ProcessId -Force
        }
}

function Stop-OrphanPresentMon {
    <#
      presentmon owns an ETW kernel session. When the app is killed hard (Stop-Process,
      a crash, a logout) its atexit never runs, so the child survives with its session
      open and burns CPU forever. The app's own kill-on-exit job object cannot always
      be created -- if the app is already inside a job that forbids nesting, Windows
      answers ERROR_ACCESS_DENIED -- and the role-scoped session name only reclaims the
      session when something next starts. So: kill the ones whose parent is gone.
      Elevated, because an orphan inherited from an elevated run is elevated too.
    #>
    $live = @(Get-CimInstance Win32_Process | ForEach-Object ProcessId)
    Get-CimInstance Win32_Process -Filter "Name='presentmon.exe'" |
        Where-Object { $live -notcontains $_.ParentProcessId } |
        ForEach-Object {
            "  killing orphan presentmon pid $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

if (-not (Test-Admin)) {
    throw "needs an elevated PowerShell. One-liner: Start-Process pwsh -Verb RunAs -ArgumentList " +
          "'-NoProfile','-ExecutionPolicy','Bypass','-Command', `"& { & '$PSCommandPath' } `" " +
          "-WorkingDirectory '$Root'"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Remove) {
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "unregistered $TaskName"
    }
    Stop-AppInstances
    Stop-OrphanPresentMon
    "app instances stopped; the panel is free"
    return
}

if (-not (Test-Path $Pyw)) { throw "no interpreter at $Pyw - create the venv first (tools\bootstrap.ps1)" }
if (-not (Test-Path $Main)) { throw "no main.py at $Main" }

if (-not $SkipChecks) {
    <#
      Registering the task is the last thing a user does, and until now it was also the
      first moment anything checked that the environment could do the job: a clone with
      no vendored library, no pythonnet, or a PresentMon binary that is not the pinned
      one registered happily, started at every logon, and quietly drew `--` in the
      columns the elevation was supposed to fill. Validate first, in the interpreter the
      task will actually run, and say what is missing rather than registering a task
      that will under-deliver.
    #>
    ' checking the dependencies the elevated task needs'
    $checkArgs = @((Join-Path $PSScriptRoot 'env_check.py'), '--root', $Root,
                   '--strict', '--manifest')
    & $Py @checkArgs
    if ($LASTEXITCODE -ne 0) {
        throw ("the environment is incomplete (env_check.py exited $LASTEXITCODE, see " +
               'the FAIL lines above). Fix those, or run this with -SkipChecks to ' +
               'register the task anyway.')
    }
}

" stopping existing instances (the panel has one COM port)"
Stop-AppInstances
Stop-OrphanPresentMon
Start-Sleep -Seconds 1

$action = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$Main`"" -WorkingDirectory $Root
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'PC desk telemetry on the 5-inch Turing panel (elevated: ETW frame stats + LHM CPU sensors).' |
    Out-Null
" $(if ($existing) { 'updated' } else { 'registered' }) task '$TaskName' -> $Pyw main.py"

if ($NoStart) { return }

" starting it now"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 20   # a revision-C start wakes, resets and re-detects the panel

$info = Get-ScheduledTaskInfo -TaskName $TaskName
"  task state : $($info.State)"
"  last result: 0x$('{0:X}' -f $info.LastTaskResult)   (0x41301 = running)"
$live = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'main\.py' })
if ($live) {
    # two pids are normal: the venv launcher and the interpreter it re-execs. The
    # interpreter is the one whose parent is the other one; presentmon hangs off it.
    $pids = @($live | ForEach-Object ProcessId)
    "  app pids   : $($pids -join ', ')"
    $kids = @(Get-CimInstance Win32_Process -Filter "Name='presentmon.exe'" |
        Where-Object { $pids -contains $_.ParentProcessId })
    "  frame stats: $(if ($kids) { "presentmon pid $($kids.ProcessId -join ', ')" } else { 'no presentmon child - frame stats are off, see log.log' })"
    (Get-Content (Join-Path $Root 'log.log') -Tail 3 -ErrorAction SilentlyContinue) | ForEach-Object { "  log: $_" }
} else {
    "  NOT running - check $Root\log.log (the app writes its start-up trace there)"
}
