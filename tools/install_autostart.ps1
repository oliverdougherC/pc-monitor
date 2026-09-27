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
otherwise the new one dies on "Cannot open COM port".

Recovery, because a panel that stopped at 3 a.m. should not wait for the next logon:

  * the task restarts on a non-zero exit, a bounded 3 times per 5-minute interval;
  * tools/watchdog_autostart.ps1 runs every 5 minutes as PCMonitorWatchdog and
    restarts the task when the app's own heartbeat file stops advancing - the one
    failure the app cannot report, because the loop that would log it is the loop
    that hung;
  * the budget, the backoff and the "give up, this needs a human" verdict live in
    app/liveness.py, which is why they can be tested without waiting for a hang that
    never reproduces. The numbers this script registers are read back and printed at
    the end, so the policy can be inspected instead of assumed.

.EXAMPLE
  # one elevated window, does everything including the first start
  Start-Process pwsh -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command', "& { & '$(Get-Location)\tools\install_autostart.ps1' } "

.EXAMPLE
  .\tools\install_autostart.ps1 -Remove    # unregister and stop (elevated too)
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$TaskName = 'PCMonitor'
$WatchdogName = 'PCMonitorWatchdog'
$Pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Main = Join-Path $Root 'main.py'

# Bounded recovery, in two halves, because they catch different failures.
#
# RestartCount/RestartInterval is Task Scheduler's restart-on-failure: it reacts to a
# process that *exited non-zero*, which is what a fatal start-up error now does. The
# interval is fixed (the scheduler has no backoff), so the backoff and the permanent
# verdict live in app/liveness.py, which the watchdog consults before it touches
# anything: three attempts an hour, doubling waits, then leave it alone for a human.
# A fatal misconfiguration therefore burns its budget and stops, instead of relaunching
# an app that can never start.
$RestartCount = 3
$RestartIntervalMin = 5
# How often the watchdog looks. It must be shorter than the stall threshold in
# app/liveness.py (600 s) so a hang is seen twice before anything is restarted.
$WatchdogEveryMin = 5

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
    # The watchdog goes first, on purpose. Unregistering the app and then stopping its
    # instances leaves a window in which the observer sees a stale heartbeat and
    # cheerfully starts the thing that was just uninstalled; recovery that undoes a
    # deliberate removal is not recovery.
    if (Get-ScheduledTask -TaskName $WatchdogName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $WatchdogName -Confirm:$false
        "unregistered $WatchdogName (the observer is gone too, or a removal restarts itself)"
    }
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        "unregistered $TaskName"
    }
    Stop-AppInstances
    Stop-OrphanPresentMon
    "app instances stopped; the panel is free"
    return
}

if (-not (Test-Path $Pyw)) { throw "no interpreter at $Pyw - create the venv first" }
if (-not (Test-Path $Main)) { throw "no main.py at $Main" }

" stopping existing instances (the panel has one COM port)"
Stop-AppInstances
Stop-OrphanPresentMon
Start-Sleep -Seconds 1

$action = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$Main`"" -WorkingDirectory $Root
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit 0 `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes $RestartIntervalMin)

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Trigger $trigger `
    -Settings $settings -Force `
    -Description 'PC desk telemetry on the 5-inch Turing panel (elevated: ETW frame stats + LHM CPU sensors). Restarts up to 3x/5min on a non-zero exit; PCMonitorWatchdog restarts a hung loop.' |
    Out-Null
" $(if ($existing) { 'updated' } else { 'registered' }) task '$TaskName' -> $Pyw main.py"

# The observer. The app task can be restarted when it *fails*; nothing can notice that
# it stopped making progress while still running, because the heartbeat and the loop
# that would report it are the same thread. So: a second task, its own schedule, whose
# whole policy lives in `python -m app.liveness decide`.
$Wd = Join-Path $PSScriptRoot 'watchdog_autostart.ps1'
if (-not (Test-Path $Wd)) { throw "no watchdog script at $Wd" }
$Shell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $Shell) { $Shell = (Get-Command powershell.exe).Source }
$wdAction = New-ScheduledTaskAction -Execute $Shell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Wd`"" -WorkingDirectory $Root
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogEveryMin) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$wdSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $WatchdogName -Action $wdAction -Principal $principal `
    -Trigger $wdTrigger -Settings $wdSettings -Force `
    -Description "Watches PCMonitor's heartbeat file and restarts the task if the loop stops beating. Policy and backoff: app/liveness.py. Log: watchdog.log." |
    Out-Null
" $(if (Get-ScheduledTask -TaskName $WatchdogName) { 'registered' } else { 'registered' }) task '$WatchdogName' -> every $WatchdogEveryMin min (log: watchdog.log)"

# Printed because this is the only part of the recovery story that can be checked
# without waiting for a crash: the numbers below are what Task Scheduler will actually
# enforce, read back from the registration rather than echoed from the parameters.
$eff = (Get-ScheduledTask -TaskName $TaskName).Settings
"  restart on failure: $($eff.RestartCount) x every $($eff.RestartInterval)  (bounded; app/liveness.py backs off, then gives up)"
"  execution time    : $($eff.ExecutionTimeLimit)  (unlimited on purpose - this task runs for weeks)"
"  multiple instances: $($eff.MultipleInstances)  (one app instance, one ETW collector)"

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
