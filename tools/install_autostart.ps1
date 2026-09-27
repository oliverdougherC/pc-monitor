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
$WatchdogName = 'PCMonitorWatchdog'
$Pyw = Join-Path $Root '.venv\Scripts\pythonw.exe'
# The console interpreter of the same venv: env_check.py has to ask the questions of the
# interpreter the scheduled task will run, not of whichever python is on PATH.
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Main = Join-Path $Root 'main.py'
# The collector's identity, named here rather than inferred later: a presentmon.exe is
# this project's only if it is the pinned binary under this root AND it carries the
# role-scoped ETW session this project names. Both come from this install, so neither can
# be confused with another application's capture.
$Collector = Join-Path $Root 'vendor\presentmon\presentmon.exe'
$Session = 'PCMonitor-main'

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

function Invoke-Owned([string]$Verb) {
    <#
      Both stop helpers used to be one filename filter and a hard kill: every python or
      pythonw process whose command line contained the substring for our entry-point
      script, force-terminated, run elevated. On any development machine that includes
      another project's dev server, a notebook kernel and an editor's language server,
      because main.py is the most common entry-point name in Python. The collector helper
      was the same shape - any presentmon.exe whose parent pid was no longer live - and a
      parent pid is not ownership, because pids are recycled.

      Ownership is decided in app/owned.py, from the record the app writes about itself
      and from canonical paths, and it declines wherever ownership is not established.
      PowerShell's part is to run it and to show what it declined. Nothing in this file
      filters a process by name or by command line any more, and nothing here kills one
      directly: that is the guarantee tools/owned_process_selftest.py holds.
    #>
    if (-not (Test-Path $Py)) {
        Write-Output 'no interpreter at .venv\Scripts\python.exe - not touching any process'
        return @()
    }
    $out = @(& $Py -m app.owned $Verb --session $Session --collector $Collector 2>&1 |
        ForEach-Object { "$_" })
    if ($LASTEXITCODE -ne 0) {
        throw "app.owned $Verb failed (exit $LASTEXITCODE): $($out -join '; ')"
    }
    return , $out
}

function Stop-OwnedProcesses {
    <#
      One pass, both halves: the app first (asked politely, forced only if it ignores the
      request for GRACEFUL_S), then any collector still holding an ETW session. Asking is
      not politeness for its own sake - the app stops its own presentmon and closes the
      COM port on the way out, which a forced stop never lets it do, and an orphaned ETW
      session is what the old orphan pass existed to clean up.
    #>
    Invoke-Owned 'stop' | Where-Object { $_ -notmatch '^(pids|collectors) ' }
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
    Stop-OwnedProcesses
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
Stop-OwnedProcesses
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
# The health check used to ask the same substring question the stop helper did, so it
# could just as happily report somebody else's main.py as "our task is running" - and it
# did, in the other direction: another project's server would make a failed install look
# healthy. Same ownership module, same canonical paths, so "running" means this install.
$own = Invoke-Owned 'live'
$appLine = @($own | Where-Object { $_ -match '^pids ' })
$pids = if ($appLine) { ($appLine[0] -replace '^pids ', '') } else { '-' }
$own | Where-Object { $_ -notmatch '^(pids|collectors) ' } | ForEach-Object { "  $_" }
if ($pids -and $pids -ne '-') {
    # two pids are normal: the venv launcher and the interpreter it hands off to. The
    # interpreter is the one whose parent is the other one; presentmon hangs off it.
    "  app pids   : $pids"
    $kidLine = @($own | Where-Object { $_ -match '^collectors ' })
    $kids = if ($kidLine) { ($kidLine[0] -replace '^collectors ', '') } else { '-' }
    "  frame stats: $(if ($kids -and $kids -ne '-') { "presentmon pid $kids" } else { 'no presentmon child - frame stats are off, see log.log' })"
    (Get-Content (Join-Path $Root 'log.log') -Tail 3 -ErrorAction SilentlyContinue) | ForEach-Object { "  log: $_" }
} else {
    "  NOT running - check $Root\log.log (the app writes its start-up trace there)"
}
